#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳无人机禁飞/审批空域配置（WGS84）。

说明：
- HARD 区域始终参与调度避让，前端不能解除；
- CONTROLLED 区域默认未审批，取得审批后可在界面逐区放行；
- `geometry_accuracy=approximate` 表示依据公开文字和公开设施位置制作的演示边界，
  不能替代民航 UOM、航行情报或主管部门批复中的正式坐标。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Sequence, Tuple

EARTH_R = 6_371_000.0


def circle_polygon(lon: float, lat: float, radius_m: float, steps: int = 72) -> List[List[float]]:
    """生成近似测地圆，返回闭合 WGS84 环。"""
    points: List[List[float]] = []
    lat_scale = 180.0 / (math.pi * EARTH_R)
    lon_scale = lat_scale / max(0.2, math.cos(math.radians(lat)))
    for index in range(max(24, int(steps))):
        angle = 2.0 * math.pi * index / max(24, int(steps))
        points.append([
            lon + math.cos(angle) * radius_m * lon_scale,
            lat + math.sin(angle) * radius_m * lat_scale,
        ])
    points.append(list(points[0]))
    return points


def border_corridor_polygons(
    points: Sequence[Tuple[float, float]], north_buffer_m: float = 500.0,
) -> List[List[List[float]]]:
    """将深港边界折线分段生成深圳一侧（近似向北）的缓冲带。"""
    lat_offset = north_buffer_m * 180.0 / (math.pi * EARTH_R)
    polygons: List[List[List[float]]] = []
    for start, end in zip(points, points[1:]):
        polygons.append([
            [start[0], start[1]],
            [end[0], end[1]],
            [end[0], end[1] + lat_offset],
            [start[0], start[1] + lat_offset],
            [start[0], start[1]],
        ])
    return polygons


SOURCE_LOCAL_RULE = "https://www.sz.gov.cn/gkmlpt/content/7/7786/mpost_7786541.html"
SOURCE_CURRENT_NOTICE = "https://ga.sz.gov.cn/ZWGK/QT/GZDT_1/content/post_12908605.html"
SOURCE_AIRPORT_NOTICE = "https://www.sz.gov.cn/zfgb/2017/gb1000/content/post_4990686.html"
SOURCE_LOW_ALTITUDE_PLAN = "https://pnr.sz.gov.cn/xxgk/gggs/content/post_12469261.html"

_BORDER_LINE = (
    (113.8950, 22.4650),
    (113.9400, 22.4970),
    (113.9820, 22.5110),
    (114.0520, 22.5140),
    (114.0830, 22.5210),
    (114.1160, 22.5320),
    (114.1580, 22.5480),
    (114.2360, 22.5460),
)


# 可直接修改此列表中的坐标、有效期和默认审批状态。
AIRSPACE_ZONES: List[Dict] = [
    {
        "id": "SZ-TEMP-FUTIAN-2026",
        "name": "福田中部临时从严管控区",
        "category": "HARD",
        "description": "新洲路—农林路—北环大道—滨河大道围合区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "依据四条道路公开文字生成的道路交点示意边界。",
        "source_name": "深圳市公安局低空飞行从严管控通告",
        "source_url": SOURCE_CURRENT_NOTICE,
        "effective_from": "2026-07-24T00:00:00+08:00",
        "effective_to": "2026-11-23T23:59:59+08:00",
        "polygons": [[
            [114.0124, 22.5658], [114.0523, 22.5657],
            [114.0519, 22.5308], [114.0121, 22.5318],
            [114.0124, 22.5658],
        ]],
    },
    {
        "id": "SZ-HARD-DAYA-BAY-NUCLEAR",
        "name": "大亚湾核电基地5公里限制区",
        "category": "HARD",
        "description": "项目按公开规划所述核电基地周边5公里飞行限制区保守处理。",
        "geometry_accuracy": "approximate",
        "boundary_note": "以公开设施位置为圆心的5公里演示圆，正式运行须替换为主管部门边界。",
        "source_name": "深圳市低空航空器起降设施布局规划",
        "source_url": SOURCE_LOW_ALTITUDE_PLAN,
        "effective_from": None,
        "effective_to": None,
        "polygons": [circle_polygon(114.5436, 22.5965, 5000.0)],
    },
    {
        "id": "SZ-HARD-HK-BORDER-500M",
        "name": "深港边界深圳侧500米缓冲区",
        "category": "HARD",
        "description": "按轻型无人机规则采用深圳一侧500米缓冲。",
        "geometry_accuracy": "approximate",
        "boundary_note": "公开行政边界折线简化结果；正式运行须换成权威边界矢量。",
        "source_name": "深圳市民用微轻型无人机管理暂行办法",
        "source_url": SOURCE_LOCAL_RULE,
        "effective_from": None,
        "effective_to": None,
        "polygons": border_corridor_polygons(_BORDER_LINE),
    },
    {
        "id": "SZ-CTRL-BAOAN-AIRPORT",
        "name": "深圳宝安国际机场管制区",
        "category": "CONTROLLED",
        "description": "机场障碍物限制面和净空保护相关管制区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "8公里演示圆，不等同于UOM机场管制空域多边形。",
        "source_name": "深圳机场净空区域安全保护通告",
        "source_url": SOURCE_AIRPORT_NOTICE,
        "effective_from": None,
        "effective_to": None,
        "default_approved": False,
        "polygons": [circle_polygon(113.8107, 22.6393, 8000.0)],
    },
    {
        "id": "SZ-CTRL-NANTOU-HELIPORT",
        "name": "南头直升机场管制区",
        "category": "CONTROLLED",
        "description": "南头机场C类空域附近的审批管制示意区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "按有人驾驶航空器起降点周边3公里规则生成演示圆。",
        "source_name": "深圳市低空航空器起降设施布局规划",
        "source_url": SOURCE_LOW_ALTITUDE_PLAN,
        "effective_from": None,
        "effective_to": None,
        "default_approved": False,
        "polygons": [circle_polygon(113.9228, 22.5355, 3000.0)],
    },
    {
        "id": "SZ-CTRL-WEST-PORTS",
        "name": "蛇口—赤湾—妈湾港区管制区",
        "category": "CONTROLLED",
        "description": "深圳西部主要港口设施及周边审批管制示意区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "按港区公开位置生成覆盖性示意边界。",
        "source_name": "深圳市民用微轻型无人机管理暂行办法",
        "source_url": SOURCE_LOCAL_RULE,
        "effective_from": None,
        "effective_to": None,
        "default_approved": False,
        "polygons": [[
            [113.8730, 22.4660], [113.9260, 22.4660],
            [113.9260, 22.5050], [113.8730, 22.5050],
            [113.8730, 22.4660],
        ]],
    },
    {
        "id": "SZ-CTRL-YANTIAN-PORT",
        "name": "盐田港区管制区",
        "category": "CONTROLLED",
        "description": "盐田港口设施及周边审批管制示意区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "按港区公开位置生成覆盖性示意边界。",
        "source_name": "深圳市民用微轻型无人机管理暂行办法",
        "source_url": SOURCE_LOCAL_RULE,
        "effective_from": None,
        "effective_to": None,
        "default_approved": False,
        "polygons": [[
            [114.2420, 22.5480], [114.3000, 22.5480],
            [114.3000, 22.5900], [114.2420, 22.5900],
            [114.2420, 22.5480],
        ]],
    },
    {
        "id": "SZ-CTRL-DACHAN-BAY-PORT",
        "name": "大铲湾港区管制区",
        "category": "CONTROLLED",
        "description": "大铲湾港口设施及周边审批管制示意区域。",
        "geometry_accuracy": "approximate",
        "boundary_note": "按港区公开位置生成覆盖性示意边界。",
        "source_name": "深圳市民用微轻型无人机管理暂行办法",
        "source_url": SOURCE_LOCAL_RULE,
        "effective_from": None,
        "effective_to": None,
        "default_approved": False,
        "polygons": [circle_polygon(113.8535, 22.5640, 1700.0)],
    },
]


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def zone_is_active(zone: Dict, now: datetime | None = None) -> bool:
    current = now or datetime.now().astimezone()
    start = _parse_time(zone.get("effective_from"))
    end = _parse_time(zone.get("effective_to"))
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def default_approval_states() -> Dict[str, bool]:
    return {
        zone["id"]: bool(zone.get("default_approved", False))
        for zone in AIRSPACE_ZONES
        if zone["category"] == "CONTROLLED"
    }
