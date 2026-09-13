#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无人机能耗与换电站配置接口。

只需要修改本文件中的变量即可调整能耗、最低安全电量、换电时间和换电站坐标。
坐标统一使用 WGS84（经度 lon、纬度 lat），与 Cesium 和后端规划器一致。
"""

# 水平飞行基础能耗：空载、无额外风阻时每飞行 100km 消耗的电量百分比。
# 例如 220 表示平均每公里消耗约 2.2%，理论满电水平航程约 45km。
BASE_CONSUMPTION_PERCENT_PER_100_KM = 220.0

# 每增加 1kg 载重，水平及升降能耗增加的比例。0.06 表示每公斤增加 6%。
PAYLOAD_ENERGY_FACTOR_PER_KG = 0.06

# 上升/下降 100m 对应的额外电量百分比。
ASCENT_CONSUMPTION_PERCENT_PER_100_M = 2.5
DESCENT_CONSUMPTION_PERCENT_PER_100_M = 0.4

# 天气基础安全系数，以及最强逆风时的最大能耗倍率。
# 当前项目以 UAV.wind_factor 表示风况：1.0 为正常，越低表示逆风越明显。
WEATHER_SAFETY_MULTIPLIER = 1.05
MAX_HEADWIND_ENERGY_MULTIPLIER = 1.45

# 每一航段完成后必须保留的安全电量；低于该余量会提前寻找换电站。
BATTERY_RESERVE_PERCENT = 12.0

# 低于该电量的空闲无人机不参加新任务分配。
MIN_DISPATCH_BATTERY_PERCENT = 15.0

# 任务结束后若没有可立即衔接的任务，电量低于该值时优先恢复到基地/换电站。
POST_TASK_RECOVERY_THRESHOLD_PERCENT = 35.0

# 没有待执行任务时，无人机在任务终点等待多久后自动返回所属基地（仿真秒）。
IDLE_RETURN_DELAY_SECONDS = 20.0

# 暂时无安全方案的任务不会直接失败，而是按该间隔自动重新尝试调度（现实秒）。
BLOCKED_TASK_RETRY_SECONDS = 8.0

# 安全返航路线暂时不可用时，悬停/原地停放后重新规划的间隔（现实秒）。
SAFE_RECOVERY_RETRY_SECONDS = 8.0

# 换电使用“仿真秒”。仿真倍速为 4x 时，20 秒换电约需现实 5 秒。
BATTERY_SWAP_TIME_SECONDS = 20.0
BATTERY_AFTER_SWAP_PERCENT = 100.0

# 基地充电参数。充电时间按仿真秒计算，并随仿真倍速变化。
# 充电时间按缺少的电量比例折算，例如 50% 电量充至 100% 约需一半时间。
BASE_CHARGE_TIME_SECONDS = 120.0
BASE_CHARGE_TARGET_PERCENT = 100.0

# 允许紧急/等待任务调用正在基地充电、且已达到最低出勤电量的无人机。
ALLOW_BASE_CHARGE_INTERRUPTION = True

# 电量不足时按地理绕行距离排序，只对最近的若干站执行完整建筑避障规划。
MAX_STATION_CANDIDATES_TO_PLAN = 8

# 单个“起飞→取货”或“取货→终点”航段允许的最多换电次数。
MAX_SWAP_STOPS_PER_LEG = 4

# 30 个深圳换电站初始点。可以直接修改、删除或继续添加。
# id 必须唯一；name 用于地图标签；lon/lat 为 WGS84 坐标。
# 当前坐标已避开 airspace_config.py 中全部绝对禁飞区和审批管制区。
BATTERY_STATIONS = [
    {"id": "SWAP-01", "name": "新安换电站", "lon": 113.91075, "lat": 22.5740},
    {"id": "SWAP-02", "name": "深圳湾东换电站", "lon": 113.9580, "lat": 22.5160},
    {"id": "SWAP-03", "name": "科技园北换电站", "lon": 113.9560, "lat": 22.57025},
    {"id": "SWAP-04", "name": "西丽换电站", "lon": 113.9547, "lat": 22.5894},
    {"id": "SWAP-05", "name": "后海换电站", "lon": 113.9730, "lat": 22.5210},
    {"id": "SWAP-06", "name": "宝安中心换电站", "lon": 113.8845, "lat": 22.5558},
    {"id": "SWAP-07", "name": "西乡换电站", "lon": 113.8797, "lat": 22.5798},
    {"id": "SWAP-08", "name": "航城北换电站", "lon": 113.9000, "lat": 22.6920},
    {"id": "SWAP-09", "name": "沙井换电站", "lon": 113.8068, "lat": 22.7296},
    {"id": "SWAP-10", "name": "松岗换电站", "lon": 113.83765, "lat": 22.77385},
    {"id": "SWAP-11", "name": "石岩换电站", "lon": 113.93635, "lat": 22.68165},
    {"id": "SWAP-12", "name": "公明换电站", "lon": 113.8878, "lat": 22.7795},
    {"id": "SWAP-13", "name": "光明中心换电站", "lon": 113.9358, "lat": 22.7487},
    {"id": "SWAP-14", "name": "民治换电站", "lon": 114.04005, "lat": 22.6208},
    {"id": "SWAP-15", "name": "大浪换电站", "lon": 114.0018, "lat": 22.6752},
    {"id": "SWAP-16", "name": "观澜换电站", "lon": 114.05565, "lat": 22.7215},
    {"id": "SWAP-17", "name": "安托山换电站", "lon": 114.0020, "lat": 22.5540},
    {"id": "SWAP-18", "name": "福田中心换电站", "lon": 114.0570, "lat": 22.5417},
    {"id": "SWAP-19", "name": "梅林换电站", "lon": 114.0605, "lat": 22.5702},
    {"id": "SWAP-20", "name": "罗湖中心换电站", "lon": 114.1108, "lat": 22.5488},
    {"id": "SWAP-21", "name": "笋岗换电站", "lon": 114.1150, "lat": 22.5722},
    {"id": "SWAP-22", "name": "莲塘换电站", "lon": 114.1708, "lat": 22.5620},
    {"id": "SWAP-23", "name": "布吉换电站", "lon": 114.1218, "lat": 22.6135},
    {"id": "SWAP-24", "name": "坂田换电站", "lon": 114.0707, "lat": 22.6350},
    {"id": "SWAP-25", "name": "平湖换电站", "lon": 114.13185, "lat": 22.69745},
    {"id": "SWAP-26", "name": "横岗换电站", "lon": 114.20185, "lat": 22.6510},
    {"id": "SWAP-27", "name": "龙岗中心换电站", "lon": 114.25135, "lat": 22.72165},
    {"id": "SWAP-28", "name": "坪地换电站", "lon": 114.3090, "lat": 22.7715},
    {"id": "SWAP-29", "name": "坪山中心换电站", "lon": 114.35035, "lat": 22.6925},
    {"id": "SWAP-30", "name": "大鹏换电站", "lon": 114.4795, "lat": 22.5968},
]
