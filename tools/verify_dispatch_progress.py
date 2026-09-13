#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证耗时调度在线程执行，并持续产生可读取的真实阶段进度。"""
from __future__ import annotations

import asyncio
import importlib.util
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("shapefile") is None:
    stub = types.ModuleType("shapefile")
    stub.Reader = object
    stub.__spec__ = ModuleSpec("shapefile", loader=None)
    sys.modules["shapefile"] = stub

import simulator as simulator_module
from tools.verify_safe_dispatch_recovery import new_system, route_ok, task_payload


def slow_route(start, end, cruise_alt, *args, progress_callback=None, **kwargs):
    for fraction, stage in ((0.05, "检查端点"), (0.35, "读取建筑"), (0.65, "验证航层"), (1.0, "完成航段")):
        if progress_callback:
            progress_callback(fraction, stage)
        time.sleep(0.025)
    return route_ok(start, end, cruise_alt, *args, **kwargs)


async def verify() -> None:
    original_plan_route = simulator_module.plan_route
    original_weather = simulator_module.WEATHER_MONITOR.get_state
    try:
        simulator_module.plan_route = slow_route
        simulator_module.WEATHER_MONITOR.get_state = lambda: {
            "dispatch_allowed": True,
            "flight_action": "NORMAL",
        }
        system = new_system()
        system._log = lambda *_args: None
        task = system.create_task(task_payload(
            {"lon": 114.08, "lat": 22.55},
            {"lon": 114.10, "lat": 22.56},
        ))

        worker = asyncio.create_task(system._run_dispatch_cycle())
        observed = []
        responsive_ticks = 0
        while not worker.done():
            snapshot = system.get_dispatch_progress()
            current = snapshot.get("current") or {}
            observed.append(float(current.get("percent") or 0.0))
            responsive_ticks += 1
            await asyncio.sleep(0.01)
        await worker

        final = system.get_dispatch_progress()["current"]
        assert responsive_ticks >= 5, "调度计算期间事件循环没有保持响应"
        assert len({value for value in observed if 0 < value < 100}) >= 4, observed
        assert final["percent"] == 100.0 and final["stage"] == "调度完成", final
        assert task["status"] == "ASSIGNED"
    finally:
        simulator_module.plan_route = original_plan_route
        simulator_module.WEATHER_MONITOR.get_state = original_weather


if __name__ == "__main__":
    asyncio.run(verify())
    print("Dispatch progress verification passed.")
