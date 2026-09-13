"""验证 Open-Meteo 地理编码与天气结果整理。"""

import json
import unittest
from urllib.parse import parse_qs, urlparse

from tools import create_read_only_registry
from weather_service import OpenMeteoWeather


class _Response:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.data


class WeatherServiceTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def opener(request, timeout):
            self.requests.append((request.full_url, timeout))
            if "geocoding-api" in request.full_url:
                return _Response({"results": [{
                    "name": "上海", "country": "中国", "admin1": "上海",
                    "latitude": 31.2222, "longitude": 121.4581,
                    "timezone": "Asia/Shanghai",
                }]})
            hourly_times = [f"2026-07-{8 + i // 24:02d}T{i % 24:02d}:00" for i in range(72)]
            return _Response({
                "timezone": "Asia/Shanghai", "timezone_abbreviation": "GMT+8",
                "utc_offset_seconds": 28800,
                "current": {"time": "2026-07-08T10:00", "temperature_2m": 31.2,
                            "weather_code": 1, "wind_speed_10m": 8.0},
                "daily": {
                    "time": ["2026-07-08", "2026-07-09"],
                    "weather_code": [1, 61],
                    "temperature_2m_max": [35.0, 32.0],
                    "temperature_2m_min": [27.0, 26.0],
                },
                "hourly": {
                    "time": hourly_times,
                    "temperature_2m": [30.0] * 72,
                    "weather_code": [0] * 72,
                },
            })

        self.service = OpenMeteoWeather(opener=opener, clock=lambda: 1000.0)

    def test_forecast_geocodes_normalizes_and_bounds_hourly(self):
        result = self.service.forecast(" 上海 ", days=2, include_hourly=True)
        self.assertEqual(result["location"]["name"], "上海")
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["current"]["weather_text"], "大部晴朗")
        self.assertEqual(result["daily"][1]["weather_text"], "小雨")
        self.assertEqual(len(result["hourly"]), 48)
        query = parse_qs(urlparse(self.requests[1][0]).query)
        self.assertEqual(query["timezone"], ["auto"])
        self.assertEqual(query["forecast_days"], ["2"])
        self.assertEqual(query["temperature_unit"], ["celsius"])

    def test_forecast_cache_avoids_duplicate_requests(self):
        first = self.service.forecast("上海")
        second = self.service.forecast("上海")
        self.assertIs(first, second)
        self.assertEqual(len(self.requests), 2)

    def test_validation_and_unknown_location(self):
        with self.assertRaisesRegex(ValueError, "地点不能为空"):
            self.service.forecast(" ")
        with self.assertRaisesRegex(ValueError, "1 到 7"):
            self.service.forecast("上海", days=8)
        empty = OpenMeteoWeather(opener=lambda *_args, **_kwargs: _Response({"results": []}))
        with self.assertRaisesRegex(ValueError, "未找到"):
            empty.forecast("不存在地点")

    def test_registry_exposes_read_only_weather_tool(self):
        registry = create_read_only_registry(weather_handler=lambda **kwargs: kwargs)
        tool = registry.get("weather_forecast")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "read_only")
        result = registry.execute("weather_forecast", {"location": "上海", "days": 3})
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
