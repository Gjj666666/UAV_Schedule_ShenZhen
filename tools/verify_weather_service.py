#!/usr/bin/env python3
"""离线验证天气解析、安全门控和故障保留逻辑；不会调用真实天气 API。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weather_service import WeatherMonitor, evaluate_inflight_weather, parse_wind_level


def response_for(weather: str, windpower: str = "≤3", temperature: str = "27") -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "status": "1",
        "infocode": "10000",
        "lives": [{
            "province": "广东",
            "city": "深圳市",
            "adcode": "440300",
            "weather": weather,
            "temperature": temperature,
            "winddirection": "东南",
            "windpower": windpower,
            "humidity": "70",
            "reporttime": "2026-09-09 12:00:00",
        }],
    }, ensure_ascii=False).encode("utf-8")
    return response


def main() -> None:
    assert parse_wind_level("≤3") == 3.0
    assert parse_wind_level("4-6") == 6.0

    # 项目配置可切换为模拟模式；这一段显式启用真实模式，仅验证解析且仍使用 Mock。
    live_mode = patch("weather_service.USE_REAL_WEATHER", True)
    live_mode.start()
    monitor = WeatherMonitor(api_key="test-weather-key")
    with patch("weather_service.urlopen", return_value=response_for("晴")):
        sunny = monitor.refresh()
    assert sunny["available"] is True
    assert sunny["dispatch_allowed"] is True
    assert sunny["flight_action"] == "NORMAL"
    assert sunny["temperature_c"] == 27.0

    with patch("weather_service.urlopen", return_value=response_for("雷阵雨")):
        storm = monitor.refresh()
    assert storm["dispatch_allowed"] is False
    assert storm["flight_action"] == "RECOVER"
    assert "天气包含" in storm["pause_reason"]

    with patch("weather_service.urlopen", return_value=response_for("晴", "6-7")):
        windy = monitor.refresh()
    assert windy["dispatch_allowed"] is False
    assert windy["flight_action"] == "RECOVER"
    assert "风力" in windy["pause_reason"]

    with patch("weather_service.urlopen", return_value=response_for("晴")):
        monitor.refresh()
    with patch("weather_service.urlopen", side_effect=URLError("offline-test")):
        retained = monitor.refresh()
    assert retained["available"] is True
    assert retained["api_ok"] is False
    assert retained["dispatch_allowed"] is True
    assert "offline-test" in retained["error"]

    unavailable = WeatherMonitor(api_key="").get_state()
    assert unavailable["available"] is False
    assert unavailable["dispatch_allowed"] is False
    assert unavailable["flight_action"] == "MONITOR_ONLY"

    caution_action, _ = evaluate_inflight_weather("小雨", 3.0, 27.0, False)
    assert caution_action == "CONTINUE_CAUTION"
    live_mode.stop()

    with (
        patch("weather_service.USE_REAL_WEATHER", False),
        patch("weather_service.SIMULATED_WEATHER", "雷阵雨"),
        patch("weather_service.SIMULATED_TEMPERATURE_C", 28.0),
        patch("weather_service.SIMULATED_HUMIDITY_PERCENT", 88.0),
        patch("weather_service.SIMULATED_WIND_DIRECTION", "南"),
        patch("weather_service.SIMULATED_WIND_POWER", "4"),
        patch("weather_service.urlopen") as mocked_urlopen,
    ):
        simulated_monitor = WeatherMonitor(api_key="")
        simulated = simulated_monitor.refresh()
    assert simulated["source_mode"] == "simulated"
    assert simulated["provider"] == "手动模拟天气"
    assert simulated["dispatch_allowed"] is False
    assert simulated["flight_action"] == "RECOVER"
    assert simulated["weather"] == "雷阵雨"
    mocked_urlopen.assert_not_called()

    runtime_caution = simulated_monitor.set_simulated_weather("小雨", 25.0, 90.0, "东", "3")
    assert runtime_caution["flight_action"] == "CONTINUE_CAUTION"
    runtime_clear = simulated_monitor.set_simulated_weather("晴", 27.0, 65.0, "东南", "≤3")
    assert runtime_clear["dispatch_allowed"] is True
    assert runtime_clear["flight_action"] == "NORMAL"
    print("Weather service verification passed.")


if __name__ == "__main__":
    main()
