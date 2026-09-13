#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按任务汇总无人机规划、轨迹、能耗、天气和事件并持久化。"""
from __future__ import annotations

import copy
import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

from flight_log_config import (
    FLIGHT_LOG_DIRECTORY_NAME,
    FLIGHT_LOG_ENABLED,
    FLIGHT_LOG_MAX_TRACK_POINTS,
    FLIGHT_LOG_SAMPLE_INTERVAL_SECONDS,
    FLIGHT_LOG_SAMPLE_MIN_DISTANCE_M,
    FLIGHT_LOG_SUMMARY_FILENAME,
)


SUMMARY_FIELDS = [
    "task_id", "status", "uav_ids", "origin", "destination", "delivery_type",
    "priority", "payload_kg", "started_at", "completed_at", "duration_seconds",
    "actual_distance_m", "max_altitude_m", "battery_start_percent",
    "battery_end_percent", "gross_energy_used_percent", "swap_count",
    "track_points", "event_count", "log_file",
]


def _iso_time(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone().isoformat()


def _distance_m(a: Dict, b: Dict) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(b["lon"]) - float(a["lon"]))
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    horizontal = radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))
    return math.hypot(horizontal, float(b.get("alt", 0.0)) - float(a.get("alt", 0.0)))


class FlightLogManager:
    def __init__(self, runtime_dir: Path):
        self.enabled = bool(FLIGHT_LOG_ENABLED)
        self.directory = Path(runtime_dir) / FLIGHT_LOG_DIRECTORY_NAME
        self.summary_path = self.directory / FLIGHT_LOG_SUMMARY_FILENAME
        self._lock = Lock()
        self._active: Dict[str, Dict] = {}
        self._summaries: List[Dict] = []
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._load_existing_summaries()

    def _load_existing_summaries(self):
        if not self.summary_path.exists():
            return
        try:
            with self.summary_path.open("r", encoding="utf-8-sig", newline="") as stream:
                self._summaries = list(csv.DictReader(stream))[-500:]
        except (OSError, csv.Error):
            self._summaries = []

    @staticmethod
    def _task_details(task: Dict) -> Dict:
        return {
            "id": task.get("id"),
            "origin": copy.deepcopy(task.get("origin")),
            "destination": copy.deepcopy(task.get("destination")),
            "delivery_type": task.get("delivery_type"),
            "delivery_label": task.get("delivery_label"),
            "priority": task.get("priority"),
            "payload_kg": task.get("payload_kg"),
            "deadline_minutes": task.get("deadline_minutes"),
            "cruise_alt": task.get("cruise_alt"),
            "data_mode": task.get("data_mode"),
            "created_at": task.get("created_at"),
            "created_at_iso": _iso_time(task.get("created_at")),
        }

    @staticmethod
    def _uav_details(uav: Dict) -> Dict:
        return {
            "uav_id": uav.get("id"),
            "base": uav.get("base"),
            "max_payload_kg": uav.get("max_payload_kg"),
            "cruise_speed_mps": uav.get("cruise_speed_mps"),
            "assigned_at": time.time(),
            "assigned_at_iso": _iso_time(time.time()),
            "initial_battery_percent": round(float(uav.get("battery", 0.0)), 3),
            "initial_position": {
                "lon": uav.get("lon"), "lat": uav.get("lat"), "alt": uav.get("alt", 0.0),
            },
        }

    def start_or_resume(self, task: Dict, uav: Dict, weather: Dict):
        if not self.enabled:
            return
        task_id = str(task["id"])
        with self._lock:
            record = self._active.get(task_id)
            if record is None:
                record = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "status": "IN_PROGRESS",
                    "task": self._task_details(task),
                    "started_at": task.get("started_at") or time.time(),
                    "started_at_iso": _iso_time(task.get("started_at") or time.time()),
                    "completed_at": None,
                    "completed_at_iso": None,
                    "dispatch_weather": copy.deepcopy(weather),
                    "completion_weather": None,
                    "participants": [],
                    "route_history": [],
                    "actual_track": [],
                    "events": [],
                }
                self._active[task_id] = record
            if not any(row.get("uav_id") == uav.get("id") for row in record["participants"]):
                record["participants"].append(self._uav_details(uav))
            self._append_route_plan(record, task)
            self._append_position(record, uav, force=True)

    def record_route_plan(self, task: Dict):
        if not self.enabled:
            return
        with self._lock:
            record = self._active.get(str(task.get("id")))
            if record:
                self._append_route_plan(record, task)

    @staticmethod
    def _append_route_plan(record: Dict, task: Dict):
        plan = task.get("route_plan")
        if not plan:
            return
        revision = int(plan.get("revision") or task.get("route_revision") or 0)
        if record["route_history"] and record["route_history"][-1].get("revision") == revision:
            return
        record["route_history"].append(copy.deepcopy(plan))

    def record_position(self, task: Optional[Dict], uav: Dict, force: bool = False):
        if not self.enabled or not task:
            return
        with self._lock:
            record = self._active.get(str(task.get("id")))
            if record:
                self._append_position(record, uav, force=force)

    @staticmethod
    def _track_point(uav: Dict, now: float) -> Dict:
        return {
            "timestamp": now,
            "time_iso": _iso_time(now),
            "uav_id": uav.get("id"),
            "phase": uav.get("phase"),
            "lon": round(float(uav.get("lon", 0.0)), 7),
            "lat": round(float(uav.get("lat", 0.0)), 7),
            "alt": round(float(uav.get("alt", 0.0)), 2),
            "battery_percent": round(float(uav.get("battery", 0.0)), 3),
            "wind_factor": round(float(uav.get("wind_factor", 1.0)), 3),
            "weather_flight_factor": round(float(uav.get("weather_flight_factor", 1.0)), 3),
            "effective_flight_factor": round(min(
                float(uav.get("wind_factor", 1.0)),
                float(uav.get("weather_flight_factor", 1.0)),
            ), 3),
            "weather_action": uav.get("weather_action", "NORMAL"),
        }

    def _append_position(self, record: Dict, uav: Dict, force: bool = False):
        track = record["actual_track"]
        if len(track) >= int(FLIGHT_LOG_MAX_TRACK_POINTS):
            return
        now = time.time()
        point = self._track_point(uav, now)
        previous = track[-1] if track else None
        if previous and not force:
            elapsed = now - float(previous["timestamp"])
            moved = _distance_m(previous, point) if previous.get("uav_id") == point.get("uav_id") else float("inf")
            same_phase = previous.get("phase") == point.get("phase")
            if elapsed < float(FLIGHT_LOG_SAMPLE_INTERVAL_SECONDS) and moved < float(FLIGHT_LOG_SAMPLE_MIN_DISTANCE_M) and same_phase:
                return
        track.append(point)

    def record_event(self, event_type: str, task_id: Optional[str], uav: Optional[Dict], message: str):
        if not self.enabled or not task_id:
            return
        with self._lock:
            record = self._active.get(str(task_id))
            if not record:
                return
            now = time.time()
            row = {
                "timestamp": now,
                "time_iso": _iso_time(now),
                "type": event_type,
                "uav_id": uav.get("id") if uav else None,
                "message": message,
            }
            if uav:
                row.update({
                    "phase": uav.get("phase"),
                    "battery_percent": round(float(uav.get("battery", 0.0)), 3),
                    "position": {
                        "lon": uav.get("lon"), "lat": uav.get("lat"), "alt": uav.get("alt", 0.0),
                    },
                })
            record["events"].append(row)

    def complete(self, task: Dict, uav: Dict, weather: Dict) -> Optional[Dict]:
        if not self.enabled:
            return None
        task_id = str(task["id"])
        with self._lock:
            record = self._active.get(task_id)
            if record is None:
                # 兼容服务更新前已经开始、更新后才完成的任务。
                record = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "status": "IN_PROGRESS",
                    "task": self._task_details(task),
                    "started_at": task.get("started_at") or task.get("created_at") or time.time(),
                    "started_at_iso": _iso_time(task.get("started_at") or task.get("created_at") or time.time()),
                    "completed_at": None,
                    "completed_at_iso": None,
                    "dispatch_weather": None,
                    "completion_weather": None,
                    "participants": [self._uav_details(uav)],
                    "route_history": [],
                    "actual_track": [],
                    "events": [],
                }
                self._active[task_id] = record
            self._append_route_plan(record, task)
            self._append_position(record, uav, force=True)
            completed_at = float(task.get("completed_at") or time.time())
            record.update({
                "status": "COMPLETED",
                "completed_at": completed_at,
                "completed_at_iso": _iso_time(completed_at),
                "completion_weather": copy.deepcopy(weather),
                "final_task_state": copy.deepcopy(task),
            })
            metrics = self._metrics(record)
            record["metrics"] = metrics
            self._close_participants(record)
            filename = f"{task_id}.json"
            log_path = self.directory / filename
            temp_path = self.directory / f".{task_id}.tmp"
            temp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(log_path)
            summary = self._summary(record, filename)
            self._append_summary(summary)
            self._summaries = [row for row in self._summaries if row.get("task_id") != task_id]
            self._summaries.append(summary)
            self._summaries = self._summaries[-500:]
            self._active.pop(task_id, None)
            return copy.deepcopy(summary)

    @staticmethod
    def _metrics(record: Dict) -> Dict:
        track = record.get("actual_track") or []
        distance = 0.0
        gross_energy = 0.0
        for previous, current in zip(track, track[1:]):
            if previous.get("uav_id") != current.get("uav_id"):
                continue
            distance += _distance_m(previous, current)
            decrease = float(previous.get("battery_percent", 0.0)) - float(current.get("battery_percent", 0.0))
            if decrease > 0:
                gross_energy += decrease
        batteries = [float(point.get("battery_percent", 0.0)) for point in track]
        altitudes = [float(point.get("alt", 0.0)) for point in track]
        started_at = float(record.get("started_at") or 0.0)
        completed_at = float(record.get("completed_at") or started_at)
        return {
            "duration_seconds": round(max(0.0, completed_at - started_at), 3),
            "actual_distance_m": round(distance, 3),
            "max_altitude_m": round(max(altitudes, default=0.0), 2),
            "battery_start_percent": round(batteries[0], 3) if batteries else None,
            "battery_end_percent": round(batteries[-1], 3) if batteries else None,
            "minimum_battery_percent": round(min(batteries), 3) if batteries else None,
            "gross_energy_used_percent": round(gross_energy, 3),
            "swap_count": int((record.get("final_task_state") or {}).get("swap_count", 0)),
            "track_points": len(track),
            "event_count": len(record.get("events") or []),
            "route_revision_count": len(record.get("route_history") or []),
        }

    @staticmethod
    def _close_participants(record: Dict):
        latest_by_uav = {}
        for point in record.get("actual_track") or []:
            latest_by_uav[point.get("uav_id")] = point
        for participant in record.get("participants") or []:
            latest = latest_by_uav.get(participant.get("uav_id"))
            if latest:
                participant["final_battery_percent"] = latest.get("battery_percent")
                participant["final_position"] = {
                    "lon": latest.get("lon"), "lat": latest.get("lat"), "alt": latest.get("alt"),
                }

    @staticmethod
    def _summary(record: Dict, filename: str) -> Dict:
        task = record.get("task") or {}
        metrics = record.get("metrics") or {}
        return {
            "task_id": record.get("task_id"),
            "status": record.get("status"),
            "uav_ids": ",".join(str(row.get("uav_id")) for row in record.get("participants") or []),
            "origin": (task.get("origin") or {}).get("name", ""),
            "destination": (task.get("destination") or {}).get("name", ""),
            "delivery_type": task.get("delivery_label") or task.get("delivery_type"),
            "priority": task.get("priority"),
            "payload_kg": task.get("payload_kg"),
            "started_at": record.get("started_at_iso"),
            "completed_at": record.get("completed_at_iso"),
            "duration_seconds": metrics.get("duration_seconds"),
            "actual_distance_m": metrics.get("actual_distance_m"),
            "max_altitude_m": metrics.get("max_altitude_m"),
            "battery_start_percent": metrics.get("battery_start_percent"),
            "battery_end_percent": metrics.get("battery_end_percent"),
            "gross_energy_used_percent": metrics.get("gross_energy_used_percent"),
            "swap_count": metrics.get("swap_count"),
            "track_points": metrics.get("track_points"),
            "event_count": metrics.get("event_count"),
            "log_file": filename,
        }

    def _append_summary(self, summary: Dict):
        is_new = not self.summary_path.exists() or self.summary_path.stat().st_size == 0
        encoding = "utf-8-sig" if is_new else "utf-8"
        with self.summary_path.open("a", encoding=encoding, newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(summary)

    def list_summaries(self, limit: int = 50) -> List[Dict]:
        if not self.enabled:
            return []
        with self._lock:
            return copy.deepcopy(list(reversed(self._summaries[-max(1, int(limit)):])))

    def get_log_path(self, task_id: str) -> Optional[Path]:
        if not self.enabled or not re_task_id(task_id):
            return None
        path = self.directory / f"{task_id}.json"
        return path if path.exists() and path.is_file() else None

    def read_log(self, task_id: str) -> Optional[Dict]:
        path = self.get_log_path(task_id)
        if not path:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def reset_active(self):
        with self._lock:
            self._active.clear()


def re_task_id(task_id: str) -> bool:
    return bool(task_id) and len(task_id) <= 40 and all(char.isalnum() or char in {"-", "_"} for char in task_id)
