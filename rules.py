#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo 级可执行规则库。

真实系统中这里应替换为权威法规、运行许可、动态空域和企业 SOP。
当前只放少量清晰硬规则，避免把“规则”藏在 LLM 文本里。
"""
from __future__ import annotations

from typing import Dict, List

DEMO_RULES = [
    {"id": "ALTITUDE", "text": "Demo 规划高度限制为 50~120m；规划器可在标准高度层中自动选择更合适航层。"},
    {"id": "PAYLOAD", "text": "标准模拟物流多旋翼最大任务载重 5kg。"},
    {"id": "BATTERY_RESERVE", "text": "调度前估算任务能耗，并保留约 18% 电量余度。"},
    {"id": "NO_FLY", "text": "自定义 no_fly GeoJSON 视为不可穿越空域。"},
    {"id": "BUILDING_CLEARANCE", "text": "深圳建筑 Shapefile 的 Height + 15m 垂直余量低于候选航层时允许飞越，否则该建筑参与 A* 绕障。"},
    {"id": "CONFLICT", "text": "Demo 冲突阈值：水平小于100m且垂直小于22m时触发冲突消解。"},
]


def validate_task(task: Dict) -> List[str]:
    errors = []
    if not 50 <= float(task.get("cruise_alt", 0)) <= 120:
        errors.append("巡航高度必须在 50~120m。")
    if not 0 < float(task.get("payload_kg", 0)) <= 5:
        errors.append("当前模拟机型任务载重必须在 0~5kg。")
    if task.get("origin") == task.get("destination"):
        errors.append("起点和终点不能完全相同。")
    return errors
