#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳天气联动配置。

只需修改本文件中的变量即可调整天气来源、刷新频率和停飞规则。
"""

# 是否启用天气联动。关闭后不会请求天气 API，也不会因天气暂停新任务派遣。
WEATHER_ENABLED = True

# 天气数据来源开关：
# True  = 调用高德 API，使用深圳真实天气；
# False = 不调用天气 API，使用下方手动设置的模拟天气。
USE_REAL_WEATHER = False

# 模拟天气参数，仅在 USE_REAL_WEATHER = False 时生效。
# 例如将 SIMULATED_WEATHER 改成“雷阵雨”，系统会按下方安全规则暂停新派遣。
SIMULATED_WEATHER = "晴"
SIMULATED_TEMPERATURE_C = 27.0
SIMULATED_HUMIDITY_PERCENT = 70.0
SIMULATED_WIND_DIRECTION = "东南"
SIMULATED_WIND_POWER = "≤3"

# 高德天气 API 使用 Web 服务 Key。留空时自动复用 app_config.py 中的
# AMAP_WEB_SERVICE_KEY；如需单独 Key，可直接粘贴到下方引号内。
WEATHER_API_KEY = ""

# 深圳市行政区划编码和界面名称。
WEATHER_CITY_ADCODE = "440300"
WEATHER_CITY_NAME = "深圳市"

# 天气轮询和数据有效期（现实秒）。高德实况并非秒级更新，不建议频繁请求。
WEATHER_REFRESH_SECONDS = 300.0
WEATHER_REQUEST_TIMEOUT_SECONDS = 8.0
WEATHER_STALE_AFTER_SECONDS = 900.0

# True：从未取得天气或最后成功数据已过期时，暂停派遣新无人机（安全优先）。
# False：天气接口不可用时仍允许派遣，但界面会显示数据异常。
PAUSE_DISPATCH_IF_WEATHER_UNAVAILABLE = True

# 只要天气描述包含以下任意关键词，就暂停派遣新无人机。
UNSAFE_WEATHER_KEYWORDS = (
    "雨", "雪", "雷", "冰雹", "雾", "霾", "沙尘", "台风", "龙卷",
)

# 高德风力等级允许的最大值。比如填 5 表示 5 级及以下允许，6 级起暂停派遣。
MAX_SAFE_WIND_LEVEL = 5.0

# 温度安全范围（摄氏度）。
MIN_SAFE_TEMPERATURE_C = -5.0
MAX_SAFE_TEMPERATURE_C = 45.0

# 是否让天气变化主动影响已经在途的无人机。
# False 时天气仍会暂停新派遣，但不会改变正在执行的任务。
INFLIGHT_WEATHER_RESPONSE_ENABLED = True

# 一般不适航天气（例如小雨、阵雨、雾、霾）下，在途无人机不立即中止：
# 系统会按该系数降低速度，并用同一系数重新计算剩余航程能耗。
# 数值越小，飞行越慢，估算耗电越高；建议保持在 0.5~1.0。
INFLIGHT_CAUTION_FLIGHT_FACTOR = 0.75

# 严重天气撤离到基地或换电站时使用的飞行系数。
INFLIGHT_EMERGENCY_FLIGHT_FACTOR = 0.60

# 命中以下任一关键词时，已经在途的任务进入严重天气应急处置：
# 未取货任务中止并安全返航；已携货任务前往可达基地/换电站落地交接。
INFLIGHT_SEVERE_WEATHER_KEYWORDS = (
    "中雨", "大雨", "暴雨", "雷", "冰雹", "冻雨", "雪",
    "沙尘", "台风", "龙卷", "浓雾",
)

# 风力达到该等级时触发在途应急撤离。应不低于新派遣的安全上限。
INFLIGHT_EMERGENCY_WIND_LEVEL = 6.0
