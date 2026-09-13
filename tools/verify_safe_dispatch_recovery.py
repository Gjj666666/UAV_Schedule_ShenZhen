#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不依赖 Web 服务，验证安全等待、候选换机、返航和基地充电状态机。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试环境未安装 pyshp 时，仅为 building_store 的类型/延迟读取提供占位。
if importlib.util.find_spec("shapefile") is None:
    shapefile_stub = types.ModuleType("shapefile")
    shapefile_stub.Reader = object
    sys.modules["shapefile"] = shapefile_stub

import simulator as simulator_module
from energy_config import BASE_CHARGE_TARGET_PERCENT, IDLE_RETURN_DELAY_SECONDS
from simulator import DispatchSystem, SIM_BASES


def route_ok(start, end, cruise_alt, *_args, **_kwargs):
    alt = float(cruise_alt)
    return {
        "method": "TEST_SAFE_ROUTE",
        "planning_failed": False,
        "failure_reason": None,
        "selected_altitude": alt,
        "buildings_considered": 0,
        "blocking_buildings": 0,
        "route": [
            {"lon": float(start["lon"]), "lat": float(start["lat"]), "alt": float(start.get("alt", 0.0))},
            {"lon": float(start["lon"]), "lat": float(start["lat"]), "alt": alt},
            {"lon": float(end["lon"]), "lat": float(end["lat"]), "alt": alt},
            {"lon": float(end["lon"]), "lat": float(end["lat"]), "alt": 0.0},
        ],
    }


def new_system() -> DispatchSystem:
    system = DispatchSystem.__new__(DispatchSystem)
    system.uavs = {}
    system.tasks = {}
    system.obstacles = []
    system.custom_features = []
    system.running = False
    system.speed_factor = 4.0
    system.auto_events = False
    system._subscribers = set()
    system._event_cooldown = 0.0
    system._event = lambda *_args: None
    system._save_route = lambda *_args: None
    system._write_snapshot = lambda: None
    system.init_fleet(2)
    for uav in system.uavs.values():
        uav["battery"] = 100.0
    return system


def task_payload(origin, destination):
    return {
        "origin": {"name": "测试起点", **origin},
        "destination": {"name": "测试终点", **destination},
        "payload_kg": 1.0,
        "priority": "NORMAL",
        "cruise_alt": 80.0,
        "data_mode": "shenzhen",
    }


def main():
    original_plan_route = simulator_module.plan_route
    try:
        # 第一候选机规划失败时必须继续尝试下一架，不能直接判整个任务失败。
        system = new_system()
        first = system.uavs["UAV-01"]
        second = system.uavs["UAV-02"]
        origin = {"lon": first["lon"] + 0.0002, "lat": first["lat"] + 0.0002}
        destination = {"lon": origin["lon"] + 0.0003, "lat": origin["lat"] + 0.0003}

        def first_uav_fails(start, end, cruise_alt, *args, **kwargs):
            if abs(float(start["lon"]) - float(first["lon"])) < 1e-8 and end.get("name") == "测试起点":
                return {"method": "NO_SAFE_ROUTE", "planning_failed": True, "failure_reason": "测试阻挡", "route": []}
            return route_ok(start, end, cruise_alt, *args, **kwargs)

        simulator_module.plan_route = first_uav_fails
        task = system.create_task(task_payload(origin, destination))
        system._assign_waiting()
        assert task["status"] == "ASSIGNED"
        assert task["assigned_uav"] == second["id"]
        assert first["state"] == "IDLE"

        # 所有候选均失败时进入安全等待，并保留自动重试时间。
        system = new_system()
        simulator_module.plan_route = lambda *_args, **_kwargs: {
            "method": "NO_SAFE_ROUTE", "planning_failed": True,
            "failure_reason": "测试禁飞约束", "route": [],
        }
        task = system.create_task(task_payload(origin, destination))
        system._assign_waiting()
        assert task["status"] == "WAITING_BLOCKED"
        assert task["retry_at"] > task["created_at"]
        assert all(uav["state"] == "IDLE" for uav in system.uavs.values())

        # 空中任务中止后，任务重回安全等待；无人机必须继续返航而非空中 IDLE。
        system = new_system()
        simulator_module.plan_route = route_ok
        uav = system.uavs["UAV-01"]
        uav.update({"lon": 114.0680, "lat": 22.5490, "alt": 80.0, "state": "BUSY", "phase": "DELIVERING"})
        task = system.create_task(task_payload(origin, destination))
        task.update({"status": "IN_PROGRESS", "assigned_uav": uav["id"]})
        uav["task_id"] = task["id"]
        system._abort_task_safely(uav, task, "测试途中失去安全航线")
        assert task["status"] == "WAITING_BLOCKED"
        assert uav["task_id"] is None
        assert uav["state"] == "BUSY" and uav["phase"] == "RETURNING"

        # 到达基地后自动充电，并在完成后转成满电待命。
        uav["route_index"] = len(uav["route"]) - 1
        target_base = dict(uav["recovery_target"])
        uav["battery"] = 50.0
        system._advance_uav(uav, 0.1)
        assert uav["state"] == "CHARGING" and uav["phase"] == "BASE_CHARGING"
        assert abs(uav["lon"] - target_base["lon"]) < 1e-9
        system._advance_uav(uav, 1000.0)
        assert uav["state"] == "IDLE" and uav["phase"] == "BASE_IDLE"
        assert uav["battery"] == float(BASE_CHARGE_TARGET_PERCENT)

        # 初始基地同样具备充电能力。
        uav = system.uavs["UAV-02"]
        base = next(base for base in SIM_BASES if base["name"] == uav["base"])
        uav["battery"] = 60.0
        system._arrive_at_base(uav, base, "测试基地充电")
        assert uav["phase"] == "BASE_CHARGING"

        # 完成一个任务后，应先从当前位置衔接队列中的下一任务。
        system = new_system()
        simulator_module.plan_route = route_ok
        uav = system.uavs["UAV-01"]
        system.uavs["UAV-02"]["state"] = "BUSY"
        finished = system.create_task(task_payload(origin, destination))
        finished.update({"status": "IN_PROGRESS", "assigned_uav": uav["id"]})
        uav.update({
            "lon": destination["lon"], "lat": destination["lat"], "alt": 0.0,
            "state": "BUSY", "phase": "DELIVERING", "task_id": finished["id"],
        })
        queued = system.create_task(task_payload(
            {"lon": destination["lon"] + 0.0001, "lat": destination["lat"] + 0.0001},
            {"lon": destination["lon"] + 0.0004, "lat": destination["lat"] + 0.0004},
        ))
        system._finish_task(uav, finished)
        system._assign_waiting()
        assert queued["assigned_uav"] == uav["id"] and uav["state"] == "BUSY"

        # 没有后续任务时，终点待命超过阈值后自动返回基地。
        system = new_system()
        simulator_module.plan_route = route_ok
        uav = system.uavs["UAV-01"]
        uav.update({
            "lon": 114.0680, "lat": 22.5490, "alt": 0.0,
            "state": "IDLE", "phase": "IDLE", "battery": 80.0,
            "idle_elapsed_s": float(IDLE_RETURN_DELAY_SECONDS),
        })
        system._manage_idle_and_recovery(0.1)
        assert uav["state"] == "BUSY" and uav["phase"] == "RETURNING"

        # 已取货的任务中止后必须先由原机携货落地，再从实际交接点派另一架接驳。
        system = new_system()
        simulator_module.plan_route = route_ok
        carrier = system.uavs["UAV-01"]
        replacement = system.uavs["UAV-02"]
        cargo_task = system.create_task(task_payload(origin, destination))
        original_origin = dict(cargo_task["origin"])
        carrier.update({
            "lon": 114.0680, "lat": 22.5490, "alt": 80.0,
            "state": "BUSY", "phase": "DELIVERING", "task_id": cargo_task["id"],
        })
        cargo_task.update({
            "status": "IN_PROGRESS", "assigned_uav": carrier["id"],
            "cargo_status": "IN_TRANSIT", "cargo_location": system._cargo_location_on_uav(carrier),
        })
        system._abort_task_safely(carrier, cargo_task, "测试携货异常")
        assert cargo_task["status"] == "CARGO_RECOVERY"
        assert cargo_task["assigned_uav"] == carrier["id"]
        assert carrier["phase"] == "TO_CARGO_HANDOVER"
        system._assign_waiting()
        assert replacement["task_id"] is None, "货物落地前不应提前派接驳无人机"

        carrier["route_index"] = len(carrier["route"]) - 1
        system._advance_uav(carrier, 0.1)
        assert cargo_task["status"] == "WAITING_HANDOVER"
        assert cargo_task["cargo_status"] == "WAITING_HANDOVER"
        assert cargo_task["cargo_location"]["type"] == "HANDOVER"
        assert cargo_task["origin"] == original_origin, "原始取货点必须保留，不能被交接点覆盖"
        handover_location = dict(cargo_task["cargo_location"])
        system._assign_waiting()
        assert cargo_task["status"] == "HANDOVER_ASSIGNED"
        assert cargo_task["assigned_uav"] == replacement["id"]
        assert replacement["phase"] == "TO_HANDOVER"
        assert abs(replacement["route"][-1]["lon"] - handover_location["lon"]) < 1e-9

        replacement["route_index"] = len(replacement["route"]) - 1
        system._advance_uav(replacement, 0.1)
        assert cargo_task["status"] == "IN_PROGRESS"
        assert cargo_task["cargo_status"] == "IN_TRANSIT"
        assert cargo_task["cargo_location"]["uav_id"] == replacement["id"]

        # 没有任何可达交接点时，货物继续绑定原机，严禁虚假重派。
        system = new_system()
        carrier = system.uavs["UAV-01"]
        replacement = system.uavs["UAV-02"]
        cargo_task = system.create_task(task_payload(origin, destination))
        carrier.update({
            "lon": 114.0680, "lat": 22.5490, "alt": 80.0,
            "state": "BUSY", "phase": "DELIVERING", "task_id": cargo_task["id"],
        })
        cargo_task.update({
            "status": "IN_PROGRESS", "assigned_uav": carrier["id"],
            "cargo_status": "IN_TRANSIT", "cargo_location": system._cargo_location_on_uav(carrier),
        })
        simulator_module.plan_route = lambda *_args, **_kwargs: {
            "method": "NO_SAFE_ROUTE", "planning_failed": True,
            "failure_reason": "测试无可达交接点", "route": [],
        }
        system._abort_task_safely(carrier, cargo_task, "测试无可达交接点")
        assert cargo_task["status"] == "CARGO_EMERGENCY"
        assert cargo_task["assigned_uav"] == carrier["id"]
        assert carrier["task_id"] == cargo_task["id"]
        system._assign_waiting()
        assert replacement["task_id"] is None

        print("PASS: 候选换机、安全等待与自动重试状态正常。")
        print("PASS: 任务可连续衔接；无后续任务时自动返航并在基地充电。")
        print("PASS: 飞行中止不会空中 IDLE，而是进入独立安全恢复流程。")
        print("PASS: 已取货异常会先落地交接，再由另一架无人机从实际货物位置接驳。")
        print("PASS: 无可达交接点时货物保持原机占用，不会从原取货点虚假重派。")
    finally:
        simulator_module.plan_route = original_plan_route


if __name__ == "__main__":
    main()
