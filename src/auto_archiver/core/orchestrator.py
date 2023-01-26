from __future__ import annotations
from ast import List
from typing import Union, Dict
from dataclasses import dataclass

from ..archivers import Archiver
from ..feeders import Feeder
from ..formatters import Formatter
from ..storages import Storage
from ..enrichers import Enricher
from ..databases import Database
from .media import Media
from .metadata import Metadata

import tempfile, time, traceback
from loguru import logger


"""
how not to couple the different pieces of logic
due to the use of constants for the metadata keys?
perhaps having methods on the Metadata level that can be used to fetch a limited number of
keys, never using strings but rather methods?
eg: m = Metadata() 
    m.get("screenshot") vs m.get_all()
    m.get_url()
    m.get_hash()
    m.get_main_file().get_title()
    m.get_screenshot() # this method should only exist because of the Screenshot Enricher
    # maybe there is a way for Archivers and Enrichers and Storages to add their own methdods
    # which raises still the Q of how the database, eg., knows they exist? 
    # maybe there's a function to fetch them all, and each Database can register wathever they get
    # for eg the GoogleSheets will only register based on the available column names, it knows what it wants
    # and if it's there: great, otherwise business as usual.
    # and a MongoDatabase could register all data, for example.
    # 
How are Orchestrators created? from a configuration file?
    orchestrator = ArchivingOrchestrator(config)
        # Config contains 1 URL, or URLs, from the command line
        # OR a feeder which is described in the config file
        # config.get_feeder() # if called as docker run --url "http...." then the uses the default filter
        # if config.yaml says config
    orchestrator.start()


Example applications:
1. auto-archiver for GSheets
2. archiver for URL: feeder is CLIFeeder(config.cli.urls="") # --urls="u1,u2"
3. archiver backend for a UI that implements a REST API, the API calls CLI

Cisticola considerations:
1. By isolating the archiving logic into "Archiving only pieces of logic" these could simply call cisticola.tiktok_scraper(user, pass)
2. So the auto-archiver becomes like a puzzle and fixes to Cisticola scrapers can immediately benefit it, and contributions are focused on a single source or scraping
"""


class ArchivingOrchestrator:
    def __init__(self, config) -> None:
        self.feeder: Feeder = config.feeder
        self.formatter: Formatter = config.formatter
        self.enrichers = config.enrichers
        self.archivers: List[Archiver] = config.archivers
        self.databases: List[Database] = config.databases
        self.storages: List[Storage] = config.storages

        for a in self.archivers: a.setup()

    def feed(self) -> None:
        for item in self.feeder:
            self.feed_item(item)

    def feed_item(self, item: Metadata) -> Metadata:
        print("ARCHIVING", item)
        try:
            with tempfile.TemporaryDirectory(dir="./") as tmp_dir:
                item.set_tmp_dir(tmp_dir)
                return self.archive(item)
        except KeyboardInterrupt:
            # catches keyboard interruptions to do a clean exit
            logger.warning(f"caught interrupt on {item=}")
            for d in self.databases: d.aborted(item)
            exit()
        except Exception as e:
            logger.error(f'Got unexpected error on item {item}: {e}\n{traceback.format_exc()}')
            for d in self.databases: d.failed(item)

        # how does this handle the parameters like folder which can be different for each archiver?
        # the storage needs to know where to archive!!
        # solution: feeders have context: extra metadata that they can read or ignore,
        # all of it should have sensible defaults (eg: folder)
        # default feeder is a list with 1 element

    def archive(self, result: Metadata) -> Union[Metadata, None]:
        original_url = result.get_url()

        # 1 - cleanup
        # each archiver is responsible for cleaning/expanding its own URLs
        url = original_url
        for a in self.archivers: url = a.sanitize_url(url)
        result.set_url(url)
        if original_url != url: result.set("original_url", original_url)

        # 2 - rearchiving logic + notify start to DB
        # archivers can signal whether the content is rearchivable: eg: tweet vs webpage
        for a in self.archivers: result.rearchivable |= a.is_rearchivable(url)
        logger.debug(f"{result.rearchivable=} for {url=}")

        # signal to DB that archiving has started
        # and propagate already archived if it exists
        cached_result = None
        for d in self.databases:
            # are the databases to decide whether to archive?
            # they can simply return True by default, otherwise they can avoid duplicates. should this logic be more granular, for example on the archiver level: a tweet will not need be scraped twice, whereas an instagram profile might. the archiver could not decide from the link which parts to archive,
            # instagram profile example: it would always re-archive everything
            # maybe the database/storage could use a hash/key to decide if there's a need to re-archive
            d.started(result)
            if (local_result := d.fetch(result)):
                cached_result = (cached_result or Metadata()).merge(local_result)
        if cached_result and not cached_result.rearchivable:
            logger.debug("Found previously archived entry")
            for d in self.databases:
                d.done(cached_result)
            return cached_result

        # 3 - call archivers until one succeeds
        for a in self.archivers:
            logger.info(f"Trying archiver {a.name}")
            try: 
                # Q: should this be refactored so it's just a.download(result)?
                result.merge(a.download(result))
                if result.is_success(): break
            except Exception as e: logger.error(f"Unexpected error with archiver {a.name}: {e}")

        # what if an archiver returns multiple entries and one is to be part of HTMLgenerator?
        # should it call the HTMLgenerator as if it's not an enrichment?
        # eg: if it is enable: generates an HTML with all the returned media, should it include enrichers? yes
        # then how to execute it last? should there also be post-processors? are there other examples?
        # maybe as a PDF? or a Markdown file

        # 4 - call enrichers: have access to archived content, can generate metadata and Media
        # eg: screenshot, wacz, webarchive, thumbnails
        for e in self.enrichers:
            e.enrich(result)

        # 5 - store media
        # looks for Media in result.media and also result.media[x].properties (as list or dict values)
        for s in self.storages:
            for m in result.media:
                s.store(m, result)  # modifies media
                # Media can be inside media properties, examples include transformations on original media
                for prop in m.properties.values():
                    if isinstance(prop, Media):
                        s.store(prop, result)
                    if isinstance(prop, list) and len(prop) > 0 and isinstance(prop[0], Media):
                        for prop_media in prop:
                            s.store(prop_media, result)

        # 6 - format and store formatted if needed
        # enrichers typically need access to already stored URLs etc
        if (final_media := self.formatter.format(result)):
            for s in self.storages:
                s.store(final_media, result)
            result.set_final_media(final_media)

        # signal completion to databases (DBs, Google Sheets, CSV, ...)
        for d in self.databases: d.done(result)

        return result
