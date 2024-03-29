from dataclasses import dataclass
from typing import Any
from datetime import datetime
import json

from bs4 import BeautifulSoup
from cerberus_list_schema import Validator as ListValidator
from cerberus import Validator, SchemaError
from loguru import logger

from app.scraper.exceptions import (
    ScrapingError,
    ScrapingIssuedAtError,
    ScrapingNotFoundError,
    ScrapingValidationError,
)
from app.scraper.schemas import (
    WeatherObject,
    process_public_forecast_7_day_schema,
    process_forecast_schema,
)
from app.scraper.utils import strip_html_text
from app.utils.datetime import as_vu_to_utc


@dataclass
class ScrapeResult:
    raw_data: Any | None = None
    issued_at: datetime | None = None
    images: list[str] | None = None


def process_issued_at(
    text: str, delimiter_start: str, delimiter_end: str = "(utc time"
) -> datetime:
    """Given a text containing the `issued_at` value found between two delimiters extract the value and convert to a datetime in UTC.
    The general date format appears to be "%a %dXX %B, %Y at %H:%M" where `%dXX` is an ordinal number.
    Examples:
     - "Mon 27th March, 2023 at 15:02 (UTC Time:04:02)"
     - "Tue 28th March, 2023 at 16:05 (UTC Time:05:05)"
     - "Friday 24th March, 2023 at 17:43 (UTC Time:06:43)"
     - "Tuesday 2nd May, 2023 at 17:27 (UTC Time:06:27)"

    So far I noticed the format of the date appears consistent across pages but the delimiter for the start of the date is inconsistent.
    """
    # Prep the string
    issued_date_str = (
        text.lower()
        .split(delimiter_start.lower(), 1)[1]
        .split(delimiter_end.lower())[0]
        .strip()
    )
    issued_date_parts = issued_date_str.split()
    issued_day = issued_date_parts[1][:-2]  # remove 'st', 'nd', 'rd', 'th'
    issued_date_parts[1] = issued_day
    issued_date_str = " ".join(issued_date_parts)
    # Parse the string
    day_fmt = "%A" if "day" in issued_date_parts[0] else "%a"
    issued_at = datetime.strptime(issued_date_str, f"{day_fmt} %d %B, %Y at %H:%M")
    return as_vu_to_utc(issued_at)


async def scrape_forecast(html: str) -> ScrapeResult:
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
        # XXX this makes it hard to serialize into database for `Page` model
        # weathers = list(map(lambda w: WeatherObject(*w), weathers))
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
    return ScrapeResult(raw_data=weathers, issued_at=issued_at)


# Public Forecast
#################


async def scrape_public_forecast(html: str) -> ScrapeResult:
    """The about page of the weather forecast section.

    TODO collect the text from table element with `<article class="item-page">` and
    hash it; store the hash and date collected in a directory so that only when the
    hash changes do we save a new page. This can also alert us to changes in the
    about page which may signal other important changes to how data is collected and
    reported in other forecast pages.
    """
    raise NotImplementedError


async def scrape_public_forecast_policy(html: str) -> ScrapeResult:
    # TODO hash text contents of `<table class="forecastPublic">` to make a sanity
    # check that data presented or how data is processed is not changed. Only store
    # copies of the page that show a new hash value... I think. But maybe this is
    # the wrong html page downloaded as it appears same as `publice-forecast`
    raise NotImplementedError


async def scrape_severe_weather_outlook(html: str) -> ScrapeResult:
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


async def scrape_public_forecast_tc_outlook(html: str) -> ScrapeResult:
    raise NotImplementedError


async def scrape_public_forecast_7_day(html: str) -> ScrapeResult:
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
        issued_str = strip_html_text(
            soup.article.find("table").find_previous_sibling("strong").text
        )
        issued_at = process_issued_at(issued_str, "Port Vila at")
    except (IndexError, ValueError):
        raise ScrapingIssuedAtError(html)
    return ScrapeResult(raw_data=forecasts, issued_at=issued_at)


async def scrape_public_forecast_media(html: str) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")
    try:
        table = soup.find("table", class_="forecastPublic")
        assert table is not None, "public forecast table is missing"
    except Exception as exc:
        raise ScrapingNotFoundError(html, errors=str(exc))

    try:
        images = table.find_all("img")
        # TODO maybe allow no images
        assert len(images) > 0, "public forecast media images missing"
    except AssertionError as exc:
        raise ScrapingNotFoundError(html, errors=str(exc))

    try:
        summary_list = [t for t in table.div.contents if isinstance(t, str)]
        summary_list = list(filter(lambda t: bool(t.strip()), summary_list))
        summary = " ".join(
            " ".join([strip_html_text(t) for t in summary_list]).split("\n")
        )
    except Exception as exc:  # TODO handle expected errors
        raise ScrapingValidationError(html, errors=str(exc))

    try:
        issued_str = strip_html_text(table.div.find_all("div")[1].text).split(
            " at ", 1
        )[1]
        issued_at = datetime.strptime(issued_str, "%H:%M %p,%A %B %d %Y")
        issued_at = as_vu_to_utc(issued_at)
    except (IndexError, ValueError) as exc:
        raise ScrapingIssuedAtError(html, errors=str(exc))

    return ScrapeResult(raw_data=summary, issued_at=issued_at, images=images)


# Warnings
##########

NO_CURRENT_WARNING = "no current warning"
NoCurrentWarningsResult = ScrapeResult(raw_data=NO_CURRENT_WARNING)


async def scrape_current_bulletin(html: str) -> ScrapeResult:
    """Special bulletin board for warnins that seems to have a unique layout compared to the other warning pages.
    I do not have an example of warnings yet so I can not implement it yet."""
    soup = BeautifulSoup(html, "html.parser")
    try:
        warnings_table = soup.find("div", class_="foreWarning")
        if "no latest warning" in strip_html_text(warnings_table.h4.text):
            logger.info("No current warning reported")
            return NoCurrentWarningsResult
        else:
            raise NotImplementedError
        
        # if warnings_table:
        #     logger.debug("No warnings table found")
        #     assert "no latest warning" in strip_html_text(warnings_table.text), "Exepcted `no latest warning` in text"
        #     logger.info("No current warning reported")
        #     return NoCurrentWarningsResult
        # else:
        #     logger.debug("warnings table found")
        #     current_warnings = []
        #     raise NotImplementedError
        #     # TODO when I have an example to reference
    except Exception as exc:
        raise ScrapingNotFoundError(html, errors=str(exc))

    # TODO get issued_at


async def scrape_weather_warnings(html: str) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")
    # grab data for each warning from table
    try:
        warnings_table = soup.find("table", class_="marineFrontTabOne")
        if not warnings_table:
            logger.debug("No warnings table found")
            article = soup.find("p", class_="weatherBulletin").find_parent(
                "article", class_="item-page"
            )
            assert article is not None, "Expected html article is not found"
            assert (
                "no current warning" in strip_html_text(article.text).lower()
            ), "Expected `no current warning` in text"
            logger.info("No current warning reported")
            return NoCurrentWarningsResult
        else:
            logger.debug("warnings table found")
            current_warnings = []
            cw_tablerows = warnings_table.find_all("tr")
            assert (
                len(cw_tablerows) % 2 == 0
            ), "Expected even number of warning rows in table"
            for idx in range(
                2, len(warnings_table.find_all("tr")), 2
            ):  # start=2 to skip issued_at rows
                warn_date_str = strip_html_text(cw_tablerows[idx].text)
                warn_body = strip_html_text(cw_tablerows[idx + 1].text)
                current_warnings.append(
                    dict(
                        date=warn_date_str,
                        body=warn_body,
                    )
                )
    except Exception as exc:
        raise ScrapingNotFoundError(html, errors=str(exc))

    # grab issued at datetime
    try:
        issued_str = strip_html_text(warnings_table.find("tr").text)
        issued_at = process_issued_at(issued_str, "report issued at")
    except (IndexError, ValueError):
        raise ScrapingIssuedAtError(html)

    return ScrapeResult(raw_data=current_warnings, issued_at=issued_at)
