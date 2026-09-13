#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清晰、可解释的任务调度器。"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from energy_config import (
    ALLOW_BASE_CHARGE_INTERRUPTION,
    BATTERY_RESERVE_PERCENT,
    MIN_DISPATCH_BATTERY_PERCENT,
)
from energy_model import geodesic_energy_percent
from planner import haversine_m

PRIORITY_WEIGHT = {"LOW": 1, "NORMAL": 2, "HIGH": 3, "EMERGENCY": 4}


def task_pickup_point(task: Dict) -> Dict:
    """接驳任务从货物实际交接点取货，普通任务仍使用原始取货点。"""
    if task.get("cargo_status") == "WAITING_HANDOVER":
        point = task.get("cargo_location") or task.get("handover_station")
        if point and "lon" in point and "lat" in point:
            return point
    return task["origin"]


def task_sort_key(task: Dict):
    # 优先级越高越先；相同优先级先到先服务。
    return (-PRIORITY_WEIGHT.get(task.get("priority", "NORMAL"), 2), task.get("created_at", 0.0))


def estimate_required_battery(uav: Dict, task: Dict) -> float:
    """调度初筛：距离、载重和当前风况；实际 A* 航线在分配时再次精算。"""
    wind = float(uav.get("wind_factor", 1.0))
    pickup = task_pickup_point(task)
    to_pickup = geodesic_energy_percent(uav, pickup, 0.0, wind)
    delivery = geodesic_energy_percent(pickup, task["destination"], task["payload_kg"], wind)
    return to_pickup + delivery + BATTERY_RESERVE_PERCENT


def rank_uavs(task: Dict, uavs: Iterable[Dict]) -> List[Dict]:
    """返回按调度代价排序的全部候选机，供后端逐架验证完整安全航线。"""
    candidates: List[tuple] = []
    for uav in uavs:
        if uav.get("id") in set(task.get("excluded_uav_ids") or []):
            continue
        dispatchable = uav["state"] == "IDLE" or (
            ALLOW_BASE_CHARGE_INTERRUPTION
            and uav["state"] == "CHARGING"
            and uav.get("phase") == "BASE_CHARGING"
        )
        if not dispatchable:
            continue
        if uav["max_payload_kg"] + 1e-9 < task["payload_kg"]:
            continue
        required = estimate_required_battery(uav, task)
        # 完整任务电量不足不再直接淘汰：后端会基于真实 A* 路线判断是否可经换电站完成。
        # 这里只排除连安全前往换电站都不适合的极低电量飞机。
        if uav["battery"] < MIN_DISPATCH_BATTERY_PERCENT:
            continue
        reposition = haversine_m(uav, task_pickup_point(task))
        # 更近、更高电量的无人机优先。
        score = reposition - uav["battery"] * 15.0 + max(0.0, required - uav["battery"]) * 5.0
        candidates.append((score, uav))
    candidates.sort(key=lambda x: x[0])
    return [uav for _, uav in candidates]


def choose_uav(task: Dict, uavs: Iterable[Dict]) -> Optional[Dict]:
    candidates = rank_uavs(task, uavs)
    return candidates[0] if candidates else None
