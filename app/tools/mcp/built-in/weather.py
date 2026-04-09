"""Weather MCP server using stdio transport.

A standalone FastMCP server that provides weather data via AccuWeather API.
Launched as a subprocess by olli-agent's MCP connection manager.

Based on: a2a-weather-agent/app/agent/server_mcp.py

Usage:
    python app/mcp/stdio/weather.py

Requires:
    ACCUWEATHER_API_KEY environment variable
"""

import os
import sys
from typing import Any, Dict

import httpx
from fastmcp import FastMCP
from fastmcp.tools.tool import Tool, ToolResult


from app.utils.logger import logger

# Initialize FastMCP server
mcp = FastMCP(
    name="Weather Agent",
    instructions="A weather assistant that retrieves weather forecasts and current conditions.",
)

ACCUWEATHER_API_KEY = os.environ.get("ACCUWEATHER_API_KEY", "")
ACCUWEATHER_BASE_URL = "http://dataservice.accuweather.com"


async def _search_location(location: str) -> Dict[str, Any] | None:
    """Search AccuWeather for a location and return its key and metadata."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ACCUWEATHER_BASE_URL}/locations/v1/cities/search",
            params={"apikey": ACCUWEATHER_API_KEY, "q": location},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        loc = data[0]
        return {
            "key": loc["Key"],
            "name": loc["LocalizedName"],
            "country": loc["Country"]["LocalizedName"],
            "administrative_area": loc.get("AdministrativeArea", {}).get("LocalizedName", ""),
        }


async def _get_current_conditions(location_key: str) -> Dict[str, Any] | None:
    """Fetch current weather conditions for a location."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ACCUWEATHER_BASE_URL}/currentconditions/v1/{location_key}",
            params={"apikey": ACCUWEATHER_API_KEY, "details": "true"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        return data[0]


async def _get_daily_forecast(location_key: str, days: int = 5) -> Dict[str, Any] | None:
    """Fetch daily weather forecast for a location."""
    endpoint = f"{ACCUWEATHER_BASE_URL}/forecasts/v1/daily/5day/{location_key}"
    if days > 5:
        endpoint = f"{ACCUWEATHER_BASE_URL}/forecasts/v1/daily/5day/{location_key}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            endpoint,
            params={"apikey": ACCUWEATHER_API_KEY, "details": "true", "metric": "true"},
        )
        if resp.status_code != 200:
            return None
        return resp.json()


class GetWeatherTool(Tool):
    name: str = "get_weather"
    description: str = "Fetches current weather conditions or forecast for a given location using AccuWeather."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The city or location name to fetch weather for (e.g., 'Tokyo', 'New York').",
            },
            "weather_type": {
                "type": "string",
                "description": "The type of weather data: 'current' for current conditions or 'forecast' for daily forecast.",
                "enum": ["current", "forecast"],
            },
        },
        # "required": ["location", "weather_type"],
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        logger.debug(arguments)
        location = arguments.get("location", "")
        weather_type = arguments.get("weather_type", "current")

        if not ACCUWEATHER_API_KEY:
            return ToolResult(
                structured_content={
                    "error": "Configuration error",
                    "message": "ACCUWEATHER_API_KEY is not set.",
                }
            )

        if not location:
            return ToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "Location is required.",
                }
            )

        try:
            # Search for location
            location_data = await _search_location(location)
            if not location_data:
                return ToolResult(
                    structured_content={
                        "error": "Location not found",
                        "message": f"Could not find location: {location}",
                    }
                )

            location_key = location_data["key"]
            location_name = (
                f"{location_data['name']}, "
                f"{location_data.get('administrative_area', '')}, "
                f"{location_data['country']}"
            )

            if weather_type == "current":
                weather_data = await _get_current_conditions(location_key)
                if not weather_data:
                    return ToolResult(
                        structured_content={
                            "error": "No data",
                            "message": f"Could not retrieve current weather for {location_name}",
                        }
                    )
                return ToolResult(
                    structured_content={
                        "type": "current_weather",
                        "location": location_name,
                        "current": weather_data,
                    }
                )

            elif weather_type == "forecast":
                forecast_data = await _get_daily_forecast(location_key)
                if not forecast_data:
                    return ToolResult(
                        structured_content={
                            "error": "No data",
                            "message": f"Could not retrieve forecast for {location_name}",
                        }
                    )
                return ToolResult(
                    structured_content={
                        "type": "daily_forecast",
                        "location": location_name,
                        "headline": forecast_data.get("Headline", {}).get("Text", ""),
                        "forecast": forecast_data.get("DailyForecasts", []),
                    }
                )

            else:
                return ToolResult(
                    structured_content={
                        "error": "Invalid weather_type",
                        "message": f"weather_type must be 'current' or 'forecast', got: {weather_type}",
                    }
                )

        except Exception as e:
            return ToolResult(
                structured_content={
                    "error": "API error",
                    "message": f"Failed to fetch weather data: {str(e)}",
                }
            )


mcp.add_tool(GetWeatherTool())


if __name__ == "__main__":
    sys.stderr.write("Starting Weather MCP Server with stdio transport\n")
    mcp.run(transport="stdio")
