#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目本地配置。

此文件不会由 FastAPI 作为静态文件公开。填写高德 Web 服务 Key 后，地点搜索会
优先使用高德深圳 POI；留空时前端自动回退到 Cesium ion 地理编码。
"""

AMAP_WEB_SERVICE_KEY = "7b3cf52a674a7a84452b6a53f0302d83"

# OpenAI 配置只在后端读取，不会通过网页接口返回给浏览器。
# 把 Key 粘贴到引号内即可同时启用单任务和批量任务 AI 解析。
OPENAI_API_KEY = "sk-520ba9f53775448fb42606142b4d1d35"
OPENAI_MODEL = "deepseek-v4-flash"
OPENAI_BASE_URL = "https://api.deepseek.com"
