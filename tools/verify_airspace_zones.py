#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证硬禁飞、审批放行和大型空域绕行逻辑。"""
from __future__ import annotations

import importlib.util
from importlib.machinery import ModuleSpec
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("shapefile") is None:
    stub = types.ModuleType("shapefile")
    stub.Reader = object
    stub.__spec__ = ModuleSpec("shapefile", loader=None)
    sys.modules["shapefile"] = stub

from planner import Obstacle, plan_route
from airspace_config import default_approval_states
from simulator import DispatchSystem


class EmptyBuildingStore:
    available = True

    @staticmethod
    def buildings_at_point(_lon, _lat):
        return []

    @staticmethod
    def query_route_bboxes(*_args, **_kwargs):
        return []


def main() -> None:
    system = DispatchSystem()
    # 回归测试使用默认审批状态且不写入用户的 runtime/state.json，保证重复运行稳定。
    system.airspace_approvals = default_approval_states()
    system._regulatory_obstacles_cache = None
    system._regulatory_obstacles_signature = None
    system._write_snapshot = lambda: None
    state = system.airspace_state()
    assert state["hard_active_count"] >= 3
    assert state["controlled_active_count"] >= 2

    daya = system.airspace_restrictions_at_point({"lon": 114.5436, "lat": 22.5965})
    assert any(zone["id"] == "SZ-HARD-DAYA-BAY-NUCLEAR" for zone in daya)

    airport_point = {"lon": 113.8107, "lat": 22.6393}
    assert any(zone["id"] == "SZ-CTRL-BAOAN-AIRPORT" for zone in system.airspace_restrictions_at_point(airport_point))
    system.set_airspace_approval("SZ-CTRL-BAOAN-AIRPORT", True)
    assert not any(zone["id"] == "SZ-CTRL-BAOAN-AIRPORT" for zone in system.airspace_restrictions_at_point(airport_point))

    try:
        system.set_airspace_approval("SZ-HARD-DAYA-BAY-NUCLEAR", True)
        raise AssertionError("硬禁飞区不应允许审批解除")
    except ValueError:
        pass

    # 不依赖真实建筑库，验证 A* 能绕开横跨直达线的大型禁飞多边形。
    obstacle = Obstacle(
        kind="no_fly",
        name="TEST_ZONE",
        height=10_000.0,
        polygon=[
            (114.000, 22.520), (114.012, 22.520),
            (114.012, 22.550), (114.000, 22.550),
        ],
    )
    result = plan_route(
        {"lon": 113.985, "lat": 22.535},
        {"lon": 114.027, "lat": 22.535},
        80.0,
        "shenzhen",
        [obstacle],
        EmptyBuildingStore(),
    )
    assert result["planning_failed"] is False
    assert result["route_length_m"] > result["distance_m"]
    print("Airspace zone verification passed.")


if __name__ == "__main__":
    main()
