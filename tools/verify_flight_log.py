#!/usr/bin/env python3
"""离线验证完成任务飞行日志，不读取或修改项目 runtime 日志。"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_log import FlightLogManager


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        manager = FlightLogManager(Path(directory))
        now = time.time()
        task = {
            "id": "TASK-LOGTEST",
            "origin": {"name": "深圳北站", "lon": 114.02, "lat": 22.61},
            "destination": {"name": "人才公园", "lon": 113.94, "lat": 22.51},
            "delivery_type": "MEDICAL",
            "delivery_label": "医疗物资",
            "priority": "EMERGENCY",
            "payload_kg": 2.0,
            "deadline_minutes": 30,
            "cruise_alt": 80.0,
            "data_mode": "shenzhen",
            "created_at": now - 10,
            "started_at": now,
            "route_revision": 1,
            "route_plan": {
                "revision": 1,
                "reason": "测试路线",
                "takeoff_to_pickup": {"route": []},
                "pickup_to_destination": {"route": []},
            },
            "swap_count": 0,
        }
        uav = {
            "id": "UAV-01", "base": "TEST_BASE", "max_payload_kg": 5.0,
            "cruise_speed_mps": 15.0, "lon": 114.02, "lat": 22.61, "alt": 0.0,
            "battery": 90.0, "wind_factor": 1.0, "phase": "TO_PICKUP",
        }
        weather = {"weather": "晴", "dispatch_allowed": True, "source_mode": "simulated"}

        manager.start_or_resume(task, uav, weather)
        manager.record_event("TASK_ASSIGNED", task["id"], uav, "任务已分配")
        uav.update({"lon": 114.01, "lat": 22.60, "alt": 80.0, "battery": 85.0, "phase": "DELIVERING"})
        manager.record_position(task, uav, force=True)
        task.update({"status": "COMPLETED", "completed_at": now + 12, "cargo_status": "DELIVERED"})
        summary = manager.complete(task, uav, weather)

        assert summary is not None
        assert summary["task_id"] == task["id"]
        assert float(summary["actual_distance_m"]) > 0
        assert float(summary["gross_energy_used_percent"]) == 5.0
        detail_path = manager.get_log_path(task["id"])
        assert detail_path is not None and detail_path.exists()
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        assert detail["status"] == "COMPLETED"
        assert len(detail["actual_track"]) >= 3
        assert detail["events"][0]["type"] == "TASK_ASSIGNED"
        assert detail["dispatch_weather"]["weather"] == "晴"
        assert detail["completion_weather"]["weather"] == "晴"

        with manager.summary_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1 and rows[0]["task_id"] == task["id"]
        assert manager.list_summaries()[0]["task_id"] == task["id"]
        print("Flight log verification passed.")


if __name__ == "__main__":
    main()
