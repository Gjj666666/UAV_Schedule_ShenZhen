#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳建筑数据访问层。

数据来源：用户提供的深圳建筑 WGS84 Shapefile。
- 约 74.5 万个建筑 Polygon
- Height 字段为建筑高度（米）

设计原则：
1. 完整建筑数据只放在后端；浏览器不一次性加载全市 74 万栋建筑。
2. SQLite RTree 只保存 bbox + Height，用于快速路径规划筛选。
3. 只有 3D 展示时才从 Shapefile 读取局部真实 Polygon。
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import shapefile

EARTH_R = 6_371_000.0
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "shenzhen_buildings"
SHP_PATH = DATA_DIR / "shenzhen_buildings_wgs84.shp"
INDEX_PATH = DATA_DIR / "buildings_index.sqlite"


def _meters_to_lat_deg(m: float) -> float:
    return math.degrees(m / EARTH_R)


def _meters_to_lon_deg(m: float, lat: float) -> float:
    c = max(0.2, math.cos(math.radians(lat)))
    return math.degrees(m / (EARTH_R * c))


def _local_xy(lon: float, lat: float, lon0: float, lat0: float) -> Tuple[float, float]:
    x = math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / c2))
    qx, qy = ax + t * vx, ay + t * vy
    return math.hypot(px - qx, py - qy)


class ShenzhenBuildingStore:
    def __init__(self, shp_path: Path = SHP_PATH, index_path: Path = INDEX_PATH):
        self.shp_path = Path(shp_path)
        self.index_path = Path(index_path)
        self.available = self.shp_path.exists() and self.index_path.exists()
        self._reader: Optional[shapefile.Reader] = None
        self._reader_lock = Lock()
        self._info = self._load_info() if self.available else {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_info(self) -> Dict:
        if not self.available:
            return {}
        with self._connect() as conn:
            rows = conn.execute("SELECT key,value FROM dataset_info").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        numeric = {"count", "min_height", "max_height", "min_lon", "min_lat", "max_lon", "max_lat"}
        for k in numeric:
            if k in raw:
                raw[k] = int(raw[k]) if k == "count" else float(raw[k])
        return raw

    def stats(self) -> Dict:
        return {
            "available": self.available,
            "source": "用户提供深圳建筑 WGS84 Shapefile",
            **self._info,
        }

    def query_bbox(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        min_height: float = 0.0,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """通过 RTree 查询 bbox 相交建筑，只返回 bbox + Height，不读取 Polygon。"""
        if not self.available:
            return []
        sql = """
            SELECT r.id, r.minx, r.maxx, r.miny, r.maxy, m.height
            FROM building_rtree r
            JOIN building_meta m ON m.id = r.id
            WHERE r.maxx >= ? AND r.minx <= ?
              AND r.maxy >= ? AND r.miny <= ?
              AND m.height >= ?
        """
        params: List[object] = [min_lon, max_lon, min_lat, max_lat, float(min_height)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_route_bboxes(
        self,
        start: Dict,
        end: Dict,
        buffer_m: float = 1000.0,
        min_height: float = 0.0,
        max_candidates: int = 50000,
    ) -> List[Dict]:
        """筛出直线航线走廊附近的建筑 bbox。

        先用 RTree 做矩形粗筛，再按 bbox 中心到起终点线段的距离过滤。
        """
        if not self.available:
            return []
        lat0 = (start["lat"] + end["lat"]) / 2.0
        dlat = _meters_to_lat_deg(buffer_m)
        dlon = _meters_to_lon_deg(buffer_m, lat0)
        min_lon = min(start["lon"], end["lon"]) - dlon
        max_lon = max(start["lon"], end["lon"]) + dlon
        min_lat = min(start["lat"], end["lat"]) - dlat
        max_lat = max(start["lat"], end["lat"]) + dlat
        candidates = self.query_bbox(min_lon, min_lat, max_lon, max_lat, min_height=min_height)

        lon0 = (start["lon"] + end["lon"]) / 2.0
        sx, sy = _local_xy(start["lon"], start["lat"], lon0, lat0)
        ex, ey = _local_xy(end["lon"], end["lat"], lon0, lat0)
        selected = []
        for b in candidates:
            cx = (b["minx"] + b["maxx"]) / 2.0
            cy = (b["miny"] + b["maxy"]) / 2.0
            x, y = _local_xy(cx, cy, lon0, lat0)
            x1, y1 = _local_xy(b["minx"], b["miny"], lon0, lat0)
            x2, y2 = _local_xy(b["maxx"], b["maxy"], lon0, lat0)
            half_diag = math.hypot(x2 - x1, y2 - y1) / 2.0
            d = _point_segment_distance(x, y, sx, sy, ex, ey)
            if d <= buffer_m + half_diag:
                b["corridor_distance_m"] = d
                selected.append(b)
        selected.sort(key=lambda b: (b["corridor_distance_m"], -b["height"]))
        return selected[:max_candidates]

    def _get_reader(self) -> shapefile.Reader:
        if self._reader is None:
            self._reader = shapefile.Reader(str(self.shp_path), encoding="utf-8")
        return self._reader

    @staticmethod
    def _shape_rings(shape) -> List[List[List[float]]]:
        points = shape.points
        parts = list(shape.parts) + [len(points)]
        rings: List[List[List[float]]] = []
        for i in range(len(parts) - 1):
            ring = [[float(x), float(y)] for x, y in points[parts[i]:parts[i + 1]]]
            if len(ring) >= 3:
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                rings.append(ring)
        return rings

    @staticmethod
    def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
            if ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
            ):
                inside = not inside
            j = i
        return inside

    def buildings_at_point(self, lon: float, lat: float, min_height: float = 0.0) -> List[Dict]:
        """返回真正包含目标点的建筑，不只依赖 RTree 包围框。"""
        records = self.query_bbox(lon, lat, lon, lat, min_height=min_height)
        if not records:
            return []
        matches = []
        with self._reader_lock:
            reader = self._get_reader()
            for b in records:
                try:
                    shape = reader.shape(int(b["id"]) - 1)
                except Exception:
                    continue
                # Shapefile 外环/内环使用奇偶规则组合，可正确排除建筑内院。
                inside = False
                for ring in self._shape_rings(shape):
                    if self._point_in_ring(lon, lat, ring):
                        inside = not inside
                if inside:
                    matches.append({
                        "id": int(b["id"]),
                        "height": float(b["height"]),
                        "name": f"SZ-BLD-{int(b['id']):06d}",
                    })
        return sorted(matches, key=lambda item: item["height"], reverse=True)

    def geojson_for_records(self, records: Sequence[Dict], max_features: int = 1500) -> Dict:
        """读取真实建筑 Polygon，供 Cesium 局部 3D 拉伸显示。"""
        features = []
        if not self.available:
            return {"type": "FeatureCollection", "features": features}
        with self._reader_lock:
            reader = self._get_reader()
            for b in records[:max_features]:
                rec_idx = int(b["id"]) - 1
                try:
                    shape = reader.shape(rec_idx)
                except Exception:
                    continue
                rings = self._shape_rings(shape)
                if not rings:
                    continue
                # Shapefile 多 part 在 Demo 中作为 MultiPolygon 的独立面显示。
                if len(rings) == 1:
                    geom = {"type": "Polygon", "coordinates": [rings[0]]}
                else:
                    geom = {"type": "MultiPolygon", "coordinates": [[[p for p in ring]] for ring in rings]}
                features.append({
                    "type": "Feature",
                    "properties": {
                        "kind": "building",
                        "record_id": int(b["id"]),
                        "height": round(float(b["height"]), 3),
                        "name": f"SZ-BLD-{int(b['id']):06d}",
                    },
                    "geometry": geom,
                })
        return {"type": "FeatureCollection", "features": features}

    def count_bbox(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        min_height: float = 0.0,
    ) -> int:
        """统计 bbox 内与视野相交的建筑数量，用于前端 LOD/状态提示。"""
        if not self.available:
            return 0
        sql = """
            SELECT COUNT(*) AS n
            FROM building_rtree r
            JOIN building_meta m ON m.id = r.id
            WHERE r.maxx >= ? AND r.minx <= ?
              AND r.maxy >= ? AND r.miny <= ?
              AND m.height >= ?
        """
        with self._connect() as conn:
            row = conn.execute(
                sql,
                [min_lon, max_lon, min_lat, max_lat, float(min_height)],
            ).fetchone()
        return int(row["n"] if row else 0)

    def query_bbox_for_visual(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        min_height: float = 0.0,
        limit: int = 6000,
    ) -> List[Dict]:
        """视野显示查询。

        当当前视野建筑过多时优先返回较高建筑，避免城市总览时一次把几十万
        Polygon 传给浏览器。缩放到局部后 min_height 会自动下降，从而显示全部
        细小建筑。
        """
        if not self.available:
            return []
        sql = """
            SELECT r.id, r.minx, r.maxx, r.miny, r.maxy, m.height
            FROM building_rtree r
            JOIN building_meta m ON m.id = r.id
            WHERE r.maxx >= ? AND r.minx <= ?
              AND r.maxy >= ? AND r.miny <= ?
              AND m.height >= ?
            ORDER BY m.height DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                [min_lon, max_lon, min_lat, max_lat, float(min_height), int(limit)],
            ).fetchall()
        return [dict(r) for r in rows]

    def geojson_in_bbox(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
        min_height: float = 1.0,
        max_features: int = 6000,
    ) -> Dict:
        """按当前 Cesium 视野返回真实建筑 Polygon。

        这是“全深圳动态流式建筑”接口：不再依赖任务航线。用户移动/缩放地图
        时，前端按当前视野持续调用本接口。
        """
        matched = self.count_bbox(
            min_lon, min_lat, max_lon, max_lat, min_height=min_height
        )
        records = self.query_bbox_for_visual(
            min_lon, min_lat, max_lon, max_lat,
            min_height=min_height, limit=max_features
        )
        fc = self.geojson_for_records(records, max_features=max_features)
        fc["meta"] = {
            "matched": matched,
            "returned": len(fc["features"]),
            "truncated": matched > len(fc["features"]),
            "min_height": float(min_height),
            "bbox": [min_lon, min_lat, max_lon, max_lat],
            "dataset_count": self._info.get("count", 0),
        }
        return fc

    def geojson_near_route(
        self,
        start: Dict,
        end: Dict,
        buffer_m: float = 700.0,
        max_features: int = 1200,
        min_height: float = 3.0,
    ) -> Dict:
        records = self.query_route_bboxes(
            start, end,
            buffer_m=buffer_m,
            min_height=min_height,
            max_candidates=max(max_features * 4, 4000),
        )
        # 视觉上优先保留靠近航线的建筑；过多时只取局部代表集。
        fc = self.geojson_for_records(records, max_features=max_features)
        fc["meta"] = {
            "matched": len(records),
            "returned": len(fc["features"]),
            "buffer_m": buffer_m,
            "dataset_count": self._info.get("count", 0),
        }
        return fc


BUILDING_STORE = ShenzhenBuildingStore()
