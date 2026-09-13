#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证禁飞区栅格化、约束缓存和 20 条任务的机队并行执行队列。"""
from __future__ import annotations

import importlib.util
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("shapefile") is None:
    stub = types.ModuleType("shapefile")
    stub.Reader = object
    stub.__spec__ = ModuleSpec("shapefile", loader=None)
    sys.modules["shapefile"] = stub

import planner
import simulator as simulator_module
from planner import Obstacle, clear_route_cache, plan_route
from tools.verify_safe_dispatch_recovery import new_system, route_ok, task_payload


class EmptyBuildingStore:
    available = True

    @staticmethod
    def buildings_at_point(*_args, **_kwargs):
        return []

    @staticmethod
    def query_route_bboxes(*_args, **_kwargs):
        return []


def verify_raster_and_cache() -> None:
    start = {"lon": 114.0000, "lat": 22.5350, "alt": 0.0}
    end = {"lon": 114.0300, "lat": 22.5350, "alt": 0.0}
    obstacle = Obstacle(
        kind="no_fly",
        name="TEST-NO-FLY",
        polygon=[
            (114.012, 22.528), (114.018, 22.528),
            (114.018, 22.542), (114.012, 22.542),
        ],
    )
    store = EmptyBuildingStore()
    original_raster = planner.RASTERIZE_CUSTOM_OBSTACLES
    try:
        planner.RASTERIZE_CUSTOM_OBSTACLES = False
        clear_route_cache()
        legacy = plan_route(start, end, 80, "shenzhen", [obstacle], store)
        planner.RASTERIZE_CUSTOM_OBSTACLES = True
        clear_route_cache()
        optimized = plan_route(start, end, 80, "shenzhen", [obstacle], store)
        assert legacy["planning_failed"] == optimized["planning_failed"] is False
        assert legacy["route"] == optimized["route"]

        cached = plan_route(start, end, 80, "shenzhen", [obstacle], store)
        assert cached["cache_hit"] is True
        changed = Obstacle(kind="no_fly", name="CHANGED", polygon=list(obstacle.polygon))
        uncached = plan_route(start, end, 80, "shenzhen", [changed], store)
        assert uncached["cache_hit"] is False
    finally:
        planner.RASTERIZE_CUSTOM_OBSTACLES = original_raster
        clear_route_cache()


def verify_twenty_task_queue() -> None:
    original_plan_route = simulator_module.plan_route
    original_weather = simulator_module.WEATHER_MONITOR.get_state
    try:
        simulator_module.plan_route = route_ok
        simulator_module.WEATHER_MONITOR.get_state = lambda: {
            "dispatch_allowed": True,
            "flight_action": "NORMAL",
        }
        system = new_system()
        system.init_fleet(6)
        system._log = lambda *_args: None
        for index in range(20):
            offset = index * 0.00005
            system.create_task(task_payload(
                {"lon": 114.0800 + offset, "lat": 22.5500},
                {"lon": 114.1000 + offset, "lat": 22.5600},
            ))

        system._assign_waiting()
        active = [task for task in system.tasks.values() if task["status"] == "ASSIGNED"]
        waiting = [task for task in system.tasks.values() if task["status"] == "WAITING"]
        assert len(active) == 6
        assert len(waiting) == 14
        assert sum(uav["state"] == "BUSY" for uav in system.uavs.values()) == 6

        released_task = active[0]
        released_uav = system.uavs[released_task["assigned_uav"]]
        released_task["status"] = "COMPLETED"
        released_uav.update({"state": "IDLE", "phase": "IDLE", "task_id": None, "route": []})
        system._assign_waiting()
        assert sum(task["status"] == "ASSIGNED" for task in system.tasks.values()) == 6
        assert sum(task["status"] == "WAITING" for task in system.tasks.values()) == 13

        full_system = new_system()
        full_system.init_fleet(20)
        full_system._log = lambda *_args: None
        # 基地扩展到全市后，让任务随各机所属基地分布，避免这个队列测试
        # 被“跨城起飞电量不足”这一独立安全约束干扰。
        fleet = list(full_system.uavs.values())
        for index, home_uav in enumerate(fleet):
            offset = (index // 10) * 0.00005
            full_system.create_task(task_payload(
                {"lon": home_uav["lon"] + 0.0002 + offset, "lat": home_uav["lat"] + 0.0002},
                {"lon": home_uav["lon"] + 0.0050 + offset, "lat": home_uav["lat"] + 0.0030},
            ))
        full_system._assign_waiting()
        assert sum(task["status"] == "ASSIGNED" for task in full_system.tasks.values()) == 20
        assert sum(uav["state"] == "BUSY" for uav in full_system.uavs.values()) == 20
    finally:
        simulator_module.plan_route = original_plan_route
        simulator_module.WEATHER_MONITOR.get_state = original_weather


if __name__ == "__main__":
    verify_raster_and_cache()
    verify_twenty_task_queue()
    print("Fast no-fly and 20-task dispatch verification passed.")
