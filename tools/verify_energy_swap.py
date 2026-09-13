#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证能耗模型、30个站点、多站换电计划和换电状态机。"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from building_store import BUILDING_STORE
from energy_config import BATTERY_STATIONS, BATTERY_SWAP_TIME_SECONDS
from energy_model import movement_energy_percent
from planner import plan_route
from simulator import DispatchSystem


def main():
    assert len(BATTERY_STATIONS) == 30
    assert len({station["id"] for station in BATTERY_STATIONS}) == 30
    occupied = [
        station["id"] for station in BATTERY_STATIONS
        if BUILDING_STORE.buildings_at_point(station["lon"], station["lat"])
    ]
    assert not occupied, f"换电站落入建筑轮廓：{occupied}"

    normal = movement_energy_percent(1000, 20, payload_kg=0, wind_factor=1.0)
    loaded = movement_energy_percent(1000, 20, payload_kg=2, wind_factor=1.0)
    headwind = movement_energy_percent(1000, 20, payload_kg=2, wind_factor=0.65)
    assert 0 < normal < loaded < headwind

    system = DispatchSystem.__new__(DispatchSystem)
    system.obstacles = []
    # 本脚本只验证能耗/换电状态机；空域限制由 verify_airspace_zones.py 独立验证。
    # 避免测试起点附近后来新增的硬禁飞示意区干扰换电模型回归。
    system._planning_obstacles = lambda: []
    system.speed_factor = 10.0
    system._event = lambda *args: None
    start = {"lon": 113.8980, "lat": 22.5310, "alt": 25.0}
    target = {"lon": 114.4795, "lat": 22.5968}
    direct = plan_route(start, target, 80, "shenzhen", [], BUILDING_STORE)
    assert not direct.get("planning_failed")
    plan = system._plan_energy_aware_leg(start, target, direct, 19.0, 2.0, 1.0, 80, "shenzhen")
    assert plan, "跨深圳低电量任务未找到多站换电计划"
    stops = plan["display_result"]["swap_stops"]
    assert len(stops) >= 2
    assert len(plan["actions"]) == len(stops) + 1

    task = {"id": "TASK-SWAP-TEST", "swap_count": 0, "message": ""}
    uav = {"id": "UAV-TEST", "battery": 19.0, "swap_count": 0}
    system._activate_leg_plan(uav, plan, "DELIVERING")
    completed_swaps = 0
    while uav["phase"] == "TO_SWAP":
        station = uav["swap_station"]
        assert uav["route"][-1]["alt"] == 0.0
        uav.update({"lon": station["lon"], "lat": station["lat"], "alt": 0.0})
        system._begin_battery_swap(uav, task)
        assert uav["phase"] == "SWAPPING"
        system._advance_battery_swap(uav, task, BATTERY_SWAP_TIME_SECONDS / system.speed_factor + 0.1)
        completed_swaps += 1
    assert uav["phase"] == "DELIVERING"
    assert completed_swaps == len(stops)
    assert task["swap_count"] == len(stops)
    assert uav["battery"] == 100.0

    print(f"PASS: 30个站点有效；跨深圳低电量航段规划 {len(stops)} 次换电。")
    print("PASS: 距离/升降/载重/风况能耗与多站换电状态机正常。")


if __name__ == "__main__":
    main()
