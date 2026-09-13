#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳真实/模拟天气监控及无人机派遣安全判定。"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Awaitable, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_config import AMAP_WEB_SERVICE_KEY
from weather_config import (
    INFLIGHT_EMERGENCY_WIND_LEVEL,
    INFLIGHT_SEVERE_WEATHER_KEYWORDS,
    INFLIGHT_WEATHER_RESPONSE_ENABLED,
    MAX_SAFE_TEMPERATURE_C,
    MAX_SAFE_WIND_LEVEL,
    MIN_SAFE_TEMPERATURE_C,
    PAUSE_DISPATCH_IF_WEATHER_UNAVAILABLE,
    SIMULATED_HUMIDITY_PERCENT,
    SIMULATED_TEMPERATURE_C,
    SIMULATED_WEATHER,
    SIMULATED_WIND_DIRECTION,
    SIMULATED_WIND_POWER,
    UNSAFE_WEATHER_KEYWORDS,
    USE_REAL_WEATHER,
    WEATHER_API_KEY,
    WEATHER_CITY_ADCODE,
    WEATHER_CITY_NAME,
    WEATHER_ENABLED,
    WEATHER_REFRESH_SECONDS,
    WEATHER_REQUEST_TIMEOUT_SECONDS,
    WEATHER_STALE_AFTER_SECONDS,
)


AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"


class WeatherServiceError(RuntimeError):
    pass


def parse_wind_level(value: object) -> Optional[float]:
    """从“≤3”“3-4”“5”等高德风力文本中提取最大等级。"""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    return max((float(number) for number in numbers), default=None)


def parse_number(value: object) -> Optional[float]:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def evaluate_flight_weather(weather: str, wind_level: Optional[float], temperature_c: Optional[float]) -> tuple[bool, str]:
    reasons = []
    matched = next((keyword for keyword in UNSAFE_WEATHER_KEYWORDS if keyword in weather), None)
    if matched:
        reasons.append(f"天气包含“{matched}”")
    if wind_level is not None and wind_level > float(MAX_SAFE_WIND_LEVEL):
        reasons.append(f"风力 {wind_level:g} 级超过安全上限 {float(MAX_SAFE_WIND_LEVEL):g} 级")
    if temperature_c is not None and temperature_c < float(MIN_SAFE_TEMPERATURE_C):
        reasons.append(f"气温 {temperature_c:g}℃低于安全下限 {float(MIN_SAFE_TEMPERATURE_C):g}℃")
    if temperature_c is not None and temperature_c > float(MAX_SAFE_TEMPERATURE_C):
        reasons.append(f"气温 {temperature_c:g}℃超过安全上限 {float(MAX_SAFE_TEMPERATURE_C):g}℃")
    if reasons:
        return False, "；".join(reasons)
    return True, "天气满足当前派遣安全阈值"


def evaluate_inflight_weather(
    weather: str,
    wind_level: Optional[float],
    temperature_c: Optional[float],
    dispatch_allowed: bool,
) -> tuple[str, str]:
    """返回在途处置等级：NORMAL、CONTINUE_CAUTION、RECOVER 或 MONITOR_ONLY。"""
    if not INFLIGHT_WEATHER_RESPONSE_ENABLED:
        return "MONITOR_ONLY", "在途天气应急联动已关闭"

    severe_reasons = []
    matched = next((keyword for keyword in INFLIGHT_SEVERE_WEATHER_KEYWORDS if keyword in weather), None)
    if matched:
        severe_reasons.append(f"天气包含“{matched}”")
    if wind_level is not None and wind_level >= float(INFLIGHT_EMERGENCY_WIND_LEVEL):
        severe_reasons.append(
            f"风力 {wind_level:g} 级达到在途撤离阈值 {float(INFLIGHT_EMERGENCY_WIND_LEVEL):g} 级"
        )
    if temperature_c is not None and temperature_c < float(MIN_SAFE_TEMPERATURE_C):
        severe_reasons.append(f"气温 {temperature_c:g}℃低于安全下限 {float(MIN_SAFE_TEMPERATURE_C):g}℃")
    if temperature_c is not None and temperature_c > float(MAX_SAFE_TEMPERATURE_C):
        severe_reasons.append(f"气温 {temperature_c:g}℃超过安全上限 {float(MAX_SAFE_TEMPERATURE_C):g}℃")
    if severe_reasons:
        return "RECOVER", "；".join(severe_reasons)
    if not dispatch_allowed:
        return "CONTINUE_CAUTION", "天气不适合新派遣，在途无人机减速并重新核算电量"
    return "NORMAL", "在途飞行条件正常"


class WeatherMonitor:
    def __init__(self, api_key: Optional[str] = None):
        configured_key = WEATHER_API_KEY.strip() or AMAP_WEB_SERVICE_KEY.strip()
        self._api_key = configured_key if api_key is None else api_key.strip()
        self._simulated_override: Optional[Dict] = None
        self._state_lock = Lock()
        self._refresh_lock = Lock()
        self._state: Dict = (
            self._simulated_state()
            if WEATHER_ENABLED and not USE_REAL_WEATHER
            else self._unavailable_state("等待首次天气更新" if self._api_key else "未配置高德天气 Web 服务 Key")
        )

    def _unavailable_state(self, error: str) -> Dict:
        dispatch_allowed = not bool(PAUSE_DISPATCH_IF_WEATHER_UNAVAILABLE)
        return {
            "enabled": bool(WEATHER_ENABLED),
            "provider": "高德天气",
            "source_mode": "real",
            "simulated": False,
            "configured": bool(self._api_key),
            "available": False,
            "api_ok": False,
            "stale": True,
            "city": WEATHER_CITY_NAME,
            "adcode": WEATHER_CITY_ADCODE,
            "weather": "",
            "temperature_c": None,
            "humidity_percent": None,
            "wind_direction": "",
            "wind_power": "",
            "wind_level": None,
            "report_time": None,
            "fetched_at": None,
            "last_success_epoch": None,
            "dispatch_allowed": dispatch_allowed,
            "dispatch_status": "天气数据不可用，允许派遣" if dispatch_allowed else "天气数据不可用，暂停新派遣",
            "pause_reason": "" if dispatch_allowed else error,
            "flight_action": "MONITOR_ONLY",
            "flight_action_reason": "天气数据不可用，仅暂停新派遣，不主动改变在途航线",
            "error": error,
        }

    def _simulated_state(self) -> Dict:
        override = self._simulated_override or {}
        weather = str(override.get("weather", SIMULATED_WEATHER) or "").strip() or "未知"
        temperature = parse_number(override.get("temperature_c", SIMULATED_TEMPERATURE_C))
        humidity = parse_number(override.get("humidity_percent", SIMULATED_HUMIDITY_PERCENT))
        wind_power = str(override.get("wind_power", SIMULATED_WIND_POWER) or "").strip()
        wind_level = parse_wind_level(wind_power)
        allowed, reason = evaluate_flight_weather(weather, wind_level, temperature)
        flight_action, flight_reason = evaluate_inflight_weather(weather, wind_level, temperature, allowed)
        now = datetime.now().astimezone()
        return {
            "enabled": True,
            "provider": "手动模拟天气",
            "source_mode": "simulated",
            "simulated": True,
            "configured": True,
            "available": True,
            "api_ok": True,
            "stale": False,
            "city": WEATHER_CITY_NAME,
            "adcode": WEATHER_CITY_ADCODE,
            "weather": weather,
            "temperature_c": temperature,
            "humidity_percent": humidity,
            "wind_direction": str(override.get("wind_direction", SIMULATED_WIND_DIRECTION) or "").strip(),
            "wind_power": wind_power,
            "wind_level": wind_level,
            "report_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_at": now.astimezone(timezone.utc).isoformat(),
            "last_success_epoch": time.time(),
            "dispatch_allowed": allowed,
            "dispatch_status": "模拟天气适航，允许新派遣" if allowed else "模拟恶劣天气，暂停新派遣",
            "pause_reason": "" if allowed else reason,
            "flight_action": flight_action,
            "flight_action_reason": flight_reason,
            "error": "",
        }

    def set_simulated_weather(
        self,
        weather: str,
        temperature_c: float,
        humidity_percent: float,
        wind_direction: str,
        wind_power: str,
    ) -> Dict:
        """运行中覆盖模拟天气；仅保存在内存，服务重启后恢复配置文件值。"""
        if not WEATHER_ENABLED:
            raise WeatherServiceError("天气联动已关闭，不能设置运行时模拟天气")
        if USE_REAL_WEATHER:
            raise WeatherServiceError("当前正在使用真实天气；请先在 weather_config.py 中关闭真实天气")
        override = {
            "weather": str(weather or "").strip() or "未知",
            "temperature_c": float(temperature_c),
            "humidity_percent": max(0.0, min(100.0, float(humidity_percent))),
            "wind_direction": str(wind_direction or "").strip(),
            "wind_power": str(wind_power or "").strip(),
        }
        with self._refresh_lock:
            self._simulated_override = override
            with self._state_lock:
                self._state = self._simulated_state()
        state = self.get_state()
        print(
            f"[WEATHER] RUNTIME_SIMULATION weather={state['weather']!r} wind={state['wind_power']!r} "
            f"flight_action={state['flight_action']}",
            flush=True,
        )
        return state

    def get_state(self) -> Dict:
        with self._state_lock:
            state = dict(self._state)
        if not state["enabled"]:
            state.update({
                "dispatch_allowed": True,
                "dispatch_status": "天气联动已关闭",
                "pause_reason": "",
                "flight_action": "NORMAL",
                "flight_action_reason": "天气联动已关闭，不改变在途航线",
                "stale": False,
            })
        elif state.get("source_mode") == "simulated":
            state["stale"] = False
        else:
            last_success = state.get("last_success_epoch")
            stale = last_success is None or time.time() - float(last_success) > float(WEATHER_STALE_AFTER_SECONDS)
            state["stale"] = stale
            if stale and PAUSE_DISPATCH_IF_WEATHER_UNAVAILABLE:
                state["dispatch_allowed"] = False
                state["dispatch_status"] = "天气数据已过期，暂停新派遣" if last_success else "等待天气数据，暂停新派遣"
                state["pause_reason"] = state.get("error") or "没有可用的实时天气数据"
        state.pop("last_success_epoch", None)
        return state

    def refresh(self) -> Dict:
        if not WEATHER_ENABLED:
            with self._state_lock:
                self._state = self._unavailable_state("天气联动已关闭")
                self._state["enabled"] = False
            return self.get_state()
        if not USE_REAL_WEATHER:
            with self._refresh_lock:
                with self._state_lock:
                    self._state = self._simulated_state()
                state = self.get_state()
            print(
                f"[WEATHER] SIMULATED weather={state['weather']!r} wind={state['wind_power']!r} "
                f"dispatch_allowed={state['dispatch_allowed']}",
                flush=True,
            )
            return state
        if not self._api_key:
            with self._state_lock:
                self._state = self._unavailable_state("未配置高德天气 Web 服务 Key")
            return self.get_state()

        with self._refresh_lock:
            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                live = self._fetch_live_weather()
                weather = str(live.get("weather") or "").strip()
                temperature = parse_number(live.get("temperature"))
                humidity = parse_number(live.get("humidity"))
                wind_power = str(live.get("windpower") or "").strip()
                wind_level = parse_wind_level(wind_power)
                allowed, reason = evaluate_flight_weather(weather, wind_level, temperature)
                flight_action, flight_reason = evaluate_inflight_weather(weather, wind_level, temperature, allowed)
                state = {
                    "enabled": True,
                    "provider": "高德天气",
                    "source_mode": "real",
                    "simulated": False,
                    "configured": True,
                    "available": True,
                    "api_ok": True,
                    "stale": False,
                    "city": str(live.get("city") or WEATHER_CITY_NAME),
                    "adcode": str(live.get("adcode") or WEATHER_CITY_ADCODE),
                    "weather": weather or "未知",
                    "temperature_c": temperature,
                    "humidity_percent": humidity,
                    "wind_direction": str(live.get("winddirection") or "").strip(),
                    "wind_power": wind_power,
                    "wind_level": wind_level,
                    "report_time": str(live.get("reporttime") or "").strip() or None,
                    "fetched_at": checked_at,
                    "last_success_epoch": time.time(),
                    "dispatch_allowed": allowed,
                    "dispatch_status": "天气适航，允许新派遣" if allowed else "恶劣天气，暂停新派遣",
                    "pause_reason": "" if allowed else reason,
                    "flight_action": flight_action,
                    "flight_action_reason": flight_reason,
                    "error": "",
                }
                with self._state_lock:
                    self._state = state
                print(
                    f"[WEATHER] SUCCESS city={state['city']!r} weather={weather!r} "
                    f"wind={wind_power!r} dispatch_allowed={allowed}",
                    flush=True,
                )
            except WeatherServiceError as exc:
                with self._state_lock:
                    previous = dict(self._state)
                    if previous.get("available"):
                        previous.update({"api_ok": False, "fetched_at": checked_at, "error": str(exc)})
                        self._state = previous
                    else:
                        self._state = self._unavailable_state(str(exc))
                        self._state["fetched_at"] = checked_at
                print(f"[WEATHER] FAILED error={str(exc)!r}", flush=True)
            return self.get_state()

    def _fetch_live_weather(self) -> Dict:
        params = {
            "key": self._api_key,
            "city": WEATHER_CITY_ADCODE,
            "extensions": "base",
            "output": "json",
        }
        request = Request(
            f"{AMAP_WEATHER_URL}?{urlencode(params)}",
            headers={"User-Agent": "Shenzhen-UAV-Dispatch/1.0"},
        )
        try:
            with urlopen(request, timeout=float(WEATHER_REQUEST_TIMEOUT_SECONDS)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise WeatherServiceError(f"高德天气 HTTP 请求失败：HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", None)
            raise WeatherServiceError(f"高德天气连接失败：{reason or exc}") from exc
        if payload.get("status") != "1":
            info = payload.get("info") or "未知错误"
            infocode = payload.get("infocode") or ""
            raise WeatherServiceError(f"高德天气接口拒绝请求：{info}{f' ({infocode})' if infocode else ''}")
        lives = payload.get("lives") or []
        if not lives:
            raise WeatherServiceError("高德天气没有返回深圳实况数据")
        return dict(lives[0])

    async def run(self, on_update: Optional[Callable[[], Optional[Awaitable]]] = None):
        while True:
            await asyncio.to_thread(self.refresh)
            if on_update:
                result = on_update()
                if inspect.isawaitable(result):
                    await result
            await asyncio.sleep(max(10.0, float(WEATHER_REFRESH_SECONDS)))


WEATHER_MONITOR = WeatherMonitor()
