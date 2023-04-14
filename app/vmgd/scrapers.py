
# TODO rename process to scrape

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



ProcessResult = tuple[datetime, Any]


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