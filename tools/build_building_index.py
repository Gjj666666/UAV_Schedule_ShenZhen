#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为深圳建筑 Shapefile 建立轻量 SQLite RTree 空间索引。"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
import shapefile

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "shenzhen_buildings"
INDEX_PATH = DATA_DIR / "buildings_index.sqlite"


def find_shapefile() -> Path:
    files = sorted(DATA_DIR.glob("*.shp"))
    if not files:
        raise FileNotFoundError(f"没有在 {DATA_DIR} 找到 .shp")
    return files[0]


def iter_polygon_bboxes(shp_path: Path):
    """按记录顺序快速读取 Polygon 记录 bbox，不解析全部顶点。"""
    with shp_path.open("rb") as f:
        f.seek(100)  # Shapefile 文件头固定 100 字节
        rec_idx = 0
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            _rec_no, content_words = struct.unpack(">2i", header)
            content_bytes = content_words * 2
            prefix = f.read(min(content_bytes, 36))
            if len(prefix) < 4:
                break
            shape_type = struct.unpack("<i", prefix[:4])[0]
            if shape_type in (5, 15, 25) and len(prefix) >= 36:  # Polygon/Z/M
                minx, miny, maxx, maxy = struct.unpack("<4d", prefix[4:36])
                yield rec_idx, minx, miny, maxx, maxy
            else:
                yield rec_idx, None, None, None, None
            remaining = content_bytes - len(prefix)
            if remaining > 0:
                f.seek(remaining, 1)
            rec_idx += 1


def build_index(force: bool = False):
    shp = find_shapefile()
    if INDEX_PATH.exists() and not force:
        print(f"Index already exists: {INDEX_PATH}")
        return
    if INDEX_PATH.exists():
        INDEX_PATH.unlink()

    reader = shapefile.Reader(str(shp), encoding="utf-8")
    field_names = [f[0] for f in reader.fields[1:]]
    try:
        height_idx = field_names.index("Height")
    except ValueError as exc:
        raise RuntimeError(f"缺少 Height 字段，现有字段：{field_names}") from exc

    conn = sqlite3.connect(INDEX_PATH)
    conn.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE building_meta (
            id INTEGER PRIMARY KEY,
            height REAL NOT NULL
        );
        CREATE VIRTUAL TABLE building_rtree USING rtree(
            id,
            minx, maxx,
            miny, maxy
        );
        CREATE TABLE dataset_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)

    rec_iter = reader.iterRecords()
    bbox_iter = iter_polygon_bboxes(shp)
    meta_rows = []
    rtree_rows = []
    count = 0
    min_h = float("inf")
    max_h = float("-inf")
    for rec, bbox in zip(rec_iter, bbox_iter):
        rec_idx, minx, miny, maxx, maxy = bbox
        if minx is None:
            continue
        h = float(rec[height_idx] or 0.0)
        rid = rec_idx + 1
        meta_rows.append((rid, h))
        rtree_rows.append((rid, minx, maxx, miny, maxy))
        count += 1
        min_h = min(min_h, h)
        max_h = max(max_h, h)
        if len(meta_rows) >= 10000:
            conn.executemany("INSERT INTO building_meta(id,height) VALUES (?,?)", meta_rows)
            conn.executemany("INSERT INTO building_rtree(id,minx,maxx,miny,maxy) VALUES (?,?,?,?,?)", rtree_rows)
            meta_rows.clear(); rtree_rows.clear()
            if count % 100000 == 0:
                print(f"Indexed {count:,} buildings...")
    if meta_rows:
        conn.executemany("INSERT INTO building_meta(id,height) VALUES (?,?)", meta_rows)
        conn.executemany("INSERT INTO building_rtree(id,minx,maxx,miny,maxy) VALUES (?,?,?,?,?)", rtree_rows)

    bbox = reader.bbox
    info = {
        "count": str(count),
        "min_height": str(min_h),
        "max_height": str(max_h),
        "min_lon": str(bbox[0]),
        "min_lat": str(bbox[1]),
        "max_lon": str(bbox[2]),
        "max_lat": str(bbox[3]),
        "crs": "EPSG:4326",
        "height_field": "Height",
    }
    conn.executemany("INSERT INTO dataset_info(key,value) VALUES (?,?)", info.items())
    conn.commit(); conn.close(); reader.close()
    print(f"Built {INDEX_PATH}: {count:,} buildings")


if __name__ == "__main__":
    build_index(force=True)
