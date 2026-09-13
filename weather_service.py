"""Open-Meteo geocoding and forecast client with small in-memory caches."""

from __future__ import annotations

import json
import socket
from threading import Lock
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_CODES = {
    0: "晴朗", 1: "大部晴朗", 2: "局部多云", 3: "阴天",
    45: "有雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨",
    56: "轻微冻毛毛雨", 57: "较强冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "轻微冻雨", 67: "较强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "米雪", 80: "小阵雨", 81: "中阵雨", 82: "强阵雨", 85: "小阵雪",
    86: "强阵雪", 95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴强冰雹",
}


class OpenMeteoWeather:
    def __init__(
        self, timeout: float = 15.0, opener: Callable[..., Any] = urlopen,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout = timeout
        self._opener = opener
        self._clock = clock
        self._lock = Lock()
        self._geocode_cache: dict[str, tuple[float, dict]] = {}
        self._forecast_cache: dict[tuple, tuple[float, dict]] = {}

    def forecast(self, location: str, days: int = 3, include_hourly: bool = False) -> dict:
        location = location.strip()
        if not location:
            raise ValueError("天气查询地点不能为空。")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
            raise ValueError("days 必须在 1 到 7 之间。")
        if not isinstance(include_hourly, bool):
            raise ValueError("include_hourly 必须是 boolean。")
        place = self._geocode(location)
        key = (round(place["latitude"], 4), round(place["longitude"], 4), days, include_hourly)
        cached = self._cache_get(self._forecast_cache, key, 300)
        if cached is not None:
            return cached

        params = {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "timezone": "auto", "forecast_days": days,
            "temperature_unit": "celsius", "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "current": ",".join([
                "temperature_2m", "apparent_temperature", "relative_humidity_2m",
                "precipitation", "rain", "weather_code", "cloud_cover",
                "pressure_msl", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            ]),
            "daily": ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "apparent_temperature_max", "apparent_temperature_min",
                "precipitation_sum", "precipitation_probability_max", "sunrise", "sunset",
                "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant",
            ]),
        }
        if include_hourly:
            params["hourly"] = ",".join([
                "temperature_2m", "apparent_temperature", "precipitation_probability",
                "precipitation", "weather_code", "wind_speed_10m",
            ])
        payload = self._get_json(FORECAST_URL, params, "Open-Meteo 天气预报")
        result = self._normalize(place, payload, include_hourly)
        self._cache_set(self._forecast_cache, key, result)
        return result

    def _geocode(self, query: str) -> dict:
        key = query.casefold()
        cached = self._cache_get(self._geocode_cache, key, 86_400)
        if cached is not None:
            return cached
        payload = self._get_json(
            GEOCODING_URL,
            {"name": query, "count": 5, "language": "zh", "format": "json"},
            "Open-Meteo 地理编码",
        )
        candidates = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"未找到天气查询地点：{query}")
        item = candidates[0]
        place = {
            "query": query, "name": item.get("name"), "country": item.get("country"),
            "admin1": item.get("admin1"), "latitude": item.get("latitude"),
            "longitude": item.get("longitude"), "timezone": item.get("timezone"),
        }
        if not isinstance(place["latitude"], (int, float)) or not isinstance(place["longitude"], (int, float)):
            raise RuntimeError("Open-Meteo 地理编码缺少有效经纬度。")
        self._cache_set(self._geocode_cache, key, place)
        return place

    def _normalize(self, place: dict, payload: dict, include_hourly: bool) -> dict:
        current = dict(payload.get("current") or {})
        current["weather_text"] = WEATHER_CODES.get(current.get("weather_code"), "未知天气")
        daily_raw = payload.get("daily") or {}
        dates = daily_raw.get("time") or []
        daily = []
        for index, date in enumerate(dates[:7]):
            item = {"date": date}
            for name, values in daily_raw.items():
                if name == "time" or not isinstance(values, list) or index >= len(values):
                    continue
                item[name] = values[index]
            item["weather_text"] = WEATHER_CODES.get(item.get("weather_code"), "未知天气")
            daily.append(item)
        hourly = []
        if include_hourly:
            raw = payload.get("hourly") or {}
            times = raw.get("time") or []
            for index, timestamp in enumerate(times[:48]):
                item = {"time": timestamp}
                for name, values in raw.items():
                    if name == "time" or not isinstance(values, list) or index >= len(values):
                        continue
                    item[name] = values[index]
                item["weather_text"] = WEATHER_CODES.get(item.get("weather_code"), "未知天气")
                hourly.append(item)
        return {
            "provider": "Open-Meteo", "location": place,
            "timezone": payload.get("timezone"), "timezone_abbreviation": payload.get("timezone_abbreviation"),
            "utc_offset_seconds": payload.get("utc_offset_seconds"),
            "current": current, "daily": daily, "hourly": hourly,
            "note": "天气预报不等同于官方灾害预警；台风和预警信息请查询气象部门。",
        }

    def _get_json(self, base_url: str, params: dict, label: str) -> dict:
        request = Request(
            base_url + "?" + urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "SJTUClaw/1.0"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"{label}失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"{label}网络连接失败或超时，请稍后重试。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label}返回了无法解析的响应。") from exc
        if not isinstance(payload, dict) or payload.get("error") is True:
            reason = payload.get("reason") if isinstance(payload, dict) else None
            raise RuntimeError(f"{label}返回异常：{reason or '未知错误'}")
        return payload

    def _cache_get(self, cache: dict, key, ttl: float):
        with self._lock:
            item = cache.get(key)
            if item and self._clock() - item[0] < ttl:
                return item[1]
        return None

    def _cache_set(self, cache: dict, key, value) -> None:
        with self._lock:
            cache[key] = (self._clock(), value)
