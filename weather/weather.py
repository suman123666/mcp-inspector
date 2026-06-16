from typing import Any
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather", log_level="ERROR")

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"

WMO_CODES: dict[int, str] = {
    0: "晴天", 1: "基本晴朗", 2: "局部多云", 3: "阴天",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "中毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "冰粒",
    80: "小阵雨", 81: "中阵雨", 82: "大阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "冰雹雷暴", 99: "强冰雹雷暴",
}


async def fetch(params: dict) -> dict[str, Any] | None:
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None


@mcp.tool()
async def get_forecast(latitude: float, longitude: float) -> str:
    """Get 5-day weather forecast for a location.

    Args:
        latitude: Latitude of the location
        longitude: Longitude of the location
    """
    data = await fetch({
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,wind_speed_10m_max,precipitation_sum",
        "forecast_days": 5,
        "timezone": "auto",
    })
    if not data or "daily" not in data:
        return "Unable to fetch forecast data."

    d = data["daily"]
    lines = []
    for i in range(len(d["time"])):
        desc = WMO_CODES.get(d["weather_code"][i], f"code={d['weather_code'][i]}")
        lines.append(
            f"{d['time'][i]}: {desc}, "
            f"最高 {d['temperature_2m_max'][i]}°C / 最低 {d['temperature_2m_min'][i]}°C, "
            f"风速 {d['wind_speed_10m_max'][i]} km/h, 降水 {d['precipitation_sum'][i]} mm"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_current_weather(latitude: float, longitude: float) -> str:
    """Get current weather conditions for a location.

    Args:
        latitude: Latitude of the location
        longitude: Longitude of the location
    """
    data = await fetch({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,precipitation",
        "timezone": "auto",
    })
    if not data or "current" not in data:
        return "Unable to fetch current weather."

    c = data["current"]
    desc = WMO_CODES.get(c.get("weather_code", 0), f"code={c.get('weather_code')}")
    return (
        f"当前天气: {desc}\n"
        f"温度: {c.get('temperature_2m')}°C\n"
        f"湿度: {c.get('relative_humidity_2m')}%\n"
        f"风速: {c.get('wind_speed_10m')} km/h\n"
        f"降水: {c.get('precipitation')} mm"
    )


if __name__ == "__main__":
    mcp.run(transport='stdio')
