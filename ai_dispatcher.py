#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型任务理解层。

本模块只把自然语言解析成现有调度系统已经支持的任务字段，不直接生成经纬度、
不绕过规则校验，也不直接创建任务。地点坐标仍由地图地点服务或用户选点确认。
"""
from __future__ import annotations

import importlib.util
import os
from typing import Dict, List, Literal

from pydantic import BaseModel, Field

try:
    import app_config
except ImportError:  # 保留部署时仅使用环境变量的方式
    app_config = None


DEFAULT_MODEL = "gpt-5.6"


class AITaskDraft(BaseModel):
    origin_text: str = Field(min_length=1, max_length=120)
    destination_text: str = Field(min_length=1, max_length=120)
    delivery_type: Literal["MEDICAL", "EMERGENCY", "PARCEL", "COLD_CHAIN", "DOCUMENT"]
    priority: Literal["LOW", "NORMAL", "HIGH", "EMERGENCY"]
    payload_kg: float = Field(gt=0, le=5)
    deadline_minutes: int = Field(ge=10, le=240)
    cruise_alt: Literal[60, 80, 100, 120]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    task_summary: str = Field(min_length=1, max_length=240)
    assumptions: List[str] = Field(max_length=8)


class AIBatchTaskDrafts(BaseModel):
    tasks: List[AITaskDraft] = Field(min_length=1, max_length=50)


class AIConfigError(RuntimeError):
    pass


class AIServiceError(RuntimeError):
    pass


def _config_value(name: str, default: str = "") -> str:
    """环境变量可覆盖代码配置；两者都没有时使用默认值。"""
    environment_value = os.getenv(name, "").strip()
    if environment_value:
        return environment_value
    file_value = getattr(app_config, name, "") if app_config is not None else ""
    return str(file_value or default).strip()


def get_ai_status() -> Dict:
    api_key = _config_value("OPENAI_API_KEY")
    return {
        "configured": bool(api_key),
        "sdk_available": importlib.util.find_spec("openai") is not None,
        "provider": "OpenAI Responses API",
        "model": _config_value("OPENAI_MODEL", DEFAULT_MODEL),
        "coordinate_source": "高德深圳 POI / Cesium ion / 地图选点 / 用户输入经纬度",
        "confirmation_required": True,
    }


def parse_task_text(text: str) -> Dict:
    api_key = _config_value("OPENAI_API_KEY")
    if not api_key:
        raise AIConfigError("未配置 OPENAI_API_KEY；AI 助手不会影响原有手动任务流程。")
    if importlib.util.find_spec("openai") is None:
        raise AIConfigError("未安装 openai Python SDK，请重新安装 requirements.txt。")

    from openai import OpenAI

    model = _config_value("OPENAI_MODEL", DEFAULT_MODEL)
    client_options = {"api_key": api_key, "timeout": 45.0, "max_retries": 1}
    base_url = _config_value("OPENAI_BASE_URL")
    if base_url:
        client_options["base_url"] = base_url

    system_prompt = """
你是深圳低空无人机配送系统的任务录入助手。把用户自然语言严格转换为给定结构。

规则：
1. 只提取地点名称或用户明确提供的“经度,纬度”文本，绝不猜测或编造坐标。
2. delivery_type 只能是 MEDICAL、EMERGENCY、PARCEL、COLD_CHAIN、DOCUMENT。
3. priority 只能是 LOW、NORMAL、HIGH、EMERGENCY。急救、救命、灾害等任务通常为 EMERGENCY。
4. 载重范围 0~5kg；时限 10~240 分钟；巡航高度只选择 60、80、100、120m。用户明确给出的重量和时限数字必须原样提取，例如“2公斤”必须是 2.0，不能改成默认值。
5. 用户未说明时，默认普通包裹、NORMAL、1kg、45分钟、80m，并把默认项写入 assumptions。
6. 地点含糊时不要擅自补成具体地址，保留原名称，将置信度设为 LOW 并在 assumptions 说明。
7. task_summary 使用简洁中文，只解释提取结果和主要调度意图，不输出思维过程。
8. 本步骤只生成草案，最终仍由用户确认、地理编码和后端规则校验。
""".strip()

    try:
        client = OpenAI(**client_options)
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text.strip()},
            ],
            text_format=AITaskDraft,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise AIServiceError("模型没有返回可用的结构化任务草案。")
        draft = parsed.model_dump()
        draft["origin_text"] = draft["origin_text"].strip()
        draft["destination_text"] = draft["destination_text"].strip()
        return {"draft": draft, "model": model, "requires_confirmation": True}
    except AIServiceError:
        raise
    except Exception as exc:
        message = str(exc).strip().replace(api_key, "***")
        raise AIServiceError(f"大模型解析失败：{message[:360]}") from exc


def parse_tasks_text(text: str) -> Dict:
    """把包含多条任务的自然语言一次解析成独立任务草案列表。"""
    api_key = _config_value("OPENAI_API_KEY")
    if not api_key:
        raise AIConfigError("未配置 OPENAI_API_KEY；仍可使用 CSV、XLSX 或 JSON 批量导入。")
    if importlib.util.find_spec("openai") is None:
        raise AIConfigError("未安装 openai Python SDK，请重新安装 requirements.txt。")

    from openai import OpenAI

    model = _config_value("OPENAI_MODEL", DEFAULT_MODEL)
    client_options = {"api_key": api_key, "timeout": 60.0, "max_retries": 1}
    base_url = _config_value("OPENAI_BASE_URL")
    if base_url:
        client_options["base_url"] = base_url

    system_prompt = """
你是深圳低空无人机配送系统的批量任务录入助手。用户会一次输入多条飞行任务，
你必须逐条拆分，按出现顺序返回 tasks 数组；不能合并任务，也不能遗漏任务。

每条任务规则：
1. 只提取地点名称或明确给出的“经度,纬度”文本，绝不猜测坐标。
2. delivery_type 只能是 MEDICAL、EMERGENCY、PARCEL、COLD_CHAIN、DOCUMENT。
3. priority 只能是 LOW、NORMAL、HIGH、EMERGENCY；急救、救命、灾害通常为 EMERGENCY。
4. 重量、时限必须逐条读取原数字。“2公斤”必须输出 2.0，不能替换为默认值。
5. 载重 0~5kg；时限 10~240分钟；巡航高度只选 60、80、100、120m。
6. 未说明时默认普通包裹、NORMAL、1kg、45分钟、80m，并将默认项写入 assumptions。
7. 地点含糊时保留原名称，把 confidence 设为 LOW，不得把上一条任务的字段复制过来。
8. 本步骤只生成批量草案，后续仍需地点服务解析、预览确认和后端安全校验。
""".strip()

    try:
        client = OpenAI(**client_options)
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text.strip()},
            ],
            text_format=AIBatchTaskDrafts,
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.tasks:
            raise AIServiceError("模型没有返回可用的批量任务草案。")
        drafts = []
        for index, task in enumerate(parsed.tasks, start=1):
            draft = task.model_dump()
            draft["origin_text"] = draft["origin_text"].strip()
            draft["destination_text"] = draft["destination_text"].strip()
            draft["source_row"] = index
            draft["data_mode"] = "shenzhen"
            draft["warnings"] = list(draft.get("assumptions") or [])
            draft["errors"] = []
            drafts.append(draft)
        return {"drafts": drafts, "count": len(drafts), "model": model, "requires_confirmation": True}
    except AIServiceError:
        raise
    except Exception as exc:
        message = str(exc).strip().replace(api_key, "***")
        raise AIServiceError(f"大模型批量解析失败：{message[:360]}") from exc
