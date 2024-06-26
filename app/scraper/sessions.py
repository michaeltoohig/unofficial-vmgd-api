from dataclasses import dataclass
import enum

from app.scraper.pages import PageMapping, PagePath
from app.scraper.scrapers import (
    scrape_current_bulletin,
    scrape_forecast,
    scrape_public_forecast_7_day,
    scrape_public_forecast_media,
    scrape_weather_warnings,
)
from app.scraper.aggregators import (
    aggregate_forecast_media,
    aggregate_forecast_week,
    aggregate_weather_warnings,
)


@dataclass
class SessionMapping:
    name: str
    pages: list[PageMapping]
    process: callable  # processes results from PageMappings


class ForecastSession(str, enum.Enum):
    FORECAST_GENERAL = "forecast_general"
    FORECAST_MEDIA = "forecast_media"


class WarningSession(str, enum.Enum):
    WARNING_BULLETIN = "warning_bulletin"
    WARNING_MARINE = "warning_marine"
    WARNING_HIGHT_SEAS = "warning_hight_seas"
    WARNING_SEVERE_WEATHER = "warning_severe_weather"


session_mappings = [
    SessionMapping(
        name=ForecastSession.FORECAST_GENERAL,
        pages=[
            PageMapping(PagePath.FORECAST_MAP, scrape_forecast),
            PageMapping(PagePath.FORECAST_WEEK, scrape_public_forecast_7_day),
        ],
        process=aggregate_forecast_week,
    ),
    SessionMapping(
        name=ForecastSession.FORECAST_MEDIA,
        pages=[PageMapping(PagePath.FORECAST_MEDIA, scrape_public_forecast_media)],
        process=aggregate_forecast_media,
    ),
    SessionMapping(
        name=WarningSession.WARNING_BULLETIN,
        pages=[PageMapping(PagePath.WARNING_BULLETIN, scrape_current_bulletin)],
        process=aggregate_weather_warnings,
    ),
    SessionMapping(
        name=WarningSession.WARNING_MARINE,
        pages=[PageMapping(PagePath.WARNING_MARINE, scrape_weather_warnings)],
        process=aggregate_weather_warnings,
    ),
    SessionMapping(
        name=WarningSession.WARNING_HIGHT_SEAS,
        pages=[PageMapping(PagePath.WARNING_HIGHT_SEAS, scrape_weather_warnings)],
        process=aggregate_weather_warnings,
    ),
    SessionMapping(
        name=WarningSession.WARNING_SEVERE_WEATHER,
        pages=[PageMapping(PagePath.WARNING_SEVERE_WEATHER, scrape_weather_warnings)],
        process=aggregate_weather_warnings,
    ),
]
