from dataclasses import dataclass
from datetime import datetime, timedelta
import enum
import json
from pathlib import Path
from typing import Any
import uuid

import anyio
from bs4 import BeautifulSoup
from cerberus_list_schema import Validator as ListValidator
from cerberus import Validator, SchemaError
import httpx
from loguru import logger

from app import config, models
from app.database import AsyncSession, async_session
from app.locations import get_location_by_name, save_forecast_location
from app.pages import get_latest_page, handle_page_error, process_issued_at
from app.utils.datetime import as_utc, as_vu_to_utc, now


BASE_URL = "https://www.vmgd.gov.vu/vmgd/index.php"
ProcessResult = tuple[datetime, Any]


def _save_html(html: str, fp: Path) -> Path:
    vmgd_directory = Path(config.ROOT_DIR) / "data" / "vmgd"
    if fp.is_absolute():
        if not fp.is_relative_to(vmgd_directory):
            raise Exception(f"Bad path for saving html {fp}")
    else:
        fp = vmgd_directory / fp
        if not fp.parent.exists():
            fp.parent.mkdir(parents=True)
    fp.write_text(html)



class FetchError(Exception):
    def __init__(self, url: str, resp: httpx.Response | None = None) -> None:
        resp_part = ""
        if resp:
            filename = Path("errors") / str(uuid.uuid4())
            filepath = _save_html(resp.text, filename)
            resp_part = f", got HTTP {resp.status_code}, review HTML at {str(filename)}"
        message = f"Failed to fetch {url}{resp_part}"
        super().__init__(message)
        self.html_filepath = filepath
        self.resp = resp
        self.url = url


class PageUnavailableError(FetchError):
    pass


class PageNotFoundError(FetchError):
    pass


class ScrapingError(Exception):
    def __init__(
        self, html: str, raw_data: Any | None = None, errors: Any | None = None
    ) -> None:
        # filename = Path("errors") / str(uuid.uuid4())
        # filepath = _save_html(html, filename)
        errors_part = ""
        if errors:
            errors_part = f", got schema validation errors"
        message = f"Failed to scrape page{errors_part}"
        super().__init__(message)
        self.html = html
        self.raw_data = raw_data
        self.errors = errors


class ScrapingNotFoundError(ScrapingError):
    pass


class ScrapingValidationError(ScrapingError):
    pass


class ScrapingIssuedAtError(ScrapingError):
    pass


class PageErrorTypeEnum(str, enum.Enum):
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    UNAUHTORIZED = "UNAUTHORIZED"

    DATA_NOT_FOUND = "DATA_NOT_FOUND"
    DATA_NOT_VALID = "DATA_NOT_VALID"
    ISSUED_NOT_FOUND = "ISSUED_NOT_FOUND"

    INTERNAL_ERROR = "INTERNAL_ERROR"


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


process_forecast_schema = {
    "type": "list",
    "items": [
        {
            "type": "string",
            "name": "location",
        },
        {
            "type": "float",
            "name": "latitude",
        },
        {
            "type": "float",
            "name": "longitude",
        },
        {
            "type": "list",
            "name": "date",
            "items": [{"type": "string"} for _ in range(8)],
        },
        {
            "type": "list",
            "name": "minTemp",
            "items": [{"type": "integer"} for _ in range(7)],
        },
        {
            "type": "list",
            "name": "maxTemp",
            "items": [{"type": "integer"} for _ in range(7)],
        },
        {
            "type": "list",
            "name": "minHumi",
            "items": [{"type": "integer"} for _ in range(7)],
        },
        {
            "type": "list",
            "name": "maxHumi",
            "items": [{"type": "integer"} for _ in range(7)],
        },
        {
            "type": "list",
            "name": "weatherCondition",
            "items": [{"type": "integer"} for _ in range(16)],
        },
        {
            "type": "list",
            "name": "windDirection",
            "items": [{"type": "float"} for _ in range(16)],
        },
        {
            "type": "list",
            "name": "windSpees",
            "items": [{"type": "integer"} for _ in range(16)],
        },
        {
            "type": "integer",
            "name": "dtFlag",
        },
        {
            "type": "string",
            "name": "currentDate",
        },
        {
            "type": "list",
            "name": "dateHour",
            "items": [{"type": "string"} for _ in range(16)],
        },
    ],
}


async def process_forecast(html: str) -> ProcessResult:
    """The main forecast page with daily temperature and humidity information and 6 hour
    interval resolution for weather condition, wind speed/direction.
    All information is encoded in a special `<script>` that contains a `var weathers`
    array which contains everything needed to reconstruct the information found in the
    forecast map.
    The specifics of how to decode the `weathers` array is found in the `xmlForecast.js`
    file that is on the page.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Find JSON containing script tag
    weathers_script = None
    for script in soup.find_all("script"):
        if script.text.strip().startswith("var weathers"):  # special value
            weathers_script = script
            break
    else:
        raise ScrapingNotFoundError(html)

    # grab JSON data from script tag
    try:
        weathers_line = weathers_script.text.strip().split("\n", 1)[0]
        weathers_array_string = weathers_line.split(" = ", 1)[1].rsplit(";", 1)[0]
        weathers = json.loads(weathers_array_string)
        v = ListValidator(process_forecast_schema)
        errors = []
        for location in weathers:
            if not v.validate(location):
                errors.append(v.errors)
        if errors:
            raise ScrapingValidationError(html, weathers, errors)
    except SchemaError as exc:
        raise ScrapingValidationError(html, weathers, str(exc))
    # I believe catching a general exception here negates the use of raising the error above
    # except Exception as exc:
    #     logger.exception("Failed to grab data: %s", str(exc))
    #     raise ScrapingNotFoundError(html)

    # grab issued at datetime
    try:
        issued_str = soup.find("div", id="issueDate").text
        issued_at = process_issued_at(issued_str, "Forecast Issue Date:")
    except (IndexError, ValueError) as exc:
        raise ScrapingIssuedAtError(html)
    return issued_at, weathers


# Public Forecast
#################


async def process_public_forecast(html: str) -> ProcessResult:
    """The about page of the weather forecast section.

    TODO collect the text from table element with `<article class="item-page">` and
    hash it; store the hash and date collected in a directory so that only when the
    hash changes do we save a new page. This can also alert us to changes in the
    about page which may signal other important changes to how data is collected and
    reported in other forecast pages.
    """
    raise NotImplementedError


async def process_public_forecast_policy(html: str) -> ProcessResult:
    # TODO hash text contents of `<table class="forecastPublic">` to make a sanity
    # check that data presented or how data is processed is not changed. Only store
    # copies of the page that show a new hash value... I think. But maybe this is
    # the wrong html page downloaded as it appears same as `publice-forecast`
    raise NotImplementedError


async def process_severe_weather_outlook(html: str) -> ProcessResult:
    raise NotImplementedError
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="severeTable")
    # TODO assert table
    # TODO assert tablerows are 4
    # tr0 is date issues
    # tr1 is rainfall outlook
    # tr2 is inland wind outlook
    # tr3 is coastal wind outlook
    # any additional trX should be alerted and accounted for in future


async def process_public_forecast_tc_outlook(html: str) -> ProcessResult:
    raise NotImplementedError


process_public_forecast_7_day_schema = {
    "location": {"type": "string", "empty": False},
    "date": {"type": "string", "empty": False},
    "summary": {"type": "string"},
    "minTemp": {"type": "integer", "coerce": int, "min": 0, "max": 50},
    "maxTemp": {"type": "integer", "coerce": int, "min": 0, "max": 50},
}


async def process_public_forecast_7_day(html: str) -> ProcessResult:
    """Simple weekly forecast for all locations containing daily low/high temperature,
    and weather condition summary.
    """
    forecasts = []
    soup = BeautifulSoup(html, "html.parser")
    # grab data for each location from individual tables
    try:
        for table in soup.article.find_all("table"):
            for count, tr in enumerate(table.find_all("tr")):
                if count == 0:
                    location = tr.text.strip()
                    continue
                date, forecast = tr.text.strip().split(" : ")
                summary = forecast.split(".", 1)[0]
                minTemp = int(forecast.split("Min:", 1)[1].split("&", 1)[0].strip())
                maxTemp = int(forecast.split("Max:", 1)[1].split("&", 1)[0].strip())
                forecasts.append(
                    dict(
                        location=location,
                        date=date,
                        summary=summary,
                        minTemp=minTemp,
                        maxTemp=maxTemp,
                    )
                )
        v = Validator(process_public_forecast_7_day_schema)
        errors = []
        for location in forecasts:
            if not v.validate(location):
                errors.append(v.errors)
        if errors:
            raise ScrapingValidationError(html, forecasts, errors)
    except SchemaError as exc:
        raise ScrapingValidationError(html, forecasts, str(exc))

    # grab issued at datetime
    try:
        issued_str = (
            soup.article.find("table").find_previous_sibling("strong").text.lower()
        )
        issued_at = process_issued_at(issued_str, "Port Vila at")
    except (IndexError, ValueError):
        raise ScrapingIssuedAtError(html)
    return issued_at, forecasts


async def process_public_forecast_media(html: str) -> ProcessResult:
    soup = BeautifulSoup(html, "html.parser")
    try:
        table = soup.find("table", class_="forecastPublic")
    except:
        raise ScrapingNotFoundError(html)
    
    try:
        images = table.find_all("img")
        assert len(images) > 0, "public forecast media images missing"
    except AssertionError as exc:
        raise ScrapingNotFoundError(html, errors=str(exc))

    try:
        summary_list = [t for t in table.div.contents if isinstance(t, str)]
        summary_list = list(filter(lambda t: bool(t.strip()), summary_list))
        summary = " ".join(" ".join([t.replace("\t", "").strip() for t in summary_list]).split("\n"))
    except Exception as exc:  # TODO handle expected errors
        raise ScrapingValidationError(html, errors=str(exc))

    try:
        issued_str = table.div.find_all("div")[1].text.strip().split(" at ", 1)[1]
        issued_at = datetime.strptime(issued_str, "%H:%M %p,\xa0%A %B %d %Y")
        issued_at = as_vu_to_utc(issued_at)
    except (IndexError, ValueError) as exc:
        raise ScrapingIssuedAtError(html, errors=str(exc))

    return issued_at, summary, images


# Warnings
##########


async def process_current_bulletin(html: str) -> ProcessResult:
    raise NotImplementedError
    soup = BeautifulSoup(html, "html.parser")
    warning_div = soup.find("div", class_="foreWarning")
    if warning_div.text.lower().strip() == "there is no latest warning":
        # no warnings
        pass
    else:
        # has warnings
        pass


async def process_severe_weather_warning(html: str) -> ProcessResult:
    # TODO extract data from table with class `marineFrontTabOne`
    raise NotImplementedError


async def process_marine_waring(html: str) -> ProcessResult:
    # TODO extract data from table with class `marineFrontTabOne`
    raise NotImplementedError


async def process_hight_seas_warning(html: str) -> ProcessResult:
    # TODO extract data from `<article class="item-page">` and handle no warnings by text `NO CURRENT WARNING`
    raise NotImplementedError


@dataclass
class PageToFetch:
    relative_url: str
    process: callable
    # process_images: callable | None  # TODO decide how to handle pages that have images.
    # either a new process step to define for each page
    # or better yet return an 'images' key with the process results and treat that key special in the `process_page` function
    # the next level would be to save `PageImage` models as children of the Page model to keep them sorted and to prevent saving duplicate images store image hashes there, etc.
    # TODO figure it out next time to continue here

    @property
    def url(self):
        return BASE_URL + self.relative_url

    @property
    def slug(self):
        return self.relative_url.rsplit("/", 1)[1]

@dataclass
class PageSet:
    """Defines a group of `PageToFetch` objects that need to be gathered together to create a coherent end result.
    For example, both weather forecast and 7 day forecasts must be scraped and joined together to create a uniform 7 day forecast for each day of the week."""
    pages: list[PageToFetch]
    process: callable


# pages_to_fetch = [
#     PageToFetch("/forecast-division", process_forecast),
#     # PageToFetch("/forecast-division/public-forecast", process_public_forecast),
#     # PageToFetch(
#     #     "/forecast-division/public-forecast/forecast-policy",
#     #     process_public_forecast_policy,
#     # ),
#     # PageToFetch(
#     #     "/forecast-division/public-forecast/severe-weather-outlook",
#     #     process_severe_weather_outlook,
#     # ),
#     # PageToFetch(
#     #     "/forecast-division/public-forecast/tc-outlook",
#     #     process_public_forecast_tc_outlook,
#     # ),
#     PageToFetch(
#         "/forecast-division/public-forecast/7-day", process_public_forecast_7_day
#     ),
#     PageToFetch(
#         "/forecast-division/public-forecast/media", process_public_forecast_media
#     ),
#     # PageToFetch(
#     #     "/forecast-division/warnings/current-bulletin", process_current_bulletin
#     # ),
#     # PageToFetch(
#     #     "/forecast-division/warnings/severe-weather-warning",
#     #     process_severe_weather_warning,
#     # ),
#     # PageToFetch("/forecast-division/warnings/marine-warning", process_marine_waring),
#     # PageToFetch(
#     #     "/forecast-division/warnings/hight-seas-warning", process_hight_seas_warning
#     # ),
# ]


def check_cache(page: PageToFetch) -> str | None:
    # caching is for development only
    html = None
    cache_file = Path(config.ROOT_DIR / "data" / "vmgd" / page.slug)
    if cache_file.exists():
        logger.info(f"Fetching page from cache {page.slug=}")
        html = cache_file.read_text()
    return html, cache_file


async def fetch_page(page: PageToFetch):
    cache_file = None
    if config.DEBUG:
        html, cache_file = check_cache(page)
        if html:
            return html

    html = await fetch(page.url)

    if config.DEBUG:
        cache_file.write_text(html)
    return html


async def process_page(db_session: AsyncSession, ptf: PageToFetch):
    error = None

    async with async_session() as db_session:
        # latest_page = await get_latest_page(db_session, ptf.url)
        # if latest_page and as_utc(latest_page.fetched_at) < now() + timedelta(minutes=30):
        #     logger.info("Skipping page as it has recently been fetched successfully.")
        #     return

        # grab the HTML
        try:
            fetched_at = now()
            html = await fetch_page(ptf)
        except httpx.TimeoutException:
            error = (PageErrorTypeEnum.TIMEOUT, None)
        except PageUnavailableError as e:
            error = (PageErrorTypeEnum.UNAUHTORIZED, e)
        except PageNotFoundError:
            error = (PageErrorTypeEnum.NOT_FOUND, e)
        except Exception as exc:
            logger.exception("Unexpected error fetching page: %s", str(exc))
            error = (PageErrorTypeEnum.INTERNAL_ERROR, None)

        if error:
            error_type, exc = error
            await handle_page_error(
                db_session,
                url=ptf.url,
                description=error_type.value,
                html=getattr(exc, "html", None),
                raw_data=getattr(exc, "raw_data", None),
                errors=getattr(exc, "errors", None),
            )
            return False

        # process the HTML
        try:
            issued_at, data = await ptf.process(html)
        except ScrapingNotFoundError as e:
            error = (PageErrorTypeEnum.DATA_NOT_FOUND, e)
        except ScrapingValidationError as e:
            error = (PageErrorTypeEnum.DATA_NOT_VALID, e)
        except ScrapingIssuedAtError as e:
            error = (PageErrorTypeEnum.ISSUED_NOT_FOUND, e)
        except Exception as exc:
            logger.exception("Unexpected error processing page: %s", str(exc))
            error = (PageErrorTypeEnum.INTERNAL_ERROR, None)
            return False

        if error:
            error_type, exc = error
            await handle_page_error(
                db_session,
                url=ptf.url,
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


async def aggregate_forecast_data(data: list[tuple[datetime, any]]):
    """Handles forecast forecast data which currently comprises of 7-day forecast and 3 day forecast.
    Together the two pages can form a coherent weekly forecast."""

    issued_at_1, forecast_1 = data[0]
    issued_at_2, forecast_2 = data[1]

    locations_1 = set(map(lambda f: f[0], forecast_1))
    locations_2 = set(map(lambda f: f['location'], forecast_2))
    assert locations_1 == locations_2, "Forecast locations differ"

    # Organize data by location
    forecasts = {}
    for name in locations_1:
        forecast = {}
        data = next(filter(lambda x: x[0].lower() == name.lower(), forecast_1))
        v = ListValidator(process_forecast_schema)
        normalized_data = v.normalized_as_dict(data)
        starting_dt = datetime.strftime("%a %d", normalized_data["date"][0]).replace(year=issued_at_1.year, month=issued_at_1.month)
        dates = {0: starting_dt}
        for i in range(1, len(normalized_data["date"].keys())):
            dates[i] = starting_dt + timedelta(days=1)
            assert normalized_data["date"][i] == dates[i].strftime("%a %d")
        normalized_data["date"] = dates
        forecast["data_1"] = normalized_data
        data = list(filter(lambda x: x['location'].lower() == name.lower(), forecast_2))
        starting_dt = datetime.strftime("%A %d", data[0]["date"]).replace(year=issued_at_2.year, month=issued_at_2.month)
        for i in data:
            assert 
        forecast["data_2"] = data

        # Save location with forecast
        async with async_session() as db_session:
            latitude = forecast["data_1"]["latitude"]
            longitude = forecast["data_1"]["longitude"]
            location = await save_forecast_location(db_session, name, latitude, longitude)
            forecast["location"] = location
        
    # TODO orgainize data for each location into forecast objects
        
        # assert issued_at_1.strftime("%a %d") == forecast["data_1"]["date"][0]

        # Create daily forecast objects
        fo = forecast["data_1"]
        wanted_keys = ["date", "minTemp", "maxTemp", "minHumi", "maxHumi"]
        results = []
        for i in range(6):  # 7 days
            newDict = {}
            for key, value in fo.items():
                if key in wanted_keys:
                    newDict[key] = value[i]
            # assert forecast["data_2"][i]["date"] == newDict["date"]
            newDict.update({"summary": forecast["data_2"][0]["summary"]})
            newDict.update({"location": forecast["location"]})
            results.append(newDict)
        
        import pdb; pdb.set_trace()
        
        
        # data = [{key: value[i] for key, value in data.items()} for i in range(len(next(iter(data.values()))))]
        data = []
        for i in range(len(forecast["data_1"]["dates"]) - 1):
            fo = forecast["data_1"]
            try:
                da = dict(
                    date=fo["dates"][i],
                    minTemp=fo["minTemps"][i],
                    maxTemp=fo["maxTemps"][i],
                    minHumi=fo["minHumi"][i],
                    maxHumi=fo["maxHumi"][i],
                    summary=None,
                )
                data.append(da)
            except KeyError:
                logger.debug(f"{name} has run short at index={i}")
        forecast["daily"] = data

        # Create quarterly forecast objects
        import pdb; pdb.set_trace()
        
        data = []
        for i in range(len(forecast["data_2"])):
            pass

        
        # index 6/7 is daily humi values
    for location in forecast.keys():
        pass



page_sets = [
    PageSet(
        pages = [
            PageToFetch("/forecast-division", process_forecast),
            PageToFetch(
                "/forecast-division/public-forecast/7-day", process_public_forecast_7_day
            ),
        ],
        process = aggregate_forecast_data, 
    ),
]


async def aggregate_data(page_set: PageSet):
    async with httpx.AsyncClient() as client:
        set_data = []
        for ptf in page_set.pages:
            logger.debug(ptf)
            # TODO later: somehow make sure the whole set exists if a page was recently fetched successfully already
            # TODO fetch each url async like
            # TODO process the data
            try:
                logger.info(f"ptf {ptf.url}")
                page_data = await process_page(None, ptf)
                logger.info(f"got data len {len(page_data)}")
                if not page_data:
                    raise Exception("No return")
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
    # expect indexerror


async def process_all_pages(db_session) -> None:
    pass


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
        for ss in page_sets:
            tg.start_soon(aggregate_data, ss)