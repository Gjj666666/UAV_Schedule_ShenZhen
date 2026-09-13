#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证在途天气降速、未取货返航和携货交接，不调用天气 API。"""
from __future__ import annotations

import importlib.util
from importlib.machinery import ModuleSpec
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("shapefile") is None:
    shapefile_stub = types.ModuleType("shapefile")
    shapefile_stub.Reader = object
    shapefile_stub.__spec__ = ModuleSpec("shapefile", loader=None)
    sys.modules["shapefile"] = shapefile_stub

import simulator as simulator_module
from tools.verify_safe_dispatch_recovery import new_system, route_ok, task_payload


def weather(action: str, reason: str) -> dict:
    return {
        "flight_action": action,
        "flight_action_reason": reason,
        "weather": "小雨" if action == "CONTINUE_CAUTION" else "雷暴",
        "wind_power": "3" if action == "CONTINUE_CAUTION" else "6",
        "temperature_c": 27.0,
        "dispatch_allowed": action == "NORMAL",
    }


def main() -> None:
    original_plan_route = simulator_module.plan_route
    original_weather = simulator_module.WEATHER_MONITOR.get_state
    try:
        simulator_module.plan_route = route_ok

        system = new_system()
        task = system.create_task(task_payload(
            {"lon": 114.08, "lat": 22.55},
            {"lon": 114.10, "lat": 22.56},
        ))
        simulator_module.WEATHER_MONITOR.get_state = lambda: weather("NORMAL", "正常")
        system._assign_waiting()
        uav = system.uavs[task["assigned_uav"]]

        simulator_module.WEATHER_MONITOR.get_state = lambda: weather("CONTINUE_CAUTION", "小雨谨慎飞行")
        system._apply_inflight_weather_policy()
        assert task["status"] == "ASSIGNED"
        assert uav["weather_flight_factor"] < 1.0
        assert "降速" in task["message"]

        simulator_module.WEATHER_MONITOR.get_state = lambda: weather("RECOVER", "雷暴达到撤离阈值")
        system._apply_inflight_weather_policy()
        assert task["status"] == "WAITING_BLOCKED"
        assert uav["task_id"] is None

        cargo_system = new_system()
        cargo_task = cargo_system.create_task(task_payload(
            {"lon": 114.08, "lat": 22.55},
            {"lon": 114.12, "lat": 22.57},
        ))
        simulator_module.WEATHER_MONITOR.get_state = lambda: weather("NORMAL", "正常")
        cargo_system._assign_waiting()
        cargo_uav = cargo_system.uavs[cargo_task["assigned_uav"]]
        cargo_uav.update({"phase": "DELIVERING", "lon": 114.09, "lat": 22.555, "alt": 80.0})
        cargo_task.update({"status": "IN_PROGRESS", "cargo_status": "IN_TRANSIT"})

        simulator_module.WEATHER_MONITOR.get_state = lambda: weather("RECOVER", "雷暴达到撤离阈值")
        cargo_system._apply_inflight_weather_policy()
        assert cargo_task["status"] == "CARGO_RECOVERY"
        assert cargo_task["cargo_status"] == "IN_RECOVERY"
        assert cargo_uav["phase"] == "TO_CARGO_HANDOVER"
        assert cargo_uav["handover_target"]
    finally:
        simulator_module.plan_route = original_plan_route
        simulator_module.WEATHER_MONITOR.get_state = original_weather

    print("In-flight weather response verification passed.")


if __name__ == "__main__":
    main()
