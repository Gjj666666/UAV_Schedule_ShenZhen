#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 CSV、JSON、XLSX 批量任务解析和 AI 批量结构。"""
from __future__ import annotations

import io
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_dispatcher import AIBatchTaskDrafts, AITaskDraft
from batch_tasks import parse_task_file


def _xlsx_bytes(rows):
    def cell(column, row_number, value):
        escaped = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<c r="{column}{row_number}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    columns = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    xml_rows = []
    for row_number, row in enumerate(rows, start=1):
        xml_rows.append(
            f'<row r="{row_number}">' + "".join(cell(columns[index], row_number, value) for index, value in enumerate(row)) + "</row>"
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(xml_rows)
        + "</sheetData></worksheet>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet.encode("utf-8"))
    return output.getvalue()


def main():
    header = ["起点", "终点", "配送类型", "优先级", "重量kg", "时限分钟", "巡航高度"]
    task = ["深圳北站", "人才公园", "医疗物资", "紧急", "2", "30", "80"]
    csv_raw = (",".join(header) + "\n" + ",".join(task) + "\n").encode("utf-8")
    csv_result = parse_task_file("tasks.csv", csv_raw)
    csv_draft = csv_result["drafts"][0]
    assert csv_draft["payload_kg"] == 2.0
    assert csv_draft["delivery_type"] == "MEDICAL"
    assert csv_draft["priority"] == "EMERGENCY"
    assert not csv_draft["errors"]

    json_result = parse_task_file(
        "tasks.json",
        ('[{"origin":{"name":"深圳北站","lon":114.024436,"lat":22.612652},'
         '"destination":{"name":"人才公园","lon":113.938252,"lat":22.515095},"payload_kg":2}]').encode("utf-8"),
    )
    assert json_result["total"] == 1
    assert json_result["drafts"][0]["payload_kg"] == 2.0
    assert json_result["drafts"][0]["origin"]["lon"] == 114.024436

    xlsx_result = parse_task_file("tasks.xlsx", _xlsx_bytes([header, task]))
    assert xlsx_result["total"] == 1
    assert xlsx_result["drafts"][0]["deadline_minutes"] == 30
    assert not xlsx_result["drafts"][0]["errors"]

    batch = AIBatchTaskDrafts(tasks=[AITaskDraft(
        origin_text="深圳北站",
        destination_text="人才公园",
        delivery_type="MEDICAL",
        priority="EMERGENCY",
        payload_kg=2,
        deadline_minutes=30,
        cruise_alt=80,
        confidence="HIGH",
        task_summary="急救药品配送",
        assumptions=[],
    )])
    assert batch.tasks[0].payload_kg == 2.0

    print("PASS: CSV、JSON、XLSX 批量解析正常，2kg/紧急/医疗字段保持正确。")
    print("PASS: AI 批量结构化任务模型校验正常。")


if __name__ == "__main__":
    main()
