#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归验证：前海换电站到福田中心换电站不应被粗网格误判为无路。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 该测试只读取 SQLite 建筑包围框；环境没有 pyshp 时提供导入占位，不读取 shp。
if importlib.util.find_spec("shapefile") is None:
    sys.modules["shapefile"] = types.ModuleType("shapefile")

from building_store import ShenzhenBuildingStore  # noqa: E402
from planner import plan_route  # noqa: E402


def main():
    store = ShenzhenBuildingStore()
    assert store.available, "深圳建筑索引不可用"
    # 运行记录已确认两个换电站点均不在真实建筑 Polygon 内；此回归只隔离检查
    # 长航线网格栅格化，避免没有 pyshp 的测试环境读取 Polygon。
    store.buildings_at_point = lambda lon, lat, min_height=0.0: []
    progress_updates = []
    result = plan_route(
        {"name": "前海换电站", "lon": 113.8976, "lat": 22.5312},
        {"name": "福田中心换电站", "lon": 114.0570, "lat": 22.5417},
        80,
        "shenzhen",
        [],
        store,
        progress_callback=lambda percent, stage: progress_updates.append((percent, stage)),
    )
    assert not result.get("planning_failed"), result.get("failure_reason")
    assert result["route"]
    assert result["selected_altitude"] in {100.0, 120.0}
    assert result["buildings_considered"] > 0
    assert len(progress_updates) >= 5
    assert progress_updates[-1][0] == 1.0
    assert any("航层" in stage for _, stage in progress_updates)
    print(
        "PASS: 前海换电站 -> 福田中心换电站，"
        f"选择 {result['selected_altitude']:.0f}m 航层，"
        f"路线 {result['route_length_m']:.1f}m，"
        f"检查建筑 {result['buildings_considered']} 栋"
    )


if __name__ == "__main__":
    main()
