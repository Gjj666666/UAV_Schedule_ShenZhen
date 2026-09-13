#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量任务文件解析：CSV、JSON 和无需第三方依赖的基础 XLSX。"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET


MAX_BATCH_TASKS = 200

FIELD_ALIASES = {
    "origin_text": ["origin", "origin_text", "起点", "出发地", "取货点", "起飞点"],
    "origin_lon": ["origin_lon", "start_lon", "起点经度", "出发地经度", "取货点经度"],
    "origin_lat": ["origin_lat", "start_lat", "起点纬度", "出发地纬度", "取货点纬度"],
    "destination_text": ["destination", "destination_text", "终点", "目的地", "送达点"],
    "destination_lon": ["destination_lon", "dest_lon", "end_lon", "终点经度", "目的地经度"],
    "destination_lat": ["destination_lat", "dest_lat", "end_lat", "终点纬度", "目的地纬度"],
    "delivery_type": ["delivery_type", "type", "任务类型", "配送类型", "货物类型"],
    "priority": ["priority", "优先级", "紧要程度"],
    "payload_kg": ["payload_kg", "payload", "weight", "重量", "重量kg", "载重", "载重kg"],
    "deadline_minutes": ["deadline_minutes", "deadline", "时限", "时限分钟", "截止时间"],
    "cruise_alt": ["cruise_alt", "altitude", "航高", "巡航高度", "首选航高"],
    "data_mode": ["data_mode", "数据模式", "建筑模式"],
}

DELIVERY_TYPES = {
    "MEDICAL": "MEDICAL", "医疗": "MEDICAL", "医疗物资": "MEDICAL", "急救药品": "MEDICAL",
    "EMERGENCY": "EMERGENCY", "应急": "EMERGENCY", "应急物资": "EMERGENCY",
    "PARCEL": "PARCEL", "普通": "PARCEL", "普通包裹": "PARCEL", "包裹": "PARCEL",
    "COLD_CHAIN": "COLD_CHAIN", "冷链": "COLD_CHAIN", "冷链物资": "COLD_CHAIN",
    "DOCUMENT": "DOCUMENT", "文件": "DOCUMENT", "文档": "DOCUMENT",
}

PRIORITIES = {
    "EMERGENCY": "EMERGENCY", "紧急": "EMERGENCY", "特急": "EMERGENCY",
    "HIGH": "HIGH", "高": "HIGH", "高优先级": "HIGH",
    "NORMAL": "NORMAL", "普通": "NORMAL", "正常": "NORMAL", "中": "NORMAL",
    "LOW": "LOW", "低": "LOW", "低优先级": "LOW",
}

DATA_MODES = {"shenzhen": "shenzhen", "深圳": "shenzhen", "cesium": "cesium", "custom": "custom", "自定义": "custom"}


class BatchFileError(ValueError):
    pass


def _clean_key(value) -> str:
    return re.sub(r"[\s_（）()\-]+", "", str(value or "").strip().lower())


def _alias_lookup(row: Dict, canonical: str):
    normalized = {_clean_key(key): value for key, value in row.items()}
    for alias in FIELD_ALIASES[canonical]:
        key = _clean_key(alias)
        if key in normalized and str(normalized[key] if normalized[key] is not None else "").strip() != "":
            return normalized[key]
    return None


def _number(value, default: float, label: str, errors: List[str]) -> float:
    if value is None or str(value).strip() == "":
        return default
    text = re.sub(r"[^0-9.+-]", "", str(value).strip())
    try:
        return float(text)
    except (TypeError, ValueError):
        errors.append(f"{label}不是有效数字：{value}")
        return default


def _coordinate(value, label: str, minimum: float, maximum: float, errors: List[str]) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    number = _number(value, 0.0, label, errors)
    if not minimum <= number <= maximum:
        errors.append(f"{label}超出范围：{number}")
    return number


def normalize_task_row(row: Dict, source_row: int) -> Dict:
    errors: List[str] = []
    warnings: List[str] = []
    origin_nested = row.get("origin") if isinstance(row.get("origin"), dict) else None
    destination_nested = row.get("destination") if isinstance(row.get("destination"), dict) else None
    origin_value = origin_nested.get("name") if origin_nested else _alias_lookup(row, "origin_text")
    destination_value = destination_nested.get("name") if destination_nested else _alias_lookup(row, "destination_text")
    origin_text = str(origin_value or "").strip()
    destination_text = str(destination_value or "").strip()
    origin_lon = _coordinate(
        origin_nested.get("lon") if origin_nested else _alias_lookup(row, "origin_lon"),
        "起点经度", -180, 180, errors,
    )
    origin_lat = _coordinate(
        origin_nested.get("lat") if origin_nested else _alias_lookup(row, "origin_lat"),
        "起点纬度", -90, 90, errors,
    )
    destination_lon = _coordinate(
        destination_nested.get("lon") if destination_nested else _alias_lookup(row, "destination_lon"),
        "终点经度", -180, 180, errors,
    )
    destination_lat = _coordinate(
        destination_nested.get("lat") if destination_nested else _alias_lookup(row, "destination_lat"),
        "终点纬度", -90, 90, errors,
    )

    if (origin_lon is None) != (origin_lat is None):
        errors.append("起点经纬度必须同时填写")
    if (destination_lon is None) != (destination_lat is None):
        errors.append("终点经纬度必须同时填写")
    if origin_lon is None and not origin_text:
        errors.append("缺少起点名称或起点经纬度")
    if destination_lon is None and not destination_text:
        errors.append("缺少终点名称或终点经纬度")

    delivery_raw = str(_alias_lookup(row, "delivery_type") or "PARCEL").strip()
    priority_raw = str(_alias_lookup(row, "priority") or "NORMAL").strip()
    mode_raw = str(_alias_lookup(row, "data_mode") or "shenzhen").strip()
    delivery_type = DELIVERY_TYPES.get(delivery_raw.upper()) or DELIVERY_TYPES.get(delivery_raw)
    priority = PRIORITIES.get(priority_raw.upper()) or PRIORITIES.get(priority_raw)
    data_mode = DATA_MODES.get(mode_raw.lower()) or DATA_MODES.get(mode_raw)
    if not delivery_type:
        errors.append(f"无法识别配送类型：{delivery_raw}")
        delivery_type = "PARCEL"
    if not priority:
        errors.append(f"无法识别优先级：{priority_raw}")
        priority = "NORMAL"
    if not data_mode:
        errors.append(f"无法识别数据模式：{mode_raw}")
        data_mode = "shenzhen"

    payload = _number(_alias_lookup(row, "payload_kg"), 1.0, "重量", errors)
    deadline = int(round(_number(_alias_lookup(row, "deadline_minutes"), 45, "时限", errors)))
    altitude = int(round(_number(_alias_lookup(row, "cruise_alt"), 80, "巡航高度", errors)))
    if _alias_lookup(row, "payload_kg") is None:
        warnings.append("未填写重量，使用默认值 1kg")
    if _alias_lookup(row, "deadline_minutes") is None:
        warnings.append("未填写时限，使用默认值 45 分钟")
    if _alias_lookup(row, "cruise_alt") is None:
        warnings.append("未填写巡航高度，使用默认值 80m")

    draft = {
        "source_row": source_row,
        "origin_text": origin_text,
        "destination_text": destination_text,
        "delivery_type": delivery_type,
        "priority": priority,
        "payload_kg": payload,
        "deadline_minutes": deadline,
        "cruise_alt": altitude,
        "data_mode": data_mode,
        "warnings": warnings,
        "errors": errors,
    }
    if origin_lon is not None and origin_lat is not None:
        draft["origin"] = {"name": origin_text or f"{origin_lon:.6f},{origin_lat:.6f}", "lon": origin_lon, "lat": origin_lat}
    if destination_lon is not None and destination_lat is not None:
        draft["destination"] = {
            "name": destination_text or f"{destination_lon:.6f},{destination_lat:.6f}",
            "lon": destination_lon,
            "lat": destination_lat,
        }
    return draft


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BatchFileError("CSV 编码无法识别，请保存为 UTF-8 或 GB18030。")


def _rows_from_csv(raw: bytes) -> List[Dict]:
    reader = csv.DictReader(io.StringIO(_decode_csv(raw)))
    if not reader.fieldnames:
        raise BatchFileError("CSV 缺少表头。")
    return [dict(row) for row in reader if any(str(value or "").strip() for value in row.values())]


def _rows_from_json(raw: bytes) -> List[Dict]:
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise BatchFileError(f"JSON 解析失败：{exc}") from exc
    rows = obj.get("tasks") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BatchFileError('JSON 必须是任务对象数组，或形如 {"tasks": [...]}。')
    return rows


def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    value = 0
    for ch in letters.group(0):
        value = value * 26 + ord(ch) - 64
    return value - 1


def _rows_from_xlsx(raw: bytes) -> List[Dict]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise BatchFileError("XLSX 文件损坏或格式不正确。") from exc
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared: List[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root.findall("m:si", ns):
            shared.append("".join(node.text or "" for node in item.iterfind(".//m:t", ns)))

    sheet_name = "xl/worksheets/sheet1.xml"
    if sheet_name not in archive.namelist():
        sheets = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        if not sheets:
            raise BatchFileError("XLSX 中没有可读取的工作表。")
        sheet_name = sheets[0]
    root = ET.fromstring(archive.read(sheet_name))
    matrix: List[List[object]] = []
    for row_node in root.findall(".//m:sheetData/m:row", ns):
        values: Dict[int, object] = {}
        for cell in row_node.findall("m:c", ns):
            index = _column_index(cell.attrib.get("r", "A1"))
            cell_type = cell.attrib.get("t")
            value_node = cell.find("m:v", ns)
            if cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.iterfind(".//m:t", ns))
            elif value_node is None:
                value = ""
            elif cell_type == "s":
                shared_index = int(value_node.text or 0)
                value = shared[shared_index] if 0 <= shared_index < len(shared) else ""
            else:
                value = value_node.text or ""
            values[index] = value
        if values:
            matrix.append([values.get(i, "") for i in range(max(values) + 1)])
    if not matrix:
        raise BatchFileError("XLSX 工作表为空。")
    headers = [str(value).strip() for value in matrix[0]]
    if not any(headers):
        raise BatchFileError("XLSX 第一行必须是表头。")
    return [
        {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers)) if headers[i]}
        for row in matrix[1:]
        if any(str(value or "").strip() for value in row)
    ]


def parse_task_file(filename: str, raw: bytes) -> Dict:
    lower = filename.lower()
    if lower.endswith(".csv"):
        rows = _rows_from_csv(raw)
        file_type = "csv"
    elif lower.endswith(".json"):
        rows = _rows_from_json(raw)
        file_type = "json"
    elif lower.endswith(".xlsx"):
        rows = _rows_from_xlsx(raw)
        file_type = "xlsx"
    else:
        raise BatchFileError("仅支持 .xlsx、.csv、.json 任务文件。")
    if not rows:
        raise BatchFileError("文件中没有任务数据。")
    if len(rows) > MAX_BATCH_TASKS:
        raise BatchFileError(f"单次最多导入 {MAX_BATCH_TASKS} 条任务，当前文件有 {len(rows)} 条。")
    drafts = [normalize_task_row(row, index + 2) for index, row in enumerate(rows)]
    return {
        "file_type": file_type,
        "total": len(drafts),
        "drafts": drafts,
        "parse_error_count": sum(bool(draft["errors"]) for draft in drafts),
    }
