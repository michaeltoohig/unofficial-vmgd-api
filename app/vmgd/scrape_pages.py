from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, List

import anyio
from bs4 import BeautifulSoup
from cerberus_list_schema import Validator as ListValidator
from cerberus import Validator, SchemaError
import httpx
from loguru import logger
from app import config

from app.database import AsyncSession, async_session
from app.pages import handle_page_error, process_issued_at
from app.vmgd.aggregators import aggregate_forecast_week
from app.vmgd.exceptions import FetchError, PageNotFoundError, PageUnavailableError, PageErrorTypeEnum, ScrapingError, ScrapingIssuedAtError, ScrapingNotFoundError, ScrapingValidationError
# from app.vmgd.pages import PageMapping
from app.vmgd.schemas import process_public_forecast_7_day_schema, process_forecast_schema
from app.utils.datetime import as_vu_to_utc, now
from app.vmgd.scrapers import scrape_forecast, scrape_public_forecast_7_day



@dataclass
class PageMapping:
    relative_url: str
    process: callable
    scraper: callable = None
    # process_images: callable | None  # TODO decide how to handle pages that have images.
    # either a new process step to define for each page
    # or better yet return an 'images' key with the process results and treat that key special in the `process_page` function
    # the next level would be to save `PageImage` models as children of the Page model to keep them sorted and to prevent saving duplicate images store image hashes there, etc.
    # TODO figure it out next time to continue here

    # TODO add html: str to keey result of scraper
    # TODO add raw_results: Dict[str, Any] to keep results from process

    @property
    def url(self):
        return config.VMGD_BASE_URL + self.relative_url

    @property
    def slug(self):
        return self.relative_url.rsplit("/", 1)[1]


async def fetch(url: str) -> str:
    logger.info(f"Fetching {url}")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
            },
            follow_redirects=True,
        )

    if resp.status_code in [401, 403]:
        raise PageUnavailableError(url, resp)
    elif resp.status_code == 404:
        raise PageNotFoundError(url, resp)

    try:
        resp.raise_for_status()
    except httpx.HTTPError as http_error:
        raise FetchError(url, resp) from http_error

    return resp.text


def check_cache(page: PageMapping) -> str | None:
    # caching is for development only
    html = None
    cache_file = Path(config.ROOT_DIR / "data" / "vmgd" / page.slug)
    if cache_file.exists():
        logger.info(f"Fetching page from cache {page.slug=}")
        html = cache_file.read_text()
    return html, cache_file


async def fetch_page(page: PageMapping):
    cache_file = None
    if config.DEBUG:
        html, cache_file = check_cache(page)
        if html:
            return html

    html = await fetch(page.url)

    if config.DEBUG:
        cache_file.write_text(html)
    return html


async def process_page_mapping(db_session: AsyncSession, mapping: PageMapping):
    error = None

    # latest_page = await get_latest_page(db_session, ptf.url)
    # if latest_page and as_utc(latest_page.fetched_at) < now() + timedelta(minutes=30):
    #     logger.info("Skipping page as it has recently been fetched successfully.")
    #     return

    # grab the HTML
    try:
        fetched_at = now()
        html = await fetch_page(mapping)
    except httpx.TimeoutException:
        error = (PageErrorTypeEnum.TIMEOUT, None)
    except PageUnavailableError as e:
        error = (PageErrorTypeEnum.UNAUHTORIZED, e)
    except PageNotFoundError:
        error = (PageErrorTypeEnum.NOT_FOUND, e)
    except Exception as exc:
        logger.exception("Unexpected error fetching page: %s" % str(exc))
        error = (PageErrorTypeEnum.INTERNAL_ERROR, None)

    if error:
        error_type, exc = error
        await handle_page_error(
            db_session,
            url=mapping.url,
            description=error_type.value,
            html=getattr(exc, "html", None),
            raw_data=getattr(exc, "raw_data", None),
            errors=getattr(exc, "errors", None),
        )
        return False

    # process the HTML
    try:
        issued_at, data = await mapping.process(html)
    except ScrapingNotFoundError as e:
        error = (PageErrorTypeEnum.DATA_NOT_FOUND, e)
    except ScrapingValidationError as e:
        error = (PageErrorTypeEnum.DATA_NOT_VALID, e)
    except ScrapingIssuedAtError as e:
        error = (PageErrorTypeEnum.ISSUED_NOT_FOUND, e)
    except Exception as exc:
        logger.exception("Unexpected error processing page: %s" % str(exc))
        error = (PageErrorTypeEnum.INTERNAL_ERROR, None)
        return False

    if error:
        error_type, exc = error
        await handle_page_error(
            db_session,
            url=mapping.url,
            description=error_type.value,
            html=getattr(exc, "html", None),
            raw_data=getattr(exc, "raw_data", None),
            errors=getattr(exc, "errors", None),
        )
        return False

    # XXX in this new style then this would be part of the process function and only errors are handled in this function
    # page = models.Page(
    #     url=ptf.url, issued_at=issued_at, raw_data=data, fetched_at=fetched_at
    # )
    # db_session.add(page)
    # await db_session.commit()
    # return True

    return issued_at, data




@dataclass
class PageSet:
    pages: List[PageMapping]
    process: callable  # processes results from PageMappings


page_sets = [
    PageSet(
        pages = [
            PageMapping("/forecast-division", scrape_forecast),
            PageMapping(
                "/forecast-division/public-forecast/7-day", scrape_public_forecast_7_day
            ),
        ],
        process = aggregate_forecast_week, 
    ),
]


# aggregate_data
async def process_page_set(page_set: PageSet):
    set_data = []
    for mapping in page_set.pages:
        logger.debug(mapping)
        # TODO later: somehow make sure the whole set exists if a page was recently fetched successfully already
        # TODO fetch each url async like
        # TODO process the data
        try:
            logger.info(f"page url {mapping.url}")
            page_data = await process_page_mapping(None, mapping)
            if not page_data:
                raise Exception("No return")
            logger.info(f"got data len {len(page_data)}")
        except:
            logger.error("Processing page failed, aborting the full page set")
            raise
        set_data.append(page_data)

        # TODO lastly, run the PageSet.process function to aggregate and store a coherent forecast to database for use by API
        # this will need to be unique for each page set.
        # Some of the simple pages to fetch could do without this step perhaps.
        # ... or `run_process_all_pages` does PageSets and individual pages

    # TODO handle errors, etc.
    await page_set.process(set_data)


async def run_process_all_pages() -> None:
    """CLI entrypoint."""
    # headers = {
    #     "User-Agent": config.USER_AGENT,
    # }
    # async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
    #     async with anyio.create_task_group() as tg:
    #         for ptf in pages_to_fetch:
    #             tg.start_soon(process_page, ptf)

    # async with anyio.create_task_group() as tg:
    #     for ptf in pages_to_fetch:
    #         tg.start_soon(process_page, None, ptf)

    async with anyio.create_task_group() as tg:
        for page_set in page_sets:
            tg.start_soon(process_page_set, page_set)


    # async with httpx.AsyncClient() as client:
    #     async with async_session() as db_session:
    #         set_data = {}
    #         for pg in page_sets:
    #             page_data = await process(pg)
    #             set_data.append(page_data)
    #             await pg.process(set_data)
    #     # etc... WIP

    # async with anyio.create_task_group() as tg:
    #     for ss in page_sets:
    #         tg.start_soon(aggregate_data, ss)