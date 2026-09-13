#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验模拟基地、换电站与配置空域之间的位置关系。"""
from __future__ import annotations

import ast
import math
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airspace_config import AIRSPACE_ZONES  # noqa: E402
from energy_config import BATTERY_STATIONS  # noqa: E402


def _load_sim_bases() -> List[Dict]:
    """只解析基地常量，避免为配置校验启动完整调度系统。"""
    tree = ast.parse((ROOT / "simulator.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "SIM_BASES" for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("simulator.py 中未找到 SIM_BASES")


def _point_on_segment(lon: float, lat: float, start: Sequence[float], end: Sequence[float]) -> bool:
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(end[0]), float(end[1])
    cross = (lon - ax) * (by - ay) - (lat - ay) * (bx - ax)
    if abs(cross) > 1e-10:
        return False
    return min(ax, bx) - 1e-10 <= lon <= max(ax, bx) + 1e-10 and min(ay, by) - 1e-10 <= lat <= max(ay, by) + 1e-10


def _point_in_or_on_polygon(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    previous = len(ring) - 1
    for index, point in enumerate(ring):
        if _point_on_segment(lon, lat, ring[previous], point):
            return True
        xi, yi = float(point[0]), float(point[1])
        xj, yj = float(ring[previous][0]), float(ring[previous][1])
        if (yi > lat) != (yj > lat):
            crossing_lon = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
            if lon < crossing_lon:
                inside = not inside
        previous = index
    return inside


def _zone_hits(point: Dict) -> List[str]:
    return [
        zone["id"]
        for zone in AIRSPACE_ZONES
        if any(_point_in_or_on_polygon(point["lon"], point["lat"], ring) for ring in zone["polygons"])
    ]


def _assert_unique(items: Iterable[Dict], label: str) -> None:
    values = list(items)
    ids = [item["id"] for item in values]
    coordinates = [(float(item["lon"]), float(item["lat"])) for item in values]
    assert len(ids) == len(set(ids)), f"{label}存在重复 id"
    assert len(coordinates) == len(set(coordinates)), f"{label}存在重复坐标"


def _assert_bases_outside_building_bboxes(bases: List[Dict]) -> None:
    """数据索引存在时做保守检查：基地点不能落入任何建筑 bbox。"""
    index_path = ROOT / "data" / "shenzhen_buildings" / "buildings_index.sqlite"
    if not index_path.exists():
        return
    with sqlite3.connect(index_path) as connection:
        for base in bases:
            count = connection.execute(
                """
                SELECT count(*) FROM building_rtree
                WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?
                """,
                (base["lon"], base["lon"], base["lat"], base["lat"]),
            ).fetchone()[0]
            assert count == 0, f"{base['id']} 落入建筑包围框"


def main() -> None:
    bases = _load_sim_bases()
    assert len(bases) == 10, f"应配置 10 个基地，实际为 {len(bases)}"
    assert len(BATTERY_STATIONS) == 30, f"应保留 30 个换电站，实际为 {len(BATTERY_STATIONS)}"
    _assert_unique(bases, "基地")
    _assert_unique(BATTERY_STATIONS, "换电站")

    # 不受界面审批开关影响：基础设施点必须避开配置中的全部空域。
    for point in [*bases, *BATTERY_STATIONS]:
        hits = _zone_hits(point)
        assert not hits, f"{point['id']} 位于限制空域：{', '.join(hits)}"

    # 防止后续误改后又退化为集中在中心城区。
    assert max(base["lon"] for base in bases) - min(base["lon"] for base in bases) >= 0.45
    assert max(base["lat"] for base in bases) - min(base["lat"] for base in bases) >= 0.15
    _assert_bases_outside_building_bboxes(bases)

    print("PASS: 10 个基地和 30 个换电站均位于全部配置限制空域之外")


if __name__ == "__main__":
    main()
