#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务飞行日志配置。"""

# 是否保存完成任务的详细飞行日志。
FLIGHT_LOG_ENABLED = True

# 实际轨迹采样条件：满足时间间隔或位移距离任一条件就记录一个点。
FLIGHT_LOG_SAMPLE_INTERVAL_SECONDS = 1.0
FLIGHT_LOG_SAMPLE_MIN_DISTANCE_M = 8.0

# 单个任务最多保存的实际轨迹点，防止超长仿真无限增大文件。
FLIGHT_LOG_MAX_TRACK_POINTS = 20000

# 完成日志目录和 CSV 汇总文件名，均相对于项目 runtime 目录。
FLIGHT_LOG_DIRECTORY_NAME = "flight_logs"
FLIGHT_LOG_SUMMARY_FILENAME = "flight_summary.csv"
