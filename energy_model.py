#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于实际规划航点的无人机电量估算。"""
from __future__ import annotations

from typing import Dict, Sequence

from energy_config import (
    ASCENT_CONSUMPTION_PERCENT_PER_100_M,
    BASE_CONSUMPTION_PERCENT_PER_100_KM,
    DESCENT_CONSUMPTION_PERCENT_PER_100_M,
    MAX_HEADWIND_ENERGY_MULTIPLIER,
    PAYLOAD_ENERGY_FACTOR_PER_KG,
    WEATHER_SAFETY_MULTIPLIER,
)
from planner import haversine_m


def weather_energy_multiplier(wind_factor: float = 1.0) -> float:
    """把现有速度风力系数转换成能耗倍率。"""
    normalized = max(0.0, min(1.0, float(wind_factor)))
    headwind = 1.0 + (1.0 - normalized) * (MAX_HEADWIND_ENERGY_MULTIPLIER - 1.0)
    return WEATHER_SAFETY_MULTIPLIER * headwind


def movement_energy_percent(
    horizontal_m: float,
    vertical_delta_m: float,
    payload_kg: float = 0.0,
    wind_factor: float = 1.0,
) -> float:
    horizontal = max(0.0, float(horizontal_m)) / 100_000.0 * BASE_CONSUMPTION_PERCENT_PER_100_KM
    vertical = float(vertical_delta_m)
    if vertical >= 0:
        altitude = vertical / 100.0 * ASCENT_CONSUMPTION_PERCENT_PER_100_M
    else:
        altitude = abs(vertical) / 100.0 * DESCENT_CONSUMPTION_PERCENT_PER_100_M
    payload_multiplier = 1.0 + max(0.0, float(payload_kg)) * PAYLOAD_ENERGY_FACTOR_PER_KG
    return (horizontal + altitude) * payload_multiplier * weather_energy_multiplier(wind_factor)


def route_energy_percent(route: Sequence[Dict], payload_kg: float = 0.0, wind_factor: float = 1.0) -> float:
    total = 0.0
    for start, end in zip(route or [], (route or [])[1:]):
        total += movement_energy_percent(
            haversine_m(start, end),
            float(end.get("alt", 0.0)) - float(start.get("alt", 0.0)),
            payload_kg,
            wind_factor,
        )
    return total


def geodesic_energy_percent(start: Dict, end: Dict, payload_kg: float = 0.0, wind_factor: float = 1.0) -> float:
    """调度初筛使用的直线下界；最终决策仍使用 A* 实际航点。"""
    return movement_energy_percent(haversine_m(start, end), 0.0, payload_kg, wind_factor)

