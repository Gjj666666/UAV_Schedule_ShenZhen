#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径规划性能参数；仅调整计算方式，不降低建筑和禁飞区安全约束。"""

# 路线缓存会把起终点、航层、建筑索引版本和全部空域边界共同作为键。
ROUTE_CACHE_MAX_ITEMS = 2000
ROUTE_CACHE_TTL_SECONDS = 1200.0
FAILED_ROUTE_CACHE_TTL_SECONDS = 45.0

# 禁飞区在进入 A* 前栅格化；之后每个节点只进行集合查询。
RASTERIZE_CUSTOM_OBSTACLES = True

# 安全等待任务采用指数退避，避免无解任务频繁占满 CPU。
BLOCKED_RETRY_INITIAL_SECONDS = 20.0
BLOCKED_RETRY_MULTIPLIER = 2.0
BLOCKED_RETRY_MAX_SECONDS = 300.0
