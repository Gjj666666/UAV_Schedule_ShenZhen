#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳低空高度感知航线规划器（V2）。

核心改进：
1. 深圳 Shapefile 建筑数据真正参与规划，不再只做 3D 展示。
2. 使用 Height 字段 + 15m 垂直安全余量判断某高度层能否飞越建筑。
3. 在多个高度层上分别执行 A*，比较“升高飞越”和“横向绕行”的综合代价。
4. 完整 74.5 万栋建筑通过 RTree 先做局部走廊筛选，避免全量加载。
5. 用户上传的 GeoJSON 建筑/禁飞区仍可作为额外约束叠加。

这是清晰可运行的“分层 3D / height-aware A*”原型，不是连续空间全 3D A*。
"""
from __future__ import annotations

import heapq
import math
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from planning_config import (
    FAILED_ROUTE_CACHE_TTL_SECONDS,
    RASTERIZE_CUSTOM_OBSTACLES,
    ROUTE_CACHE_MAX_ITEMS,
    ROUTE_CACHE_TTL_SECONDS,
)

EARTH_R = 6_371_000.0
BUILDING_VERTICAL_CLEARANCE_M = 15.0
HORIZONTAL_CLEARANCE_M = 25.0
STANDARD_ALT_LAYERS = (60.0, 80.0, 100.0, 120.0)

_ROUTE_CACHE = OrderedDict()
_ROUTE_CACHE_LOCK = Lock()
_ROUTE_CACHE_HITS = 0
_ROUTE_CACHE_MISSES = 0


@dataclass
class Obstacle:
    kind: str
    polygon: List[Tuple[float, float]]  # lon, lat
    height: float = 10_000.0
    name: str = ""


def _point_cache_key(point: Dict) -> Tuple[float, float, float]:
    return (
        round(float(point["lon"]), 8),
        round(float(point["lat"]), 8),
        round(float(point.get("alt", 0.0)), 2),
    )


def _obstacles_cache_key(obstacles: Sequence[Obstacle]) -> Tuple:
    return tuple(
        (
            str(obstacle.kind),
            round(float(obstacle.height), 2),
            str(obstacle.name),
            tuple((round(float(lon), 7), round(float(lat), 7)) for lon, lat in obstacle.polygon),
        )
        for obstacle in obstacles
    )


def _building_store_cache_key(building_store) -> Tuple:
    if building_store is None:
        return ("none",)
    index_path = getattr(building_store, "index_path", None)
    if index_path is None:
        return (
            type(building_store).__module__, type(building_store).__qualname__,
            id(building_store), bool(getattr(building_store, "available", False)),
        )
    path = Path(index_path)
    try:
        stat = path.stat()
        version = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        version = (str(path), None, None)
    return (
        type(building_store).__module__, type(building_store).__qualname__,
        bool(getattr(building_store, "available", False)), version,
    )


def _route_cache_key(
    start: Dict,
    end: Dict,
    cruise_alt: float,
    obstacles: Sequence[Obstacle],
    building_store,
) -> Tuple:
    return (
        _point_cache_key(start),
        _point_cache_key(end),
        round(float(cruise_alt), 2),
        _obstacles_cache_key(obstacles),
        _building_store_cache_key(building_store),
    )


def clear_route_cache() -> None:
    global _ROUTE_CACHE_HITS, _ROUTE_CACHE_MISSES
    with _ROUTE_CACHE_LOCK:
        _ROUTE_CACHE.clear()
        _ROUTE_CACHE_HITS = 0
        _ROUTE_CACHE_MISSES = 0


def route_cache_stats() -> Dict:
    with _ROUTE_CACHE_LOCK:
        return {
            "enabled": int(ROUTE_CACHE_MAX_ITEMS) > 0,
            "items": len(_ROUTE_CACHE),
            "max_items": max(0, int(ROUTE_CACHE_MAX_ITEMS)),
            "hits": _ROUTE_CACHE_HITS,
            "misses": _ROUTE_CACHE_MISSES,
        }


def haversine_m(a: Dict, b: Dict) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, [a["lon"], a["lat"], b["lon"], b["lat"]])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def _local_xy(lon: float, lat: float, lon0: float, lat0: float) -> Tuple[float, float]:
    x = math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def _lonlat(x: float, y: float, lon0: float, lat0: float) -> Tuple[float, float]:
    lon = lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(lat0))))
    lat = lat0 + math.degrees(y / EARTH_R)
    return lon, lat


def _point_in_polygon(x: float, y: float, poly: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / c2))
    qx, qy = ax + t * vx, ay + t * vy
    return math.hypot(px - qx, py - qy)


def _straight_route(start: Dict, end: Dict, cruise_alt: float) -> List[Dict[str, float]]:
    distance = max(1.0, haversine_m(start, end))
    count = max(10, min(120, math.ceil(distance / 500.0)))
    points = []
    for i in range(count + 1):
        f = i / count
        lon = start["lon"] + (end["lon"] - start["lon"]) * f
        lat = start["lat"] + (end["lat"] - start["lat"]) * f
        if f < 0.12:
            alt = 25.0 + (cruise_alt - 25.0) * (f / 0.12)
        elif f > 0.88:
            alt = 25.0 + (cruise_alt - 25.0) * ((1.0 - f) / 0.12)
        else:
            alt = cruise_alt
        points.append({"lon": lon, "lat": lat, "alt": max(20.0, alt)})
    return points


def _route_length(points: Sequence[Dict]) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def _custom_obstacle_blocked(
    x: float,
    y: float,
    obstacles_xy: Sequence[Tuple[str, float, List[Tuple[float, float]]]],
    altitude: float,
    clearance: float,
) -> bool:
    for kind, height, poly in obstacles_xy:
        if kind == "building" and height + BUILDING_VERTICAL_CLEARANCE_M < altitude:
            continue
        if _point_in_polygon(x, y, poly):
            return True
        for i in range(len(poly)):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % len(poly)]
            if _point_segment_distance(x, y, ax, ay, bx, by) < clearance:
                return True
    return False


def _altitude_candidates(preferred_alt: float) -> List[float]:
    # 保留用户首选高度，同时允许算法向上下相邻标准航层调整。
    vals = {float(preferred_alt)}
    vals.update(STANDARD_ALT_LAYERS)
    return sorted(v for v in vals if 50.0 <= v <= 120.0)


def _astar_one_layer(
    start: Dict,
    end: Dict,
    altitude: float,
    start_altitude: float,
    end_altitude: float,
    custom_obstacles: Sequence[Obstacle],
    building_records: Sequence[Dict],
    margin_m: float = 1000.0,
) -> Optional[Dict]:
    lon0 = (start["lon"] + end["lon"]) / 2.0
    lat0 = (start["lat"] + end["lat"]) / 2.0
    sx, sy = _local_xy(start["lon"], start["lat"], lon0, lat0)
    gx, gy = _local_xy(end["lon"], end["lat"], lon0, lat0)

    minx, maxx = min(sx, gx) - margin_m, max(sx, gx) + margin_m
    miny, maxy = min(sy, gy) - margin_m, max(sy, gy) + margin_m
    span = max(maxx - minx, maxy - miny)
    cell = max(55.0, min(180.0, span / 105.0))
    horizontal_clearance = max(HORIZONTAL_CLEARANCE_M, cell * 0.35)

    def node_of(x, y):
        return int(round((x - minx) / cell)), int(round((y - miny) / cell))

    def xy_of(n):
        return minx + n[0] * cell, miny + n[1] * cell

    start_n, goal_n = node_of(sx, sy), node_of(gx, gy)
    max_ix = int(math.ceil((maxx - minx) / cell))
    max_iy = int(math.ceil((maxy - miny) / cell))

    # 用真实深圳建筑 Height 判断当前高度层哪些建筑会成为障碍。
    # 网格节点代表其周围半个 cell 的区域，因此只额外扩张半格。旧实现使用
    # floor(lower)/ceil(upper) 后再闭区间遍历，会在两侧各误封最多一整格；
    # 16km 级航线的 cell 接近 180m 时，安全余量会被意外扩大到约 240m。
    blocked_cells = set()
    blocking_buildings = 0
    raster_padding = horizontal_clearance + cell * 0.5
    for b in building_records:
        if float(b["height"]) + BUILDING_VERTICAL_CLEARANCE_M < altitude:
            continue
        blocking_buildings += 1
        bx1, by1 = _local_xy(float(b["minx"]), float(b["miny"]), lon0, lat0)
        bx2, by2 = _local_xy(float(b["maxx"]), float(b["maxy"]), lon0, lat0)
        bx_min, bx_max = min(bx1, bx2) - raster_padding, max(bx1, bx2) + raster_padding
        by_min, by_max = min(by1, by2) - raster_padding, max(by1, by2) + raster_padding
        # 只封锁坐标真正落入扩张包围框的网格节点。
        ix1 = max(0, int(math.ceil((bx_min - minx) / cell)))
        ix2 = min(max_ix, int(math.floor((bx_max - minx) / cell)))
        iy1 = max(0, int(math.ceil((by_min - miny) / cell)))
        iy2 = min(max_iy, int(math.floor((by_max - miny) / cell)))
        if ix1 > ix2 or iy1 > iy2:
            continue
        for ix in range(ix1, ix2 + 1):
            for iy in range(iy1, iy2 + 1):
                blocked_cells.add((ix, iy))

    custom_xy = []
    for o in custom_obstacles:
        poly = [_local_xy(lon, lat, lon0, lat0) for lon, lat in o.polygon]
        if len(poly) >= 3:
            custom_xy.append((o.kind, o.height, poly))

    # 原实现会在 A* 扩展每个相邻节点时重新遍历所有禁飞区及其每条边，
    # 一条路线可能执行数千万次几何运算。这里按完全相同的节点判定规则，
    # 在搜索前只计算一次禁飞区阻塞格，A* 内部随后只做 O(1) 集合查询。
    custom_blocked_cells = set()
    if RASTERIZE_CUSTOM_OBSTACLES:
        for kind, height, poly in custom_xy:
            if kind == "building" and float(height) + BUILDING_VERTICAL_CLEARANCE_M < altitude:
                continue
            poly_minx = min(point[0] for point in poly) - horizontal_clearance
            poly_maxx = max(point[0] for point in poly) + horizontal_clearance
            poly_miny = min(point[1] for point in poly) - horizontal_clearance
            poly_maxy = max(point[1] for point in poly) + horizontal_clearance
            ix1 = max(0, int(math.ceil((poly_minx - minx) / cell)))
            ix2 = min(max_ix, int(math.floor((poly_maxx - minx) / cell)))
            iy1 = max(0, int(math.ceil((poly_miny - miny) / cell)))
            iy2 = min(max_iy, int(math.floor((poly_maxy - miny) / cell)))
            if ix1 > ix2 or iy1 > iy2:
                continue
            one_obstacle = [(kind, height, poly)]
            for ix in range(ix1, ix2 + 1):
                for iy in range(iy1, iy2 + 1):
                    x, y = xy_of((ix, iy))
                    if _custom_obstacle_blocked(
                        x, y, one_obstacle, altitude, horizontal_clearance,
                    ):
                        custom_blocked_cells.add((ix, iy))

    def valid(n):
        ix, iy = n
        if ix < 0 or iy < 0 or ix > max_ix or iy > max_iy:
            return False
        if n in blocked_cells or n in custom_blocked_cells:
            return False
        if RASTERIZE_CUSTOM_OBSTACLES:
            return True
        x, y = xy_of(n)
        return not _custom_obstacle_blocked(x, y, custom_xy, altitude, horizontal_clearance)

    moves = [
        (-1, -1, math.sqrt(2)), (-1, 0, 1), (-1, 1, math.sqrt(2)),
        (0, -1, 1), (0, 1, 1),
        (1, -1, math.sqrt(2)), (1, 0, 1), (1, 1, math.sqrt(2)),
    ]
    open_heap = [(0.0, start_n)]
    g_score = {start_n: 0.0}
    parent = {}
    closed = set()

    while open_heap:
        _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        if current == goal_n:
            break
        closed.add(current)
        for dx, dy, w in moves:
            nxt = (current[0] + dx, current[1] + dy)
            if not valid(nxt):
                continue
            # 对角移动必须同时经过两个相邻正交网格，避免从建筑阻塞格的
            # 角点缝隙斜穿过去。
            if dx and dy and (
                not valid((current[0] + dx, current[1]))
                or not valid((current[0], current[1] + dy))
            ):
                continue
            ng = g_score[current] + w
            if ng < g_score.get(nxt, float("inf")):
                g_score[nxt] = ng
                parent[nxt] = current
                h = math.hypot(nxt[0] - goal_n[0], nxt[1] - goal_n[1])
                heapq.heappush(open_heap, (ng + h, nxt))

    if goal_n != start_n and goal_n not in parent:
        return None

    nodes = [goal_n]
    while nodes[-1] != start_n:
        nodes.append(parent[nodes[-1]])
    nodes.reverse()

    # 压缩同方向网格点。
    compact = []
    for i, n in enumerate(nodes):
        if 0 < i < len(nodes) - 1:
            d1 = (nodes[i][0] - nodes[i - 1][0], nodes[i][1] - nodes[i - 1][1])
            d2 = (nodes[i + 1][0] - nodes[i][0], nodes[i + 1][1] - nodes[i][1])
            if d1 == d2:
                continue
        compact.append(n)

    horizontal_points: List[Dict[str, float]] = []
    for n in compact:
        x, y = xy_of(n)
        lon, lat = _lonlat(x, y, lon0, lat0)
        horizontal_points.append({"lon": lon, "lat": lat, "alt": altitude})

    # 起降阶段先在端点垂直升到安全航层，再开始水平飞行；避免原实现的低空斜线穿楼。
    points: List[Dict[str, float]] = [
        {"lon": start["lon"], "lat": start["lat"], "alt": start_altitude}
    ]
    if abs(start_altitude - altitude) > 0.1:
        points.append({"lon": start["lon"], "lat": start["lat"], "alt": altitude})
    for point in horizontal_points:
        last = points[-1]
        if haversine_m(last, point) < 1.0 and abs(last["alt"] - point["alt"]) < 0.1:
            continue
        points.append(point)
    cruise_end = {"lon": end["lon"], "lat": end["lat"], "alt": altitude}
    if haversine_m(points[-1], cruise_end) >= 1.0:
        points.append(cruise_end)
    if abs(end_altitude - altitude) > 0.1:
        points.append({"lon": end["lon"], "lat": end["lat"], "alt": end_altitude})

    return {
        "route": points,
        "route_length_m": _route_length(points),
        "altitude": altitude,
        "blocking_buildings": blocking_buildings,
        "grid_cell_m": round(cell, 1),
    }


def _height_aware_route(
    start: Dict,
    end: Dict,
    preferred_alt: float,
    start_altitude: float,
    end_altitude: float,
    custom_obstacles: Sequence[Obstacle],
    building_store,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Optional[Dict]:
    required_altitude = max(start_altitude, end_altitude)
    candidates = [alt for alt in _altitude_candidates(preferred_alt) if alt >= required_altitude]
    if not candidates:
        return None
    min_relevant_height = max(0.0, min(candidates) - BUILDING_VERTICAL_CLEARANCE_M)

    # 大型机场、核电和临时管控区可能宽达数公里。固定 1.1km 搜索边界会让
    # A* 明明可以绕行却直接失败，因此只对靠近当前直达走廊的障碍自适应扩边。
    route_min_lon, route_max_lon = sorted((float(start["lon"]), float(end["lon"])))
    route_min_lat, route_max_lat = sorted((float(start["lat"]), float(end["lat"])))
    nearby_padding_deg = 0.025
    planning_margin_m = 1100.0
    route_lon0 = (float(start["lon"]) + float(end["lon"])) / 2.0
    route_lat0 = (float(start["lat"]) + float(end["lat"])) / 2.0
    for obstacle in custom_obstacles:
        if not obstacle.polygon:
            continue
        lons = [point[0] for point in obstacle.polygon]
        lats = [point[1] for point in obstacle.polygon]
        if (
            max(lons) < route_min_lon - nearby_padding_deg
            or min(lons) > route_max_lon + nearby_padding_deg
            or max(lats) < route_min_lat - nearby_padding_deg
            or min(lats) > route_max_lat + nearby_padding_deg
        ):
            continue
        local_points = [_local_xy(lon, lat, route_lon0, route_lat0) for lon, lat in obstacle.polygon]
        span_x = max(point[0] for point in local_points) - min(point[0] for point in local_points)
        span_y = max(point[1] for point in local_points) - min(point[1] for point in local_points)
        # 对狭长的边界/道路缓冲带按短边扩张，对圆形机场区域按直径扩张。
        detour_span = min(span_x, span_y)
        planning_margin_m = max(planning_margin_m, min(9500.0, detour_span * 0.65 + 900.0))

    # 只查询航线附近建筑；不把 74.5 万栋全部放进内存。
    if progress_callback:
        progress_callback(0.18, "正在读取航线走廊建筑高度")
    building_records = building_store.query_route_bboxes(
        start,
        end,
        buffer_m=max(1400.0, planning_margin_m + 300.0),
        min_height=min_relevant_height,
        max_candidates=100000,
    ) if building_store and building_store.available else []
    if progress_callback:
        progress_callback(0.32, f"已读取 {len(building_records)} 栋候选建筑")

    direct_distance = haversine_m(start, end)
    results = []
    altitude_attempts = []
    for index, alt in enumerate(candidates):
        if progress_callback:
            progress_callback(
                0.32 + 0.60 * index / max(1, len(candidates)),
                f"正在验证 {alt:.0f} 米航层（{index + 1}/{len(candidates)}）",
            )
        r = _astar_one_layer(
            start, end, alt, start_altitude, end_altitude,
            custom_obstacles, building_records, margin_m=planning_margin_m,
        )
        blocking_count = sum(
            float(building["height"]) + BUILDING_VERTICAL_CLEARANCE_M >= alt
            for building in building_records
        )
        altitude_attempts.append({
            "altitude": alt,
            "route_found": bool(r),
            "blocking_buildings": blocking_count,
        })
        if not r:
            continue
        # 代价：水平航程 + 升降成本 + 偏离用户首选高度的轻微惩罚。
        climb_cost = max(0.0, alt - 25.0) * 4.0
        preference_cost = abs(alt - preferred_alt) * 6.0
        r["score"] = r["route_length_m"] + climb_cost + preference_cost
        results.append(r)

    if progress_callback:
        progress_callback(0.94, "正在比较各航层路线代价")

    if not results:
        return {
            "planning_failed": True,
            "buildings_considered": len(building_records),
            "altitude_candidates": candidates,
            "altitude_attempts": altitude_attempts,
            "direct_distance_m": direct_distance,
        }
    best = min(results, key=lambda r: r["score"])
    return {
        **best,
        "buildings_considered": len(building_records),
        "altitude_candidates": candidates,
        "altitude_attempts": altitude_attempts,
        "direct_distance_m": direct_distance,
    }


def _plan_route_uncached(
    start: Dict,
    end: Dict,
    cruise_alt: float,
    data_mode: str,
    obstacles: Sequence[Obstacle],
    building_store=None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict:
    """生成航线。

    data_mode 只控制前端展示偏好；只要深圳建筑库可用，规划始终优先参考该建筑库。
    """
    cruise_alt = float(max(50.0, min(120.0, cruise_alt)))
    distance = haversine_m(start, end)

    if progress_callback:
        progress_callback(0.03, "正在检查起点和终点安全性")

    if building_store is not None and getattr(building_store, "available", False):
        start_buildings = building_store.buildings_at_point(start["lon"], start["lat"])
        end_buildings = building_store.buildings_at_point(end["lon"], end["lat"])
        start_altitude = max(
            [25.0, float(start.get("alt", 0.0))]
            + [float(b["height"]) + BUILDING_VERTICAL_CLEARANCE_M for b in start_buildings]
        )
        end_altitude = max(
            [25.0, float(end.get("alt", 0.0))]
            + [float(b["height"]) + BUILDING_VERTICAL_CLEARANCE_M for b in end_buildings]
        )
        required_altitude = max(start_altitude, end_altitude)
        if required_altitude > max(STANDARD_ALT_LAYERS):
            location = "起点" if start_altitude >= end_altitude else "终点"
            conflict = start_buildings[0] if location == "起点" else end_buildings[0]
            return {
                "method": "NO_SAFE_ROUTE",
                "planning_failed": True,
                "failure_reason": (
                    f"{location}位于建筑 {conflict['name']} 内，建筑高度 {conflict['height']:.1f}m，"
                    f"加 {BUILDING_VERTICAL_CLEARANCE_M:.0f}m 余量后超过最高航层 {max(STANDARD_ALT_LAYERS):.0f}m；"
                    "请使用地图选点选择附近开阔起降点。"
                ),
                "distance_m": distance,
                "route": [],
                "preferred_alt": cruise_alt,
                "selected_altitude": None,
                "buildings_considered": 0,
                "blocking_buildings": 1,
                "building_vertical_clearance_m": BUILDING_VERTICAL_CLEARANCE_M,
                "start_altitude": start_altitude,
                "end_altitude": end_altitude,
            }
        result = _height_aware_route(
            start, end, cruise_alt, start_altitude, end_altitude, obstacles, building_store,
            progress_callback,
        )
        if result and not result.get("planning_failed"):
            if progress_callback:
                progress_callback(1.0, "本航段安全路线已生成")
            return {
                "method": "SHENZHEN_HEIGHT_AWARE_LAYERED_ASTAR",
                "distance_m": distance,
                "route": result["route"],
                "cruise_alt": result["altitude"],
                "preferred_alt": cruise_alt,
                "selected_altitude": result["altitude"],
                "altitude_candidates": result["altitude_candidates"],
                "altitude_attempts": result["altitude_attempts"],
                "route_length_m": result["route_length_m"],
                "buildings_considered": result["buildings_considered"],
                "blocking_buildings": result["blocking_buildings"],
                "building_vertical_clearance_m": BUILDING_VERTICAL_CLEARANCE_M,
                "grid_cell_m": result["grid_cell_m"],
                "score": result["score"],
                "planning_failed": False,
                "start_altitude": start_altitude,
                "end_altitude": end_altitude,
            }
        failure_details = result or {}
        if progress_callback:
            progress_callback(1.0, "本航段没有找到可用安全路线")
        altitude_attempts = failure_details.get("altitude_attempts", [])
        return {
            "method": "NO_SAFE_ROUTE",
            "planning_failed": True,
            "failure_reason": "所有允许航层均无法找到满足建筑和禁飞区约束的路线；已禁止直线回退。",
            "distance_m": distance,
            "route": [],
            "preferred_alt": cruise_alt,
            "selected_altitude": None,
            "buildings_considered": failure_details.get("buildings_considered", 0),
            "blocking_buildings": max(
                (attempt.get("blocking_buildings", 0) for attempt in altitude_attempts),
                default=0,
            ),
            "altitude_candidates": failure_details.get("altitude_candidates", []),
            "altitude_attempts": altitude_attempts,
            "building_vertical_clearance_m": BUILDING_VERTICAL_CLEARANCE_M,
            "start_altitude": start_altitude,
            "end_altitude": end_altitude,
        }

    # 没有建筑数据时不再生成未经避障验证的直线航线。
    return {
        "method": "NO_BUILDING_DATA",
        "planning_failed": True,
        "failure_reason": "深圳建筑数据不可用，出于安全考虑未生成无避障直线航线。",
        "distance_m": distance,
        "route": [],
        "preferred_alt": cruise_alt,
        "selected_altitude": None,
        "altitude_candidates": [],
        "route_length_m": 0.0,
        "buildings_considered": 0,
        "blocking_buildings": 0,
        "building_vertical_clearance_m": BUILDING_VERTICAL_CLEARANCE_M,
    }


def plan_route(
    start: Dict,
    end: Dict,
    cruise_alt: float,
    data_mode: str,
    obstacles: Sequence[Obstacle],
    building_store=None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict:
    """使用完整约束版本键缓存路线；任何边界、端点或建筑索引变化都会失效。"""
    global _ROUTE_CACHE_HITS, _ROUTE_CACHE_MISSES
    if int(ROUTE_CACHE_MAX_ITEMS) <= 0:
        result = _plan_route_uncached(
            start, end, cruise_alt, data_mode, obstacles, building_store, progress_callback,
        )
        result["cache_hit"] = False
        return result

    key = _route_cache_key(start, end, cruise_alt, obstacles, building_store)
    now = time.monotonic()
    with _ROUTE_CACHE_LOCK:
        cached = _ROUTE_CACHE.get(key)
        if cached is not None:
            expires_at, cached_result = cached
            if expires_at > now:
                _ROUTE_CACHE.move_to_end(key)
                _ROUTE_CACHE_HITS += 1
                result = deepcopy(cached_result)
                result["cache_hit"] = True
                if progress_callback:
                    progress_callback(1.0, "已复用相同安全约束下的路线缓存")
                return result
            del _ROUTE_CACHE[key]
        _ROUTE_CACHE_MISSES += 1

    result = _plan_route_uncached(
        start, end, cruise_alt, data_mode, obstacles, building_store, progress_callback,
    )
    result["cache_hit"] = False
    ttl = (
        float(FAILED_ROUTE_CACHE_TTL_SECONDS)
        if result.get("planning_failed")
        else float(ROUTE_CACHE_TTL_SECONDS)
    )
    if ttl > 0.0:
        stored = deepcopy(result)
        stored["cache_hit"] = False
        with _ROUTE_CACHE_LOCK:
            _ROUTE_CACHE[key] = (time.monotonic() + ttl, stored)
            _ROUTE_CACHE.move_to_end(key)
            while len(_ROUTE_CACHE) > max(1, int(ROUTE_CACHE_MAX_ITEMS)):
                _ROUTE_CACHE.popitem(last=False)
    return result
