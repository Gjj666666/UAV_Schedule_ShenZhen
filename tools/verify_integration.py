#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速验证深圳建筑数据是否真正接入规划器。"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from building_store import BUILDING_STORE
from planner import plan_route


def main():
    print("Building dataset:")
    print(json.dumps(BUILDING_STORE.stats(), ensure_ascii=False, indent=2))
    start = {"name": "福田测试A", "lon": 114.0520, "lat": 22.5428}
    end = {"name": "福田测试B", "lon": 114.0665, "lat": 22.5428}
    result = plan_route(start, end, 80, "shenzhen", [], BUILDING_STORE)
    print("\nPlanner result:")
    for k in [
        "method", "preferred_alt", "selected_altitude", "buildings_considered",
        "blocking_buildings", "building_vertical_clearance_m", "route_length_m",
    ]:
        print(f"  {k}: {result.get(k)}")
    assert result["method"] == "SHENZHEN_HEIGHT_AWARE_LAYERED_ASTAR"
    assert not result.get("planning_failed")
    assert result["buildings_considered"] > 0
    assert len(result["route"]) >= 4
    assert result["route"][0]["lon"] == result["route"][1]["lon"]
    assert result["route"][0]["lat"] == result["route"][1]["lat"]

    # 回归：京基100 POI 位于约 278m 建筑内，高于 120m 航层上限，必须安全失败，
    # 不能再回退为 buildings_considered=0 的直线航线。
    high_rise = {"name": "京基100大厦", "lon": 114.1017384, "lat": 22.5456949}
    unsafe = plan_route(high_rise, end, 80, "shenzhen", [], BUILDING_STORE)
    assert unsafe["method"] == "NO_SAFE_ROUTE"
    assert unsafe["planning_failed"] is True
    assert unsafe["route"] == []
    print("\nPASS: 深圳建筑 Height 已参与路线规划。")
    print("PASS: 高楼端点安全失败，未生成无避障直线航线。")


if __name__ == "__main__":
    main()
