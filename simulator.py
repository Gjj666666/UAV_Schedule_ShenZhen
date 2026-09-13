#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多无人机调度与飞行仿真核心。

仿真特点：
- 可初始化 1~20 架标准物流多旋翼。
- 按优先级 + 最近可用无人机进行多任务调度。
- UAV 从当前基地/位置先飞到取货点，再飞目的地。
- 2 Hz 更新位置；支持倍速。
- 进行简化 4D 冲突检测并通过高度层调整消解。
- 支持阵风、通信丢失、低电量、临时禁飞区等演示事件。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from planner import Obstacle, haversine_m, plan_route, route_cache_stats
from airspace_config import AIRSPACE_ZONES, default_approval_states, zone_is_active
from building_store import BUILDING_STORE
from energy_config import (
    BASE_CHARGE_TARGET_PERCENT,
    BASE_CHARGE_TIME_SECONDS,
    BATTERY_AFTER_SWAP_PERCENT,
    BATTERY_RESERVE_PERCENT,
    BATTERY_STATIONS,
    BATTERY_SWAP_TIME_SECONDS,
    BASE_CONSUMPTION_PERCENT_PER_100_KM,
    BLOCKED_TASK_RETRY_SECONDS,
    IDLE_RETURN_DELAY_SECONDS,
    MAX_STATION_CANDIDATES_TO_PLAN,
    MAX_SWAP_STOPS_PER_LEG,
    PAYLOAD_ENERGY_FACTOR_PER_KG,
    POST_TASK_RECOVERY_THRESHOLD_PERCENT,
    SAFE_RECOVERY_RETRY_SECONDS,
)
from energy_model import movement_energy_percent, route_energy_percent
from flight_log import FlightLogManager
from scheduler import rank_uavs, task_pickup_point, task_sort_key
from weather_service import WEATHER_MONITOR
from weather_config import (
    INFLIGHT_CAUTION_FLIGHT_FACTOR,
    INFLIGHT_EMERGENCY_FLIGHT_FACTOR,
    INFLIGHT_WEATHER_RESPONSE_ENABLED,
)
from planning_config import (
    BLOCKED_RETRY_INITIAL_SECONDS,
    BLOCKED_RETRY_MAX_SECONDS,
    BLOCKED_RETRY_MULTIPLIER,
)

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
ROUTES_DIR = RUNTIME / "routes"
LOG_DIR = RUNTIME / "logs"
UPLOAD_DIR = RUNTIME / "uploads"
for p in (ROUTES_DIR, LOG_DIR, UPLOAD_DIR):
    p.mkdir(parents=True, exist_ok=True)
FLIGHT_LOGS = FlightLogManager(RUNTIME)

# 十个“模拟运行基地”，按深圳主要行政片区分布。
# 坐标统一使用 WGS84，并避开 airspace_config.py 中全部绝对禁飞区和审批管制区；
# 初始化无人机与地图基地标记直接共用这些坐标，避免视觉位置和调度起点不一致。
# 这些点仅用于仿真，不代表真实运营许可点或已获批起降场。
SIM_BASES = [
    {"id": "BASE-01", "name": "FUTIAN_SIM_BASE", "label": "福田中心公园模拟基地", "lon": 114.0730970, "lat": 22.5496030},
    {"id": "BASE-02", "name": "NANSHAN_SIM_BASE", "label": "南山智园模拟基地", "lon": 113.9876949, "lat": 22.5907431},
    {"id": "BASE-03", "name": "LONGHUA_SIM_BASE", "label": "龙华北站中心公园模拟基地", "lon": 114.0209200, "lat": 22.6100600},
    {"id": "BASE-04", "name": "BAOAN_SIM_BASE", "label": "宝安石岩模拟基地", "lon": 113.9460000, "lat": 22.6842500},
    {"id": "BASE-05", "name": "GUANGMING_SIM_BASE", "label": "光明中心模拟基地", "lon": 113.9490000, "lat": 22.7440000},
    {"id": "BASE-06", "name": "LUOHU_SIM_BASE", "label": "罗湖东湖模拟基地", "lon": 114.1320000, "lat": 22.5807500},
    {"id": "BASE-07", "name": "LONGGANG_SIM_BASE", "label": "龙岗大运模拟基地", "lon": 114.2300000, "lat": 22.7130000},
    {"id": "BASE-08", "name": "PINGSHAN_SIM_BASE", "label": "坪山中心模拟基地", "lon": 114.3370000, "lat": 22.7072500},
    {"id": "BASE-09", "name": "YANTIAN_SIM_BASE", "label": "盐田大梅沙模拟基地", "lon": 114.3150000, "lat": 22.5960000},
    {"id": "BASE-10", "name": "DAPENG_SIM_BASE", "label": "大鹏中心模拟基地", "lon": 114.4808000, "lat": 22.6248000},
]

DELIVERY_LABELS = {
    "MEDICAL": "医疗物资",
    "EMERGENCY": "应急物资",
    "PARCEL": "普通包裹",
    "COLD_CHAIN": "冷链物资",
    "DOCUMENT": "文件",
}


class DispatchSystem:
    def __init__(self):
        self.uavs: Dict[str, Dict] = {}
        self.tasks: Dict[str, Dict] = {}
        self.obstacles: List[Obstacle] = []
        self.custom_features: List[Dict] = []
        self.airspace_approvals: Dict[str, bool] = default_approval_states()
        self._regulatory_obstacles_cache: Optional[List[Obstacle]] = None
        self._regulatory_obstacles_signature = None
        self.running = False
        self.speed_factor = 4.0
        self.auto_events = False
        self._subscribers = set()
        self._last_tick = time.monotonic()
        self._event_cooldown = 0.0
        self._dispatch_worker_active = False
        self.init_fleet(15)

    def init_fleet(self, count: int):
        count = max(1, min(1000, int(count)))
        self.uavs.clear()
        for i in range(count):
            base = SIM_BASES[i % len(SIM_BASES)]
            battery = round(82.0 + (i * 7) % 18 + random.random() * 2, 1)
            uid = f"UAV-{i+1:02d}"
            self.uavs[uid] = {
                "id": uid,
                "base": base["name"],
                # 待命无人机必须与所属基地标记完全重合；旧版人为散开会造成
                # 约百米级定位偏移，甚至把无人机显示到周边道路上。
                "lon": base["lon"],
                "lat": base["lat"],
                "alt": 0.0,
                "battery": min(100.0, battery),
                "max_payload_kg": 5.0,
                "cruise_speed_mps": 15.0,
                "max_speed_mps": 20.0,
                "state": "IDLE",
                "task_id": None,
                "phase": "IDLE",
                "route": [],
                "route_index": 0,
                "segment_t": 0.0,
                "wind_factor": 1.0,
                "weather_flight_factor": 1.0,
                "weather_action": "NORMAL",
                "weather_response_signature": "",
                "hold_until": 0.0,
                "last_event": "",
                "swap_station": None,
                "swap_remaining_s": 0.0,
                "resume_phase": None,
                "post_swap_route": [],
                "pending_energy_actions": [],
                "swap_count": 0,
                "idle_elapsed_s": 0.0,
                "charge_remaining_s": 0.0,
                "recovery_target": None,
                "recovery_retry_at": 0.0,
                "handover_target": None,
            }
        self._write_snapshot()

    def create_task(self, payload: Dict) -> Dict:
        tid = f"TASK-{uuid.uuid4().hex[:6].upper()}"
        now = time.time()
        task = {
            "id": tid,
            "origin": payload["origin"],
            "destination": payload["destination"],
            "delivery_type": payload.get("delivery_type", "PARCEL"),
            "delivery_label": DELIVERY_LABELS.get(payload.get("delivery_type", "PARCEL"), "普通包裹"),
            "priority": payload.get("priority", "NORMAL"),
            "payload_kg": float(payload.get("payload_kg", 1.0)),
            "deadline_minutes": int(payload.get("deadline_minutes", 45)),
            "cruise_alt": float(payload.get("cruise_alt", 80.0)),
            "data_mode": payload.get("data_mode", "cesium"),
            "status": "WAITING",
            "assigned_uav": None,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "route_method": None,
            # 任务级完整航线独立于 UAV 当前正在执行的 route。
            # 前端依靠 revision 判断调度/重规划后是否需要原地替换地图实体。
            "route_revision": 0,
            "route_plan": None,
            "estimated_energy_percent": None,
            "swap_count": 0,
            "message": "等待调度",
            "blocked_reason": None,
            "retry_at": None,
            "dispatch_attempts": 0,
            "planning_retry_count": 0,
            "dispatch_progress": {
                "active": False,
                "percent": 0.0,
                "stage": "等待调度",
                "detail": "任务已进入调度队列",
                "updated_at": now,
            },
            # 货物位置独立于任务路线：取货后发生异常时不能再从原取货点重派。
            "cargo_status": "WAITING_PICKUP",
            "cargo_location": {"type": "ORIGIN", **dict(payload["origin"])},
            "handover_station": None,
            "handover_history": [],
            "excluded_uav_ids": [],
        }
        self.tasks[tid] = task
        self._log("tasks.jsonl", task)
        return task

    def get_public_state(self) -> Dict:
        return {
            "running": self.running,
            "speed_factor": self.speed_factor,
            "auto_events": self.auto_events,
            "tasks": list(self.tasks.values()),
            "uavs": [self._public_uav(u) for u in self.uavs.values()],
            "custom_feature_count": len(self.custom_features),
            "building_dataset": BUILDING_STORE.stats(),
            "uav_bases": SIM_BASES,
            "battery_stations": BATTERY_STATIONS,
            "airspace": self.airspace_state(),
            "energy_config": {
                "base_consumption_percent_per_100_km": BASE_CONSUMPTION_PERCENT_PER_100_KM,
                "payload_energy_factor_per_kg": PAYLOAD_ENERGY_FACTOR_PER_KG,
                "battery_reserve_percent": BATTERY_RESERVE_PERCENT,
                "battery_swap_time_seconds": BATTERY_SWAP_TIME_SECONDS,
                "battery_after_swap_percent": BATTERY_AFTER_SWAP_PERCENT,
                "post_task_recovery_threshold_percent": POST_TASK_RECOVERY_THRESHOLD_PERCENT,
                "idle_return_delay_seconds": IDLE_RETURN_DELAY_SECONDS,
                "base_charge_time_seconds": BASE_CHARGE_TIME_SECONDS,
                "base_charge_target_percent": BASE_CHARGE_TARGET_PERCENT,
                "blocked_task_retry_seconds": BLOCKED_TASK_RETRY_SECONDS,
            },
            "weather": WEATHER_MONITOR.get_state(),
            "flight_logs": FLIGHT_LOGS.list_summaries(limit=30),
            "dispatch_progress": self.get_dispatch_progress(),
            "route_cache": route_cache_stats(),
        }

    def _public_uav(self, u: Dict) -> Dict:
        # Demo 前端需要当前规划航线用于 3D 展示，因此保留 route。
        return dict(u)

    @staticmethod
    def _set_dispatch_progress(
        task: Dict,
        percent: float,
        stage: str,
        detail: str = "",
        active: bool = True,
    ):
        """记录调度的真实阶段；整项替换可供状态接口安全读取。"""
        task["dispatch_progress"] = {
            "active": bool(active),
            "percent": round(max(0.0, min(100.0, float(percent))), 1),
            "stage": str(stage),
            "detail": str(detail),
            "updated_at": time.time(),
        }

    def get_dispatch_progress(self) -> Dict:
        rows = []
        for task in self.tasks.values():
            progress = task.get("dispatch_progress")
            if not isinstance(progress, dict):
                continue
            rows.append({
                "task_id": task["id"],
                "origin": task.get("origin", {}).get("name", "起点"),
                "destination": task.get("destination", {}).get("name", "终点"),
                "task_status": task.get("status"),
                **progress,
            })
        active = sorted(
            (row for row in rows if row.get("active")),
            key=lambda row: float(row.get("updated_at") or 0.0),
            reverse=True,
        )
        latest = max(rows, key=lambda row: float(row.get("updated_at") or 0.0), default=None)
        waiting_statuses = {"WAITING", "WAITING_BLOCKED", "WAITING_HANDOVER"}
        return {
            "busy": bool(getattr(self, "_dispatch_worker_active", False) or active),
            "active_count": len(active),
            "waiting_count": sum(task.get("status") in waiting_statuses for task in self.tasks.values()),
            "current": active[0] if active else latest,
        }

    @staticmethod
    def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
            if ((yi > lat) != (yj > lat)) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
            j = i
        return inside

    def airspace_state(self) -> Dict:
        approvals = getattr(self, "airspace_approvals", default_approval_states())
        zones = []
        for source in AIRSPACE_ZONES:
            active = zone_is_active(source)
            controlled = source["category"] == "CONTROLLED"
            approved = bool(approvals.get(source["id"], source.get("default_approved", False))) if controlled else False
            blocks_dispatch = bool(active and (not controlled or not approved))
            polygons = [[list(point) for point in ring] for ring in source.get("polygons") or []]
            geometry = {
                "type": "Polygon" if len(polygons) == 1 else "MultiPolygon",
                "coordinates": [polygons[0]] if len(polygons) == 1 else [[ring] for ring in polygons],
            }
            zones.append({
                **{key: value for key, value in source.items() if key != "polygons"},
                "active": active,
                "approved": approved,
                "blocks_dispatch": blocks_dispatch,
                "status": (
                    "EXPIRED" if not active else
                    "HARD_BLOCKED" if not controlled else
                    "APPROVED_ALLOWED" if approved else "UNAPPROVED_BLOCKED"
                ),
                "geometry": geometry,
            })
        return {
            "zones": zones,
            "hard_active_count": sum(z["active"] and z["category"] == "HARD" for z in zones),
            "controlled_active_count": sum(z["active"] and z["category"] == "CONTROLLED" for z in zones),
            "blocking_count": sum(z["blocks_dispatch"] for z in zones),
            "coordinate_system": "WGS84 (EPSG:4326)",
            "official_boundary_warning": "示意边界不能替代民航UOM、航行情报或主管部门批复坐标。",
        }

    def _planning_obstacles(self) -> List[Obstacle]:
        cached = getattr(self, "_regulatory_obstacles_cache", None)
        approvals = getattr(self, "airspace_approvals", default_approval_states())
        signature = tuple(
            (zone["id"], zone_is_active(zone), bool(approvals.get(zone["id"], False)))
            for zone in AIRSPACE_ZONES
        )
        if cached is None or signature != getattr(self, "_regulatory_obstacles_signature", None):
            cached = []
            for zone in AIRSPACE_ZONES:
                if not zone_is_active(zone):
                    continue
                if zone["category"] == "CONTROLLED" and approvals.get(zone["id"], False):
                    continue
                for index, ring in enumerate(zone.get("polygons") or []):
                    cached.append(Obstacle(
                        kind="no_fly",
                        polygon=[(float(point[0]), float(point[1])) for point in ring],
                        height=10_000.0,
                        name=f"{zone['name']}#{index + 1}",
                    ))
            self._regulatory_obstacles_cache = cached
            self._regulatory_obstacles_signature = signature
        return [*self.obstacles, *cached]

    def airspace_restrictions_at_point(self, point: Dict) -> List[Dict]:
        lon, lat = float(point["lon"]), float(point["lat"])
        return [
            zone for zone in self.airspace_state()["zones"]
            if zone["blocks_dispatch"] and any(
                self._point_in_ring(lon, lat, ring)
                for ring in next(item for item in AIRSPACE_ZONES if item["id"] == zone["id"]).get("polygons") or []
            )
        ]

    def set_airspace_approval(self, zone_id: str, approved: bool) -> Dict:
        zone = next((item for item in AIRSPACE_ZONES if item["id"] == zone_id), None)
        if not zone:
            raise ValueError("没有找到指定空域")
        if zone["category"] != "CONTROLLED":
            raise ValueError("绝对禁飞区不能通过审批选择解除")
        if not hasattr(self, "airspace_approvals"):
            self.airspace_approvals = default_approval_states()
        self.airspace_approvals[zone_id] = bool(approved)
        self._regulatory_obstacles_cache = None
        self._regulatory_obstacles_signature = None
        action = "已审批，允许规划进入" if approved else "未审批，禁止规划进入"
        self._event("AIRSPACE_APPROVAL_CHANGED", None, None, f"{zone['name']}：{action}")

        # 立即让受影响的等待任务重试，并让在途任务按新的空域许可重规划。
        for task in self.tasks.values():
            if task.get("status") == "WAITING_BLOCKED":
                task["retry_at"] = 0.0
                task["planning_retry_count"] = 0
        for uav in list(self.uavs.values()):
            task = self.tasks.get(uav.get("task_id"))
            phase = uav.get("resume_phase") if uav.get("phase") == "TO_SWAP" else uav.get("phase")
            if not task or phase not in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING"}:
                continue
            reason = f"{zone['name']}审批状态变化：{action}"
            replanned = self._divert_to_battery_station(uav, task, reason)
            if not replanned and not approved:
                self._abort_task_safely(uav, task, f"空域变为未审批且没有安全绕行路线：{zone['name']}")
        return self.airspace_state()

    @staticmethod
    def _effective_flight_factor(uav: Dict) -> float:
        """合并局部阵风与全市天气影响，供速度和能耗统一使用。"""
        wind_factor = max(0.1, min(1.0, float(uav.get("wind_factor", 1.0))))
        weather_factor = max(0.1, min(1.0, float(uav.get("weather_flight_factor", 1.0))))
        return min(wind_factor, weather_factor)

    def add_custom_geojson(self, geojson: Dict, forced_kind: str = "auto") -> Dict:
        features = geojson.get("features", []) if geojson.get("type") == "FeatureCollection" else [geojson]
        added = {"building": 0, "no_fly": 0, "landing_site": 0, "other": 0}
        normalized = []
        for f in features[:10000]:
            if not isinstance(f, dict) or f.get("type") != "Feature":
                continue
            props = dict(f.get("properties") or {})
            kind = forced_kind if forced_kind != "auto" else str(props.get("kind") or props.get("type") or "other").lower()
            if kind in {"nofly", "no-fly", "restricted", "no_fly_zone"}:
                kind = "no_fly"
            if kind not in added:
                kind = "other"
            props["kind"] = kind
            nf = {"type": "Feature", "properties": props, "geometry": f.get("geometry")}
            normalized.append(nf)
            added[kind] += 1

            geom = f.get("geometry") or {}
            if kind in {"building", "no_fly"} and geom.get("type") in {"Polygon", "MultiPolygon"}:
                polygons = geom.get("coordinates", [])
                if geom.get("type") == "Polygon":
                    polygons = [polygons]
                for polygon in polygons:
                    if not polygon or not polygon[0]:
                        continue
                    ring = [(float(p[0]), float(p[1])) for p in polygon[0] if len(p) >= 2]
                    if len(ring) < 3:
                        continue
                    height = float(props.get("height") or props.get("building_height") or 35.0)
                    if kind == "no_fly":
                        height = 10_000.0
                    self.obstacles.append(Obstacle(kind=kind, polygon=ring, height=height, name=str(props.get("name", ""))))
        self.custom_features.extend(normalized)
        return {"added": added, "total_features": len(self.custom_features), "geojson": {"type": "FeatureCollection", "features": normalized}}

    def clear_custom_data(self):
        self.obstacles.clear()
        self.custom_features.clear()

    def custom_geojson(self) -> Dict:
        return {"type": "FeatureCollection", "features": self.custom_features}

    def set_control(self, running: Optional[bool] = None, speed_factor: Optional[float] = None, auto_events: Optional[bool] = None):
        if running is not None:
            self.running = bool(running)
        if speed_factor is not None:
            self.speed_factor = max(0.5, min(30.0, float(speed_factor)))
        if auto_events is not None:
            self.auto_events = bool(auto_events)

    def reset_runtime(self):
        count = len(self.uavs) or 6
        self.tasks.clear()
        FLIGHT_LOGS.reset_active()
        self.running = False
        self._dispatch_worker_active = False
        self.init_fleet(count)

    def _save_route(self, task_id: str, phase: str, result: Dict):
        out = dict(result)
        out["task_id"] = task_id
        out["phase"] = phase
        (ROUTES_DIR / f"{task_id}_{phase.lower()}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _route_failed(result: Dict) -> bool:
        return bool(result.get("planning_failed")) or len(result.get("route") or []) < 2

    @staticmethod
    def _public_route_leg(result: Dict) -> Dict:
        """提取可随任务状态推送给前端的单段规划结果。"""
        return {
            "route": list(result.get("route") or []),
            "method": result.get("method"),
            "selected_altitude": result.get("selected_altitude"),
            "buildings_considered": result.get("buildings_considered", 0),
            "blocking_buildings": result.get("blocking_buildings", 0),
            "route_length_m": result.get("route_length_m"),
            "estimated_energy_percent": result.get("estimated_energy_percent"),
            "swap_stops": list(result.get("swap_stops") or []),
        }

    def _set_task_route_plan(
        self,
        task: Dict,
        uav_id: str,
        pickup_result: Dict,
        delivery_result: Dict,
        reason: str,
    ):
        revision = int(task.get("route_revision", 0)) + 1
        task["route_revision"] = revision
        task["route_plan"] = {
            "revision": revision,
            "updated_at": time.time(),
            "assigned_uav": uav_id,
            "reason": reason,
            "takeoff_to_pickup": self._public_route_leg(pickup_result),
            "pickup_to_destination": self._public_route_leg(delivery_result),
        }
        FLIGHT_LOGS.record_route_plan(task)

    def _update_task_route_leg(self, task: Dict, leg_name: str, result: Dict, uav_id: str, reason: str):
        """替换重规划后的剩余航段，同时保留任务另一段航线。"""
        previous = task.get("route_plan") or {}
        revision = int(task.get("route_revision", 0)) + 1
        task["route_revision"] = revision
        task["route_plan"] = {
            "revision": revision,
            "updated_at": time.time(),
            "assigned_uav": uav_id,
            "reason": reason,
            "takeoff_to_pickup": previous.get("takeoff_to_pickup"),
            "pickup_to_destination": previous.get("pickup_to_destination"),
        }
        task["route_plan"][leg_name] = self._public_route_leg(result)
        FLIGHT_LOGS.record_route_plan(task)

    def _set_cargo_recovery_route(
        self, task: Dict, uav_id: str, result: Dict, target: Dict, reason: str,
    ):
        revision = int(task.get("route_revision", 0)) + 1
        task["route_revision"] = revision
        task["route_plan"] = {
            "revision": revision,
            "updated_at": time.time(),
            "assigned_uav": uav_id,
            "reason": reason,
            "takeoff_to_pickup": None,
            "pickup_to_destination": None,
            "cargo_recovery": self._public_route_leg(result),
            "handover_target": dict(target),
        }
        FLIGHT_LOGS.record_route_plan(task)

    def _clear_task_route_plan(self, task: Dict):
        # 即使清空也递增版本，保证暂停状态下主动拉取时前端会删除旧线路。
        task["route_revision"] = int(task.get("route_revision", 0)) + 1
        task["route_plan"] = None

    @staticmethod
    def _merge_routes(*routes: Sequence[Dict]) -> List[Dict]:
        merged: List[Dict] = []
        for route in routes:
            for point in route or []:
                current = dict(point)
                if merged:
                    previous = merged[-1]
                    same_position = (
                        abs(float(previous["lon"]) - float(current["lon"])) < 1e-9
                        and abs(float(previous["lat"]) - float(current["lat"])) < 1e-9
                        and abs(float(previous.get("alt", 0.0)) - float(current.get("alt", 0.0))) < 0.1
                    )
                    if same_position:
                        continue
                merged.append(current)
        return merged

    @staticmethod
    def _route_length_3d(route: Sequence[Dict]) -> float:
        return sum(
            math.hypot(
                haversine_m(start, end),
                float(end.get("alt", 0.0)) - float(start.get("alt", 0.0)),
            )
            for start, end in zip(route or [], (route or [])[1:])
        )

    @staticmethod
    def _station_arrival_route(route: Sequence[Dict], station: Dict) -> List[Dict]:
        result = [dict(point) for point in route or []]
        if result:
            result.append({"lon": station["lon"], "lat": station["lat"], "alt": 0.0})
        return result

    @staticmethod
    def _station_departure_route(route: Sequence[Dict], station: Dict) -> List[Dict]:
        result = [dict(point) for point in route or []]
        if result:
            result.insert(0, {"lon": station["lon"], "lat": station["lat"], "alt": 0.0})
        return result

    def _plan_energy_aware_leg(
        self,
        start: Dict,
        target: Dict,
        direct_result: Dict,
        available_battery: float,
        payload_kg: float,
        wind_factor: float,
        cruise_alt: float,
        data_mode: str,
        progress_callback=None,
    ) -> Optional[Dict]:
        """返回可直接执行的航段；必要时插入一个或多个换电站。"""
        current = dict(start)
        current_battery = float(available_battery)
        current_direct = dict(direct_result)
        departing_station: Optional[Dict] = None
        used_station_ids = set()
        actions: List[Dict] = []
        route_parts: List[Sequence[Dict]] = []
        swap_stops: List[Dict] = []
        total_energy = 0.0
        total_buildings = 0
        total_blocking = 0
        max_altitude = 0.0

        for _ in range(max(0, int(MAX_SWAP_STOPS_PER_LEG)) + 1):
            if progress_callback:
                progress_callback(0.08, "正在计算本航段预计耗电")
            direct_route = list(current_direct.get("route") or [])
            if departing_station:
                direct_route = self._station_departure_route(direct_route, departing_station)
            else:
                direct_route = self._merge_routes([
                    {"lon": current["lon"], "lat": current["lat"], "alt": current.get("alt", 0.0)}
                ], direct_route)
            direct_energy = route_energy_percent(direct_route, payload_kg, wind_factor)
            if current_battery + 1e-6 >= direct_energy + BATTERY_RESERVE_PERCENT:
                if progress_callback:
                    progress_callback(1.0, "电量满足安全余量要求")
                actions.append({"route": direct_route, "station": None})
                route_parts.append(direct_route)
                total_energy += direct_energy
                total_buildings += int(current_direct.get("buildings_considered", 0))
                total_blocking += int(current_direct.get("blocking_buildings", 0))
                max_altitude = max(max_altitude, float(current_direct.get("selected_altitude") or 0.0))
                combined_route = self._merge_routes(*route_parts)
                display_result = dict(direct_result)
                display_result.update({
                    "route": combined_route,
                    "route_length_m": self._route_length_3d(combined_route),
                    "method": (
                        f"{direct_result.get('method', 'ROUTE')}+BATTERY_SWAP"
                        if swap_stops else direct_result.get("method", "ROUTE")
                    ),
                    "selected_altitude": max_altitude or direct_result.get("selected_altitude"),
                    "buildings_considered": total_buildings,
                    "blocking_buildings": total_blocking,
                    "estimated_energy_percent": round(total_energy, 3),
                    "swap_stops": swap_stops,
                })
                first_action = actions[0]
                return {
                    "display_result": display_result,
                    "actions": actions,
                    "initial_route": first_action["route"],
                    "station": first_action["station"],
                    "post_swap_route": actions[1]["route"] if len(actions) > 1 else [],
                    "ending_battery": max(0.0, current_battery - direct_energy),
                }

            if len(swap_stops) >= max(0, int(MAX_SWAP_STOPS_PER_LEG)):
                return None

            # 先用直线能耗下界筛出当前电量可能到达的站，再优先尝试更接近终点的站。
            possible = []
            for station in BATTERY_STATIONS:
                if station["id"] in used_station_ids:
                    continue
                lower_bound_route = [
                    {"lon": current["lon"], "lat": current["lat"], "alt": current.get("alt", 0.0)},
                    {"lon": station["lon"], "lat": station["lat"], "alt": 0.0},
                ]
                lower_bound = route_energy_percent(lower_bound_route, payload_kg, wind_factor)
                if lower_bound + BATTERY_RESERVE_PERCENT <= current_battery + 1e-6:
                    possible.append(station)
            possible.sort(key=lambda station: (haversine_m(station, target), haversine_m(current, station)))

            station_candidates = []
            limited_stations = possible[:max(1, int(MAX_STATION_CANDIDATES_TO_PLAN))]
            for station_index, station in enumerate(limited_stations):
                if progress_callback:
                    progress_callback(
                        0.15 + 0.72 * station_index / max(1, len(limited_stations)),
                        f"电量不足，正在验证换电站 {station['name']}（{station_index + 1}/{len(limited_stations)}）",
                    )
                station_result = plan_route(
                    current, station, cruise_alt, data_mode, self._planning_obstacles(), BUILDING_STORE,
                )
                if self._route_failed(station_result):
                    continue
                station_route = list(station_result["route"])
                if departing_station:
                    station_route = self._station_departure_route(station_route, departing_station)
                else:
                    station_route = self._merge_routes([
                        {"lon": current["lon"], "lat": current["lat"], "alt": current.get("alt", 0.0)}
                    ], station_route)
                station_route = self._station_arrival_route(station_route, station)
                station_energy = route_energy_percent(station_route, payload_kg, wind_factor)
                if station_energy + BATTERY_RESERVE_PERCENT > current_battery + 1e-6:
                    continue
                station_candidates.append((
                    haversine_m(station, target) + self._route_length_3d(station_route) * 0.15,
                    station,
                    station_result,
                    station_route,
                    station_energy,
                ))
            if not station_candidates:
                if progress_callback:
                    progress_callback(1.0, "没有找到当前电量可安全到达的换电站")
                return None

            _, station, station_result, station_route, station_energy = min(station_candidates, key=lambda row: row[0])
            stop = {
                "id": station["id"],
                "name": station["name"],
                "lon": station["lon"],
                "lat": station["lat"],
                "swap_time_seconds": BATTERY_SWAP_TIME_SECONDS,
                "energy_before_swap_percent": round(station_energy, 3),
            }
            actions.append({"route": station_route, "station": dict(station)})
            route_parts.append(station_route)
            swap_stops.append(stop)
            total_energy += station_energy
            total_buildings += int(station_result.get("buildings_considered", 0))
            total_blocking += int(station_result.get("blocking_buildings", 0))
            max_altitude = max(max_altitude, float(station_result.get("selected_altitude") or 0.0))
            used_station_ids.add(station["id"])
            current = {"lon": station["lon"], "lat": station["lat"], "alt": 0.0}
            departing_station = station
            current_battery = float(BATTERY_AFTER_SWAP_PERCENT)
            current_direct = plan_route(
                current, target, cruise_alt, data_mode, self._planning_obstacles(), BUILDING_STORE,
            )
            if self._route_failed(current_direct):
                if progress_callback:
                    progress_callback(1.0, "换电后的剩余航段无法安全规划")
                return None
        return None

    @staticmethod
    def _activate_leg_plan(uav: Dict, plan: Dict, resume_phase: str):
        actions = list(plan.get("actions") or [{"route": plan["initial_route"], "station": plan.get("station")}])
        first_action = actions[0]
        station = first_action.get("station")
        uav.update({
            "route": first_action["route"],
            "route_index": 0,
            "segment_t": 0.0,
            "swap_station": station,
            "swap_remaining_s": 0.0,
            "resume_phase": resume_phase if station else None,
            "post_swap_route": actions[1]["route"] if len(actions) > 1 else [],
            "pending_energy_actions": actions[1:],
            "phase": "TO_SWAP" if station else resume_phase,
        })

    @staticmethod
    def _current_payload(task: Dict, uav: Dict) -> float:
        phase = uav.get("phase")
        carrying = phase in {"DELIVERING", "TO_CARGO_HANDOVER"} or (
            phase in {"TO_SWAP", "SWAPPING"} and uav.get("resume_phase") == "DELIVERING"
        )
        return float(task.get("payload_kg", 0.0)) if carrying else 0.0

    @staticmethod
    def _remaining_route(uav: Dict) -> List[Dict]:
        route = uav.get("route") or []
        if len(route) < 2:
            return []
        index = min(int(uav.get("route_index", 0)), len(route) - 2)
        current = {"lon": uav["lon"], "lat": uav["lat"], "alt": uav.get("alt", 0.0)}
        return [current, *[dict(point) for point in route[index + 1:]]]

    @staticmethod
    def _energy_failure(reason: str) -> Dict:
        return {
            "method": "NO_REACHABLE_BATTERY_STATION",
            "planning_failed": True,
            "failure_reason": reason,
            "route": [],
            "selected_altitude": None,
            "buildings_considered": 0,
            "blocking_buildings": 0,
        }

    def _divert_to_battery_station(self, uav: Dict, task: Dict, reason: str) -> bool:
        current_phase = uav.get("phase")
        resume_phase = uav.get("resume_phase") if current_phase == "TO_SWAP" else current_phase
        if resume_phase not in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING"}:
            return False
        start = {"lon": uav["lon"], "lat": uav["lat"], "alt": uav.get("alt", 0.0)}
        target = task_pickup_point(task) if resume_phase in {"TO_PICKUP", "TO_HANDOVER"} else task["destination"]
        direct = plan_route(
            start, target, task["cruise_alt"], task.get("data_mode", "shenzhen"),
            self._planning_obstacles(), BUILDING_STORE,
        )
        if self._route_failed(direct):
            return False
        payload = self._current_payload(task, uav)
        plan = self._plan_energy_aware_leg(
            start, target, direct, uav["battery"], payload, self._effective_flight_factor(uav),
            task["cruise_alt"], task.get("data_mode", "shenzhen"),
        )
        if not plan:
            return False

        self._activate_leg_plan(uav, plan, resume_phase)
        leg_name = "takeoff_to_pickup" if resume_phase in {"TO_PICKUP", "TO_HANDOVER"} else "pickup_to_destination"
        self._update_task_route_leg(task, leg_name, plan["display_result"], uav["id"], reason)
        task["estimated_energy_percent"] = plan["display_result"].get("estimated_energy_percent")
        station = plan.get("station")
        if station:
            task["message"] = f"预计电量不足，改道 {station['name']} 换电后继续任务"
            uav["last_event"] = task["message"]
            self._event("BATTERY_SWAP_PLANNED", uav["id"], task["id"], task["message"])
        else:
            task["message"] = "能耗变化后已重新确认剩余航线"
        self._save_route(task["id"], f"{resume_phase.lower()}_energy_replanned", plan["display_result"])
        return True

    def _mark_task_planning_failed(self, task: Dict, result: Dict, phase: str, uav: Optional[Dict] = None):
        """记录不可恢复的规划错误；飞行中的失败必须改走安全恢复流程。"""
        reason = result.get("failure_reason") or "未找到安全航线"
        self._clear_task_route_plan(task)
        task.update({
            "status": "PLANNING_FAILED",
            "assigned_uav": None,
            "route_method": result.get("method", "NO_SAFE_ROUTE"),
            "selected_altitude": result.get("selected_altitude"),
            "buildings_considered": result.get("buildings_considered", 0),
            "blocking_buildings": result.get("blocking_buildings", 0),
            "message": reason,
        })
        self._event("TASK_PLANNING_FAILED", uav["id"] if uav else None, task["id"], f"{phase}：{reason}")

    def _block_task(self, task: Dict, reason: str, uav_id: Optional[str] = None):
        """安全方案暂时不可用：保留任务并定时重试，而不是永久判失败。"""
        self._clear_task_route_plan(task)
        retry_count = int(task.get("planning_retry_count") or 0) + 1
        retry_delay = min(
            float(BLOCKED_RETRY_MAX_SECONDS),
            max(float(BLOCKED_TASK_RETRY_SECONDS), float(BLOCKED_RETRY_INITIAL_SECONDS))
            * float(BLOCKED_RETRY_MULTIPLIER) ** max(0, retry_count - 1),
        )
        task.update({
            "status": "WAITING_BLOCKED",
            "assigned_uav": None,
            "blocked_reason": reason,
            "retry_at": time.time() + retry_delay,
            "planning_retry_count": retry_count,
            "message": f"安全等待：{reason}；约 {retry_delay:.0f} 秒后自动重试",
        })
        self._event("TASK_SAFETY_BLOCKED", uav_id, task["id"], task["message"])

    @staticmethod
    def _reset_uav_leg(uav: Dict):
        uav.update({
            "task_id": None,
            "route": [],
            "route_index": 0,
            "segment_t": 0.0,
            "swap_station": None,
            "swap_remaining_s": 0.0,
            "resume_phase": None,
            "post_swap_route": [],
            "pending_energy_actions": [],
            "handover_target": None,
        })

    def _apply_inflight_weather_policy(self):
        """把实时/模拟天气转换为在途降速、能耗复核或安全撤离动作。"""
        if not INFLIGHT_WEATHER_RESPONSE_ENABLED:
            return
        weather = WEATHER_MONITOR.get_state()
        action = str(weather.get("flight_action") or "MONITOR_ONLY")
        reason = str(weather.get("flight_action_reason") or weather.get("pause_reason") or "天气状态变化")
        signature = "|".join((
            action,
            str(weather.get("weather") or ""),
            str(weather.get("wind_power") or ""),
            str(weather.get("temperature_c") or ""),
        ))

        for uav in self.uavs.values():
            previous_action = str(uav.get("weather_action") or "NORMAL")
            if action == "RECOVER":
                uav["weather_flight_factor"] = max(
                    0.1, min(1.0, float(INFLIGHT_EMERGENCY_FLIGHT_FACTOR)),
                )
            elif action == "CONTINUE_CAUTION":
                uav["weather_flight_factor"] = max(
                    0.1, min(1.0, float(INFLIGHT_CAUTION_FLIGHT_FACTOR)),
                )
            else:
                uav["weather_flight_factor"] = 1.0
            uav["weather_action"] = action

            task = self.tasks.get(uav.get("task_id"))
            if not task:
                uav["weather_response_signature"] = signature
                continue
            if uav.get("weather_response_signature") == signature:
                continue
            uav["weather_response_signature"] = signature

            if action == "CONTINUE_CAUTION":
                message = f"天气谨慎飞行：{reason}；已降速并按新风况复核剩余电量"
                task["message"] = message
                uav["last_event"] = message
                self._event("WEATHER_CAUTION", uav["id"], task["id"], message)
                continue

            if action == "RECOVER":
                phase = uav.get("phase")
                active_phase = uav.get("hold_resume_phase") if phase == "HOLD" else phase
                # 已经在执行货物交接或安全恢复时只维持恶劣天气飞行系数，
                # 不重复中止和重建同一条恢复路线。
                if active_phase not in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING", "TO_SWAP", "SWAPPING"}:
                    continue
                message = f"严重天气应急：{reason}"
                uav["last_event"] = message
                self._event("WEATHER_EMERGENCY_DETECTED", uav["id"], task["id"], message)
                self._abort_task_safely(uav, task, message)
                continue

            if previous_action in {"CONTINUE_CAUTION", "RECOVER"}:
                message = "天气恢复正常：解除在途天气限速；安全恢复中的任务继续完成当前恢复流程"
                uav["last_event"] = message
                self._event("WEATHER_NORMALIZED", uav["id"], task["id"], message)

    def _assign_waiting(self):
        now = time.time()
        waiting = sorted((
            task for task in self.tasks.values()
            if task["status"] in {"WAITING", "WAITING_HANDOVER"}
            or (
                task["status"] == "WAITING_BLOCKED"
                and float(task.get("retry_at") or 0.0) <= now
            )
        ), key=task_sort_key)
        weather = WEATHER_MONITOR.get_state()
        if not weather.get("dispatch_allowed", True):
            reason = weather.get("pause_reason") or weather.get("dispatch_status") or "天气不满足安全派遣条件"
            for task in waiting:
                if task["status"] in {"WAITING", "WAITING_HANDOVER"}:
                    task["message"] = f"天气暂停派遣：{reason}"
                    self._set_dispatch_progress(task, 0, "天气暂停派遣", reason, active=False)
            return
        for task in waiting:
            self._set_dispatch_progress(task, 3, "开始调度", "正在读取任务参数和安全约束")
            candidates = rank_uavs(task, self.uavs.values())
            if not candidates:
                has_active = any(uav["state"] in {"BUSY", "CHARGING"} for uav in self.uavs.values())
                if has_active:
                    handover = task.get("cargo_status") == "WAITING_HANDOVER"
                    task["status"] = "WAITING_HANDOVER" if handover else "WAITING"
                    task["message"] = (
                        "货物已在安全交接点，等待其他无人机接驳"
                        if handover else "暂无可立即出勤的无人机，等待在途任务完成或基地充电"
                    )
                else:
                    self._block_task(task, "没有满足载重和最低出勤电量的无人机")
                self._set_dispatch_progress(
                    task, 0, "等待可用无人机", task.get("message") or "当前没有可调度无人机", active=False,
                )
                continue
            self._set_dispatch_progress(task, 8, "候选无人机筛选完成", f"共 {len(candidates)} 架候选无人机")
            selected = None
            failures: List[str] = []
            pickup_point = task_pickup_point(task)
            for candidate_index, uav in enumerate(candidates):
                candidate_base = 10.0 + 76.0 * candidate_index / max(1, len(candidates))
                candidate_span = 76.0 / max(1, len(candidates))

                def route_progress(section_start, section_span, section_name):
                    def update(fraction, detail):
                        percent = candidate_base + candidate_span * (
                            section_start + section_span * max(0.0, min(1.0, float(fraction)))
                        )
                        self._set_dispatch_progress(
                            task, percent, section_name, f"{uav['id']}：{detail}",
                        )
                    return update

                # 逐架验证当前位置→取货点→目的地，包括建筑、禁飞区、风况和换电。
                start = {"lon": uav["lon"], "lat": uav["lat"], "alt": uav.get("alt", 0.0)}
                self._set_dispatch_progress(
                    task, candidate_base, "规划起飞至取货点", f"正在检查候选无人机 {uav['id']}",
                )
                result = plan_route(
                    start, pickup_point, task["cruise_alt"], task["data_mode"],
                    self._planning_obstacles(), BUILDING_STORE,
                    progress_callback=route_progress(0.0, 0.30, "规划起飞至取货点"),
                )
                if self._route_failed(result):
                    failures.append(f"{uav['id']} 前往取货点：{result.get('failure_reason') or '无安全航线'}")
                    continue
                predicted_delivery_start = {
                    "lon": pickup_point["lon"],
                    "lat": pickup_point["lat"],
                    "alt": float(result["route"][-1].get("alt", 25.0)),
                }
                delivery_check = plan_route(
                    predicted_delivery_start, task["destination"], task["cruise_alt"],
                    task["data_mode"], self._planning_obstacles(), BUILDING_STORE,
                    progress_callback=route_progress(0.30, 0.30, "规划取货至终点"),
                )
                if self._route_failed(delivery_check):
                    failures.append(f"{uav['id']} 配送段：{delivery_check.get('failure_reason') or '无安全航线'}")
                    continue
                pickup_plan = self._plan_energy_aware_leg(
                    start, pickup_point, result, uav["battery"], 0.0,
                    self._effective_flight_factor(uav), task["cruise_alt"], task["data_mode"],
                    progress_callback=route_progress(0.60, 0.17, "校验起飞段能耗与换电"),
                )
                if not pickup_plan:
                    failures.append(f"{uav['id']} 当前电量无法安全到达取货点或换电站")
                    continue
                delivery_plan = self._plan_energy_aware_leg(
                    predicted_delivery_start, task["destination"], delivery_check,
                    pickup_plan["ending_battery"], task["payload_kg"],
                    self._effective_flight_factor(uav), task["cruise_alt"], task["data_mode"],
                    progress_callback=route_progress(0.77, 0.20, "校验配送段能耗与换电"),
                )
                if not delivery_plan:
                    failures.append(f"{uav['id']} 取货后无法安全到达终点或换电站")
                    continue
                selected = (uav, pickup_plan, delivery_plan)
                break

            if not selected:
                reason = "；".join(failures[:3]) or "当前没有可执行的安全方案"
                self._block_task(task, reason)
                self._set_dispatch_progress(task, 100, "本次规划未通过", reason, active=False)
                continue

            uav, pickup_plan, delivery_plan = selected

            self._set_dispatch_progress(task, 92, "写入调度方案", "正在保存两段路线并分配无人机")
            self._save_route(task["id"], "pickup", pickup_plan["display_result"])
            self._save_route(task["id"], "delivery", delivery_plan["display_result"])
            self._set_task_route_plan(
                task, uav["id"], pickup_plan["display_result"], delivery_plan["display_result"],
                "任务已分配，已按距离、载重、风况和安全余量完成能耗检查",
            )
            uav.update({
                "state": "BUSY",
                "task_id": task["id"],
                "last_event": "",
                "charge_remaining_s": 0.0,
                "idle_elapsed_s": 0.0,
                "recovery_target": None,
                "recovery_retry_at": 0.0,
            })
            is_handover = task.get("cargo_status") == "WAITING_HANDOVER"
            pickup_phase = "TO_HANDOVER" if is_handover else "TO_PICKUP"
            self._activate_leg_plan(uav, pickup_plan, pickup_phase)
            planned_swap_count = (
                len(pickup_plan["display_result"].get("swap_stops") or [])
                + len(delivery_plan["display_result"].get("swap_stops") or [])
            )
            task.update({
                "status": "HANDOVER_ASSIGNED" if is_handover else "ASSIGNED",
                "assigned_uav": uav["id"],
                "started_at": task.get("started_at") or time.time(),
                "route_method": pickup_plan["display_result"]["method"],
                "selected_altitude": pickup_plan["display_result"].get("selected_altitude"),
                "buildings_considered": pickup_plan["display_result"].get("buildings_considered", 0),
                "blocking_buildings": pickup_plan["display_result"].get("blocking_buildings", 0),
                "estimated_energy_percent": round(
                    float(pickup_plan["display_result"].get("estimated_energy_percent") or 0.0)
                    + float(delivery_plan["display_result"].get("estimated_energy_percent") or 0.0),
                    3,
                ),
                "planned_swap_count": planned_swap_count,
                "blocked_reason": None,
                "retry_at": None,
                "dispatch_attempts": int(task.get("dispatch_attempts", 0)) + 1,
                "planning_retry_count": 0,
                "message": (
                    f"{uav['id']} 先前往 {pickup_plan['station']['name']} 换电，再去{'交接点' if is_handover else '取货点'}"
                    if pickup_plan.get("station")
                    else f"{uav['id']} 正在前往{'货物交接点' if is_handover else '取货点'}"
                ),
            })
            FLIGHT_LOGS.start_or_resume(task, uav, WEATHER_MONITOR.get_state())
            self._event("TASK_ASSIGNED", uav["id"], task["id"], task["message"])
            self._set_dispatch_progress(
                task, 100, "调度完成", f"已分配 {uav['id']}，路线已显示在地图上", active=False,
            )

    @staticmethod
    def _near_point(a: Dict, b: Dict, radius_m: float = 30.0) -> bool:
        return haversine_m(a, b) <= radius_m and float(a.get("alt", 0.0)) <= 5.0

    def _base_at_position(self, uav: Dict) -> Optional[Dict]:
        return next((base for base in SIM_BASES if self._near_point(uav, base)), None)

    def _arrive_at_base(self, uav: Dict, base: Dict, reason: str = "返航完成"):
        """基地既是待命点也是充电点；到达后按缺失电量计算充电时间。"""
        self._reset_uav_leg(uav)
        uav.update({
            "base": base["name"],
            "lon": base["lon"],
            "lat": base["lat"],
            "alt": 0.0,
            "recovery_target": dict(base),
            "recovery_retry_at": 0.0,
            "idle_elapsed_s": 0.0,
        })
        missing = max(0.0, float(BASE_CHARGE_TARGET_PERCENT) - float(uav["battery"]))
        if missing > 1e-6:
            duration = float(BASE_CHARGE_TIME_SECONDS) * missing / max(1.0, float(BASE_CHARGE_TARGET_PERCENT))
            uav.update({
                "state": "CHARGING",
                "phase": "BASE_CHARGING",
                "charge_remaining_s": duration,
                "last_event": f"{reason}，正在 {base['label']} 充电",
            })
            self._event("BASE_CHARGE_STARTED", uav["id"], None, uav["last_event"])
        else:
            uav.update({
                "state": "IDLE",
                "phase": "BASE_IDLE",
                "charge_remaining_s": 0.0,
                "last_event": f"{reason}，已在 {base['label']} 满电待命",
            })
            self._event("RETURN_COMPLETE", uav["id"], None, uav["last_event"])

    def _advance_base_charging(self, uav: Dict, dt_real: float):
        sim_dt = max(0.0, dt_real * self.speed_factor)
        rate = float(BASE_CHARGE_TARGET_PERCENT) / max(1.0, float(BASE_CHARGE_TIME_SECONDS))
        uav["battery"] = min(
            float(BASE_CHARGE_TARGET_PERCENT),
            float(uav["battery"]) + rate * sim_dt,
        )
        uav["charge_remaining_s"] = max(
            0.0,
            (float(BASE_CHARGE_TARGET_PERCENT) - float(uav["battery"])) / rate,
        )
        if uav["battery"] + 1e-6 < float(BASE_CHARGE_TARGET_PERCENT):
            return
        uav.update({
            "battery": float(BASE_CHARGE_TARGET_PERCENT),
            "state": "IDLE",
            "phase": "BASE_IDLE",
            "charge_remaining_s": 0.0,
            "last_event": "基地充电完成，满电待命",
        })
        self._event("BASE_CHARGE_COMPLETED", uav["id"], None, uav["last_event"])

    @staticmethod
    def _cargo_location_on_uav(uav: Dict) -> Dict:
        return {
            "type": "UAV",
            "name": uav["id"],
            "uav_id": uav["id"],
            "lon": float(uav["lon"]),
            "lat": float(uav["lat"]),
            "alt": float(uav.get("alt", 0.0)),
        }

    def _start_cargo_handover_recovery(self, uav: Dict, task: Dict, reason: str) -> bool:
        """携货异常时，把货物和原无人机一起送到可达基地/换电站。"""
        current = {"lon": uav["lon"], "lat": uav["lat"], "alt": uav.get("alt", 0.0)}
        safe_points = [
            {**dict(base), "point_type": "BASE"} for base in SIM_BASES
        ] + [
            {**dict(station), "point_type": "BATTERY_STATION", "label": station["name"]}
            for station in BATTERY_STATIONS
        ]
        safe_points.sort(key=lambda point: haversine_m(current, point))

        # 若异常恰好发生在已落地的安全点，直接确认交接，不再生成零长度航线。
        landed_target = next((point for point in safe_points if self._near_point(uav, point)), None)
        if landed_target:
            self._complete_cargo_handover(uav, task, landed_target)
            return True

        data_mode = "custom" if self._planning_obstacles() else task.get("data_mode", "shenzhen")
        selected = None
        for target in safe_points:
            lower_bound = route_energy_percent(
                [current, {"lon": target["lon"], "lat": target["lat"], "alt": 0.0}],
                task["payload_kg"], self._effective_flight_factor(uav),
            )
            if lower_bound + BATTERY_RESERVE_PERCENT > float(uav["battery"]) + 1e-6:
                continue
            result = plan_route(
                current, target, task["cruise_alt"], data_mode,
                self._planning_obstacles(), BUILDING_STORE,
            )
            if self._route_failed(result):
                continue
            route = self._station_arrival_route(
                self._merge_routes([current], result["route"]), target,
            )
            energy = route_energy_percent(
                route, task["payload_kg"], self._effective_flight_factor(uav),
            )
            if energy + BATTERY_RESERVE_PERCENT <= float(uav["battery"]) + 1e-6:
                display_result = dict(result)
                display_result.update({
                    "route": route,
                    "route_length_m": self._route_length_3d(route),
                    "estimated_energy_percent": round(energy, 3),
                })
                selected = (target, display_result)
                break

        if selected:
            target, result = selected
            uav.update({
                "state": "BUSY",
                "phase": "TO_CARGO_HANDOVER",
                "task_id": task["id"],
                "route": list(result["route"]),
                "route_index": 0,
                "segment_t": 0.0,
                "swap_station": None,
                "swap_remaining_s": 0.0,
                "resume_phase": None,
                "post_swap_route": [],
                "pending_energy_actions": [],
                "handover_target": dict(target),
                "recovery_retry_at": 0.0,
                "last_event": f"携货安全恢复：前往 {target.get('label') or target['name']} 交接",
            })
            task.update({
                "status": "CARGO_RECOVERY",
                "assigned_uav": uav["id"],
                "cargo_status": "IN_RECOVERY",
                "cargo_location": self._cargo_location_on_uav(uav),
                "handover_station": dict(target),
                "blocked_reason": reason,
                "retry_at": None,
                "message": uav["last_event"],
            })
            self._set_cargo_recovery_route(task, uav["id"], result, target, reason)
            self._save_route(task["id"], "cargo_recovery", result)
            self._event("CARGO_RECOVERY_STARTED", uav["id"], task["id"], task["message"])
            return True

        # 无可达交接点时仍由原无人机保管货物，禁止创建虚假的接驳任务。
        uav.update({
            "state": "HOLDING" if float(uav.get("alt", 0.0)) > 5.0 else "GROUNDED",
            "phase": "SAFE_CARGO_HOLD" if float(uav.get("alt", 0.0)) > 5.0 else "SAFE_CARGO_GROUNDED",
            "task_id": task["id"],
            "route": [],
            "route_index": 0,
            "segment_t": 0.0,
            "handover_target": None,
            "recovery_retry_at": time.time() + float(SAFE_RECOVERY_RETRY_SECONDS),
            "last_event": "携货无人机暂无可达交接点，保持货物占用并自动重试安全恢复",
        })
        task.update({
            "status": "CARGO_EMERGENCY",
            "assigned_uav": uav["id"],
            "cargo_status": "IN_RECOVERY",
            "cargo_location": self._cargo_location_on_uav(uav),
            "handover_station": None,
            "blocked_reason": reason,
            "retry_at": time.time() + float(SAFE_RECOVERY_RETRY_SECONDS),
            "message": uav["last_event"],
        })
        self._clear_task_route_plan(task)
        self._event("CARGO_RECOVERY_BLOCKED", uav["id"], task["id"], task["message"])
        return False

    def _complete_cargo_handover(self, uav: Dict, task: Dict, target: Dict):
        """落地确认后释放货物，并允许另一架无人机从交接点接驳。"""
        label = target.get("label") or target.get("name") or "安全交接点"
        location = {
            "type": "HANDOVER",
            "name": label,
            "lon": float(target["lon"]),
            "lat": float(target["lat"]),
            "alt": 0.0,
            "point_type": target.get("point_type"),
        }
        history = list(task.get("handover_history") or [])
        history.append({
            "timestamp": time.time(),
            "from_uav": uav["id"],
            "location": dict(location),
        })
        self._clear_task_route_plan(task)
        task.update({
            "status": "WAITING_HANDOVER",
            "assigned_uav": None,
            "cargo_status": "WAITING_HANDOVER",
            "cargo_location": location,
            "handover_station": dict(target),
            "handover_history": history,
            # 当前承运机先恢复；接驳必须由另一架无人机完成。
            "excluded_uav_ids": [uav["id"]],
            "blocked_reason": None,
            "retry_at": None,
            "message": f"货物已在 {label} 落地，等待其他无人机接驳",
        })
        self._event("CARGO_HANDOVER_READY", uav["id"], task["id"], task["message"])

        if target.get("point_type") == "BASE":
            self._arrive_at_base(uav, target, "携货交接完成")
        else:
            self._reset_uav_leg(uav)
            uav["swap_station"] = dict(target)
            self._begin_recovery_swap(uav)

    def _start_safety_recovery(self, uav: Dict, reason: str) -> bool:
        """为脱离任务的无人机规划到基地；电量不够时先去可达换电站。"""
        current = {"lon": uav["lon"], "lat": uav["lat"], "alt": uav.get("alt", 0.0)}
        current_base = self._base_at_position(uav)
        if current_base:
            self._arrive_at_base(uav, current_base, reason)
            return True

        data_mode = "custom" if self._planning_obstacles() else "shenzhen"
        home_name = uav.get("base")
        bases = sorted(SIM_BASES, key=lambda base: (base["name"] != home_name, haversine_m(current, base)))
        feasible_bases = []
        for base in bases:
            result = plan_route(current, base, 80.0, data_mode, self._planning_obstacles(), BUILDING_STORE)
            if self._route_failed(result):
                continue
            route = self._station_arrival_route(
                self._merge_routes([current], result["route"]), base,
            )
            energy = route_energy_percent(route, 0.0, self._effective_flight_factor(uav))
            if energy + BATTERY_RESERVE_PERCENT <= float(uav["battery"]) + 1e-6:
                feasible_bases.append((haversine_m(current, base), base, route))
        if feasible_bases:
            _, base, route = min(feasible_bases, key=lambda row: row[0])
            self._reset_uav_leg(uav)
            uav.update({
                "state": "BUSY",
                "phase": "RETURNING",
                "route": route,
                "recovery_target": dict(base),
                "recovery_retry_at": 0.0,
                "last_event": f"{reason}，安全返回 {base['label']}",
            })
            self._event("SAFETY_RETURN_STARTED", uav["id"], None, uav["last_event"])
            return True

        # 无法保留安全余量到基地时，先寻找当前电量可达的换电站。
        stations = sorted(BATTERY_STATIONS, key=lambda station: haversine_m(current, station))
        for station in stations[:max(1, int(MAX_STATION_CANDIDATES_TO_PLAN))]:
            result = plan_route(current, station, 80.0, data_mode, self._planning_obstacles(), BUILDING_STORE)
            if self._route_failed(result):
                continue
            route = self._station_arrival_route(
                self._merge_routes([current], result["route"]), station,
            )
            energy = route_energy_percent(route, 0.0, self._effective_flight_factor(uav))
            if energy + BATTERY_RESERVE_PERCENT > float(uav["battery"]) + 1e-6:
                continue
            self._reset_uav_leg(uav)
            uav.update({
                "state": "BUSY",
                "phase": "TO_RECOVERY_SWAP",
                "route": route,
                "swap_station": dict(station),
                "recovery_target": dict(station),
                "recovery_retry_at": 0.0,
                "last_event": f"{reason}，先前往 {station['name']} 换电再返航",
            })
            self._event("RECOVERY_SWAP_PLANNED", uav["id"], None, uav["last_event"])
            return True

        self._reset_uav_leg(uav)
        airborne = float(uav.get("alt", 0.0)) > 5.0
        uav.update({
            "state": "HOLDING" if airborne else "GROUNDED",
            "phase": "SAFE_HOLD" if airborne else "SAFE_GROUNDED",
            "recovery_target": None,
            "recovery_retry_at": time.time() + float(SAFE_RECOVERY_RETRY_SECONDS),
            "last_event": f"{reason}；暂无可达安全点，{'原地悬停' if airborne else '原地停放'}并自动重试",
        })
        self._event("SAFETY_RECOVERY_BLOCKED", uav["id"], None, uav["last_event"])
        return False

    def _abort_task_safely(self, uav: Dict, task: Dict, reason: str):
        """未取货可重派；已取货必须先携货落地交接，不能回原取货点重派。"""
        if task.get("cargo_status") in {"IN_TRANSIT", "IN_RECOVERY"}:
            self._start_cargo_handover_recovery(uav, task, reason)
            return
        self._block_task(task, f"{uav['id']} 执行途中中止：{reason}", uav["id"])
        self._reset_uav_leg(uav)
        self._start_safety_recovery(uav, "任务中止后的安全恢复")

    def _begin_recovery_swap(self, uav: Dict):
        station = uav.get("swap_station") or {}
        uav.update({
            "state": "BUSY",
            "phase": "RECOVERY_SWAPPING",
            "route": [],
            "route_index": 0,
            "segment_t": 0.0,
            "alt": 0.0,
            "swap_remaining_s": float(BATTERY_SWAP_TIME_SECONDS),
            "last_event": f"正在 {station.get('name', '换电站')} 换电，之后返回基地",
        })
        self._event("RECOVERY_SWAP_STARTED", uav["id"], None, uav["last_event"])

    def _advance_recovery_swap(self, uav: Dict, dt_real: float):
        uav["swap_remaining_s"] = max(
            0.0, float(uav.get("swap_remaining_s", 0.0)) - dt_real * self.speed_factor,
        )
        if uav["swap_remaining_s"] > 0.0:
            return
        station = uav.get("swap_station") or {}
        uav["battery"] = float(BATTERY_AFTER_SWAP_PERCENT)
        uav["swap_count"] = int(uav.get("swap_count", 0)) + 1
        self._reset_uav_leg(uav)
        self._event("RECOVERY_SWAP_COMPLETED", uav["id"], None, f"{station.get('name', '换电站')} 换电完成")
        self._start_safety_recovery(uav, "恢复换电完成")

    def _manage_idle_and_recovery(self, dt_real: float):
        now = time.time()
        ready_waiting = any(
            task["status"] in {"WAITING", "WAITING_HANDOVER"}
            or (task["status"] == "WAITING_BLOCKED" and float(task.get("retry_at") or 0.0) <= now)
            for task in self.tasks.values()
        )
        for uav in self.uavs.values():
            if uav["state"] in {"HOLDING", "GROUNDED"}:
                if float(uav.get("recovery_retry_at") or 0.0) <= now:
                    task = self.tasks.get(uav.get("task_id"))
                    if task and task.get("cargo_status") == "IN_RECOVERY":
                        self._start_cargo_handover_recovery(uav, task, "携货安全恢复自动重试")
                    else:
                        self._start_safety_recovery(uav, "安全恢复自动重试")
                continue
            if uav["state"] != "IDLE":
                continue
            base = self._base_at_position(uav)
            if base:
                if float(uav["battery"]) + 1e-6 < float(BASE_CHARGE_TARGET_PERCENT):
                    self._arrive_at_base(uav, base, "基地待命")
                else:
                    uav["phase"] = "BASE_IDLE"
                continue
            uav["idle_elapsed_s"] = float(uav.get("idle_elapsed_s", 0.0)) + dt_real * self.speed_factor
            low_battery = float(uav["battery"]) < float(POST_TASK_RECOVERY_THRESHOLD_PERCENT)
            if low_battery or (not ready_waiting and uav["idle_elapsed_s"] >= float(IDLE_RETURN_DELAY_SECONDS)):
                self._start_safety_recovery(
                    uav,
                    "任务结束后低电量恢复" if low_battery else "任务结束后返回基地待命",
                )

    def _start_delivery_leg(self, uav: Dict, task: Dict):
        pickup_point = task_pickup_point(task)
        is_handover = task.get("cargo_status") == "WAITING_HANDOVER"
        delivery_start = {
            "lon": pickup_point["lon"], "lat": pickup_point["lat"], "alt": uav.get("alt", 0.0),
        }
        result = plan_route(delivery_start, task["destination"], task["cruise_alt"], task["data_mode"], self._planning_obstacles(), BUILDING_STORE)
        self._save_route(task["id"], "delivery", result)
        if self._route_failed(result):
            self._abort_task_safely(uav, task, result.get("failure_reason") or "配送航线规划失败")
            return
        delivery_plan = self._plan_energy_aware_leg(
            delivery_start, task["destination"], result, uav["battery"], task["payload_kg"],
            self._effective_flight_factor(uav), task["cruise_alt"], task["data_mode"],
        )
        if not delivery_plan:
            failure = self._energy_failure("取货后剩余电量不足，且没有能够安全到达的换电站。")
            self._save_route(task["id"], "delivery_energy_failed", failure)
            self._abort_task_safely(uav, task, failure["failure_reason"])
            return
        self._save_route(task["id"], "delivery", delivery_plan["display_result"])
        self._update_task_route_leg(
            task, "pickup_to_destination", delivery_plan["display_result"], uav["id"],
            "取货完成，已按实际剩余电量和当前风况确认配送航线",
        )
        self._activate_leg_plan(uav, delivery_plan, "DELIVERING")
        task["status"] = "IN_PROGRESS"
        task["route_method"] = delivery_plan["display_result"]["method"]
        task["selected_altitude"] = delivery_plan["display_result"].get("selected_altitude")
        task["buildings_considered"] = delivery_plan["display_result"].get("buildings_considered", 0)
        task["blocking_buildings"] = delivery_plan["display_result"].get("blocking_buildings", 0)
        task["estimated_energy_percent"] = delivery_plan["display_result"].get("estimated_energy_percent")
        task["planned_swap_count"] = len(delivery_plan["display_result"].get("swap_stops") or [])
        task["cargo_status"] = "IN_TRANSIT"
        task["cargo_location"] = self._cargo_location_on_uav(uav)
        task["excluded_uav_ids"] = []
        task["message"] = (
            f"{uav['id']} 已{'接驳' if is_handover else '取货'}，先前往 {delivery_plan['station']['name']} 换电"
            if delivery_plan.get("station")
            else f"{uav['id']} 已{'接驳货物' if is_handover else '取货'}，配送中"
        )
        self._event("CARGO_HANDOVER_PICKED_UP" if is_handover else "PICKUP_COMPLETE", uav["id"], task["id"], task["message"])

    def _finish_task(self, uav: Dict, task: Dict):
        task["status"] = "COMPLETED"
        task["completed_at"] = time.time()
        task["message"] = "配送完成"
        task["cargo_status"] = "DELIVERED"
        task["cargo_location"] = {"type": "DESTINATION", **dict(task["destination"]), "alt": 0.0}
        task["excluded_uav_ids"] = []
        self._event("TASK_COMPLETED", uav["id"], task["id"], "任务完成")
        try:
            summary = FLIGHT_LOGS.complete(task, uav, WEATHER_MONITOR.get_state())
            task["flight_log_file"] = summary.get("log_file") if summary else None
        except Exception as exc:
            task["flight_log_file"] = None
            self._log("events.jsonl", {
                "timestamp": time.time(),
                "type": "FLIGHT_LOG_ERROR",
                "uav_id": uav["id"],
                "task_id": task["id"],
                "message": f"完成任务日志保存失败：{exc}",
            })
        uav.update({
            "state": "IDLE", "phase": "IDLE", "task_id": None, "route": [],
            "route_index": 0, "segment_t": 0.0, "alt": 0.0,
            "swap_station": None, "swap_remaining_s": 0.0,
            "resume_phase": None, "post_swap_route": [], "pending_energy_actions": [],
            "idle_elapsed_s": 0.0, "recovery_target": None, "recovery_retry_at": 0.0,
        })

    def _begin_battery_swap(self, uav: Dict, task: Dict):
        station = uav.get("swap_station") or {}
        uav.update({
            "phase": "SWAPPING",
            "route": [],
            "route_index": 0,
            "segment_t": 0.0,
            "alt": 0.0,
            "swap_remaining_s": float(BATTERY_SWAP_TIME_SECONDS),
        })
        task["message"] = f"{uav['id']} 正在 {station.get('name', '换电站')} 换电"
        uav["last_event"] = task["message"]
        self._event("BATTERY_SWAP_STARTED", uav["id"], task["id"], task["message"])

    def _advance_battery_swap(self, uav: Dict, task: Optional[Dict], dt_real: float):
        uav["swap_remaining_s"] = max(
            0.0,
            float(uav.get("swap_remaining_s", 0.0)) - dt_real * self.speed_factor,
        )
        if uav["swap_remaining_s"] > 0.0:
            return
        station = uav.get("swap_station") or {}
        resume_phase = uav.get("resume_phase") or "TO_PICKUP"
        pending_actions = list(uav.get("pending_energy_actions") or [])
        next_action = pending_actions.pop(0) if pending_actions else {
            "route": list(uav.get("post_swap_route") or []),
            "station": None,
        }
        next_station = next_action.get("station")
        uav.update({
            "battery": float(BATTERY_AFTER_SWAP_PERCENT),
            "phase": "TO_SWAP" if next_station else resume_phase,
            "route": list(next_action.get("route") or []),
            "route_index": 0,
            "segment_t": 0.0,
            "swap_station": next_station,
            "resume_phase": resume_phase if next_station else None,
            "post_swap_route": pending_actions[0]["route"] if pending_actions else [],
            "pending_energy_actions": pending_actions,
            "swap_count": int(uav.get("swap_count", 0)) + 1,
        })
        if task:
            task["swap_count"] = int(task.get("swap_count", 0)) + 1
            if next_station:
                task["message"] = f"{station.get('name', '换电站')} 换电完成，继续前往 {next_station['name']}"
            else:
                task["message"] = (
                    f"{station.get('name', '换电站')} 换电完成，继续前往{'货物交接点' if resume_phase == 'TO_HANDOVER' else '取货点'}"
                    if resume_phase in {"TO_PICKUP", "TO_HANDOVER"}
                    else f"{station.get('name', '换电站')} 换电完成，继续配送"
                )
            uav["last_event"] = task["message"]
            self._event("BATTERY_SWAP_COMPLETED", uav["id"], task["id"], task["message"])

    def _advance_uav(self, uav: Dict, dt_real: float):
        if uav["state"] == "CHARGING" and uav.get("phase") == "BASE_CHARGING":
            self._advance_base_charging(uav, dt_real)
            return
        if uav["state"] != "BUSY":
            return
        task = self.tasks.get(uav.get("task_id"))
        if uav.get("phase") == "RECOVERY_SWAPPING":
            self._advance_recovery_swap(uav, dt_real)
            return
        if uav.get("phase") == "SWAPPING":
            self._advance_battery_swap(uav, task, dt_real)
            return
        if not uav.get("route"):
            return
        now = time.monotonic()
        if uav["hold_until"] > now:
            if uav.get("phase") != "HOLD":
                uav["hold_resume_phase"] = uav.get("phase")
            uav["phase"] = "HOLD"
            return
        if uav["phase"] == "HOLD":
            uav["phase"] = uav.pop("hold_resume_phase", None) or (
                "DELIVERING" if task and task["status"] == "IN_PROGRESS" else "TO_PICKUP"
            )

        if task and uav.get("phase") in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING", "TO_SWAP", "TO_CARGO_HANDOVER"}:
            remaining_route = self._remaining_route(uav)
            payload = self._current_payload(task, uav)
            projected = route_energy_percent(remaining_route, payload, self._effective_flight_factor(uav))
            if uav["battery"] + 1e-6 < projected + BATTERY_RESERVE_PERCENT:
                if not self._divert_to_battery_station(uav, task, "实际电量或天气变化触发换电重规划"):
                    failure = self._energy_failure("飞行途中预计电量不足，且当前电量无法安全到达任何换电站。")
                    self._abort_task_safely(uav, task, failure["failure_reason"])
                    return

        route = uav["route"]
        idx = uav["route_index"]
        if idx >= len(route) - 1:
            if uav["phase"] == "RETURNING":
                base = uav.get("recovery_target") or min(SIM_BASES, key=lambda item: haversine_m(uav, item))
                self._arrive_at_base(uav, base, "安全返航完成")
                return
            if uav["phase"] == "TO_RECOVERY_SWAP":
                self._begin_recovery_swap(uav)
                return
            if uav["phase"] == "TO_CARGO_HANDOVER" and task:
                target = uav.get("handover_target") or task.get("handover_station")
                if target:
                    self._complete_cargo_handover(uav, task, target)
                return
            if not task:
                return
            if uav["phase"] == "TO_SWAP":
                self._begin_battery_swap(uav, task)
            elif uav["phase"] in {"TO_PICKUP", "TO_HANDOVER"}:
                self._start_delivery_leg(uav, task)
            elif uav["phase"] == "DELIVERING":
                self._finish_task(uav, task)
            return

        speed = uav["cruise_speed_mps"] * self._effective_flight_factor(uav) * self.speed_factor
        travel_budget = max(0.0, dt_real * speed)
        energy_used = 0.0
        payload = self._current_payload(task, uav) if task else 0.0
        while travel_budget > 1e-6 and uav["route_index"] < len(route) - 1:
            idx = uav["route_index"]
            a, b = route[idx], route[idx + 1]
            horizontal_m = haversine_m(a, b)
            vertical_m = float(b.get("alt", 80.0)) - float(a.get("alt", 80.0))
            segment_m = max(1.0, math.hypot(horizontal_m, vertical_m))
            remaining_fraction = max(0.0, 1.0 - float(uav.get("segment_t", 0.0)))
            move_m = min(travel_budget, segment_m * remaining_fraction)
            fraction = move_m / segment_m
            energy_used += movement_energy_percent(
                horizontal_m * fraction,
                vertical_m * fraction,
                payload,
                self._effective_flight_factor(uav),
            )
            uav["segment_t"] = float(uav.get("segment_t", 0.0)) + fraction
            travel_budget -= move_m
            if uav["segment_t"] >= 1.0 - 1e-9:
                uav["route_index"] += 1
                uav["segment_t"] = 0.0

        if uav["route_index"] >= len(route) - 1:
            last = route[-1]
            uav.update({"lon": last["lon"], "lat": last["lat"], "alt": last.get("alt", 0.0)})
        else:
            idx = uav["route_index"]
            a, b = route[idx], route[idx + 1]
            t = max(0.0, min(1.0, float(uav.get("segment_t", 0.0))))
            uav["lon"] = a["lon"] + (b["lon"] - a["lon"]) * t
            uav["lat"] = a["lat"] + (b["lat"] - a["lat"]) * t
            uav["alt"] = a.get("alt", 80.0) + (b.get("alt", 80.0) - a.get("alt", 80.0)) * t
        uav["battery"] = max(0.0, float(uav["battery"]) - energy_used)
        FLIGHT_LOGS.record_position(task, uav)
        if task and task.get("cargo_status") in {"IN_TRANSIT", "IN_RECOVERY"}:
            task["cargo_location"] = self._cargo_location_on_uav(uav)

        if uav["wind_factor"] < 1.0 and random.random() < 0.05:
            uav["wind_factor"] = min(1.0, uav["wind_factor"] + 0.05)

    def _resolve_conflicts(self):
        """简化 4D 冲突消解。

        V2 不再直接把低优先级 UAV 生硬压到较低高度，因为那可能撞上真实建筑。
        优先策略：
        1. 较低优先级 UAV 尝试在更高首选航层上重新规划；
        2. 已接近 120m 时采用短时 HOLD 让行。
        新路线仍经过深圳 Height 建筑库校验。
        """
        active = [u for u in self.uavs.values() if u["state"] == "BUSY"]
        rank = {"LOW": 1, "NORMAL": 2, "HIGH": 3, "EMERGENCY": 4}
        now = time.monotonic()
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                a, b = active[i], active[j]
                horizontal = haversine_m(a, b)
                vertical = abs(a["alt"] - b["alt"])
                if horizontal >= 100.0 or vertical >= 22.0 or a["alt"] <= 10 or b["alt"] <= 10:
                    continue
                ta = self.tasks.get(a.get("task_id"), {})
                tb = self.tasks.get(b.get("task_id"), {})
                loser = a if rank.get(ta.get("priority"), 2) < rank.get(tb.get("priority"), 2) else b
                if now - loser.get("last_conflict_action", 0.0) < 8.0:
                    continue
                loser["last_conflict_action"] = now
                task = self.tasks.get(loser.get("task_id"))
                if not task:
                    continue
                # 换电途中已有专用后续路线；不让普通冲突逻辑破坏换电状态机。
                if loser.get("phase") not in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING"}:
                    continue

                if loser["alt"] < 105.0:
                    target = task_pickup_point(task) if loser["phase"] in {"TO_PICKUP", "TO_HANDOVER"} else task["destination"]
                    preferred = min(120.0, max(float(task.get("cruise_alt", 80.0)), loser["alt"] + 20.0))
                    current = {"lon": loser["lon"], "lat": loser["lat"], "alt": loser["alt"]}
                    result = plan_route(current, target, preferred, task.get("data_mode", "shenzhen"), self._planning_obstacles(), BUILDING_STORE)
                    if self._route_failed(result):
                        loser["hold_until"] = max(loser.get("hold_until", 0.0), now + 4.0)
                        loser["last_event"] = "冲突消解：安全重规划失败，短时悬停让行"
                    else:
                        loser.update({"route": result["route"], "route_index": 0, "segment_t": 0.0})
                        leg_name = "takeoff_to_pickup" if loser["phase"] in {"TO_PICKUP", "TO_HANDOVER"} else "pickup_to_destination"
                        self._update_task_route_leg(
                            task, leg_name, result, loser["id"],
                            "飞行冲突触发剩余航段重规划",
                        )
                        self._save_route(task["id"], f"{loser['phase'].lower()}_replanned", result)
                        loser["last_event"] = f"冲突消解：建筑感知重规划，目标航层 {result.get('selected_altitude', preferred):.0f}m"
                else:
                    loser["hold_until"] = max(loser.get("hold_until", 0.0), now + 4.0)
                    loser["last_event"] = "冲突消解：高航层无法继续上调，短时悬停让行"
                self._event("CONFLICT_RESOLVED", loser["id"], loser.get("task_id"), loser["last_event"])

    def trigger_event(self, event_type: str, uav_id: Optional[str] = None) -> Dict:
        candidates = [u for u in self.uavs.values() if u["state"] == "BUSY" and u.get("task_id")]
        if not candidates:
            return {"ok": False, "message": "当前没有飞行中的无人机"}
        uav = self.uavs.get(uav_id) if uav_id else random.choice(candidates)
        if not uav or uav["state"] != "BUSY" or not uav.get("task_id"):
            return {"ok": False, "message": "指定无人机当前未执行任务"}
        task = self.tasks.get(uav["task_id"])

        if event_type == "wind":
            uav["wind_factor"] = 0.65
            uav["last_event"] = "阵风：临时降速并增加能耗"
        elif event_type == "comm_loss":
            uav["hold_until"] = time.monotonic() + 6.0
            uav["last_event"] = "通信短时丢失：执行悬停，6 秒后恢复"
        elif event_type == "low_battery":
            uav["battery"] = min(uav["battery"], 19.0)
            if task and uav.get("phase") == "SWAPPING":
                uav["last_event"] = "低电量告警：无人机已在换电站执行换电"
            elif task and self._divert_to_battery_station(uav, task, "低电量告警触发换电重规划"):
                if uav.get("phase") == "TO_SWAP":
                    station = uav.get("swap_station") or {}
                    uav["last_event"] = f"低电量：已改道 {station.get('name', '换电站')}"
                else:
                    uav["last_event"] = "低电量：核算后仍可保留安全余量完成当前航段"
            else:
                if task:
                    self._abort_task_safely(uav, task, "低电量且无法安全继续当前任务")
        elif event_type == "temp_no_fly":
            # 在当前前方约 250m 位置生成一个临时圆形禁飞区，并重规划剩余配送路线。
            center_lon = uav["lon"] + 0.0022
            center_lat = uav["lat"] + 0.0010
            ring = []
            for k in range(16):
                ang = 2 * math.pi * k / 16
                ring.append((center_lon + math.cos(ang) * 0.0015, center_lat + math.sin(ang) * 0.0013))
            self.obstacles.append(Obstacle(kind="no_fly", polygon=ring, height=10_000, name="TEMP_NFZ"))
            self.custom_features.append({
                "type": "Feature",
                "properties": {"kind": "no_fly", "name": "临时禁飞区", "temporary": True},
                "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in ring] + [[ring[0][0], ring[0][1]]]]},
            })
            if task and uav["phase"] in {"TO_PICKUP", "TO_HANDOVER", "DELIVERING", "TO_SWAP", "TO_CARGO_HANDOVER"}:
                if not self._divert_to_battery_station(uav, task, "临时禁飞区触发航线与能耗重规划"):
                    failure = self._energy_failure("临时禁飞区启用后，未找到可满足电量与建筑约束的剩余航线。")
                    self._abort_task_safely(uav, task, failure["failure_reason"])
                else:
                    uav["last_event"] = task.get("message") or "临时禁飞区：已动态重规划"
            else:
                uav["last_event"] = "临时禁飞区已启用"
        else:
            return {"ok": False, "message": f"未知事件：{event_type}"}

        self._event("SIM_EVENT", uav["id"], uav.get("task_id"), uav["last_event"])
        return {"ok": True, "uav_id": uav["id"], "message": uav["last_event"]}

    async def _run_dispatch_cycle(self):
        """在线程中执行串行调度，使进度接口和页面在规划期间仍可响应。"""
        if getattr(self, "_dispatch_worker_active", False):
            return
        self._dispatch_worker_active = True
        try:
            await asyncio.to_thread(self._assign_waiting)
        finally:
            self._dispatch_worker_active = False

    async def run(self):
        self._last_tick = time.monotonic()
        while True:
            now = time.monotonic()
            dt = min(1.0, now - self._last_tick)
            self._last_tick = now
            if self.running:
                self._apply_inflight_weather_policy()
                await self._run_dispatch_cycle()
                for uav in self.uavs.values():
                    self._advance_uav(uav, dt)
                # 刚完成任务的无人机优先从当前位置无缝衔接等待任务。
                await self._run_dispatch_cycle()
                self._manage_idle_and_recovery(dt)
                self._resolve_conflicts()
                if self.auto_events and now > self._event_cooldown and random.random() < 0.004:
                    self.trigger_event(random.choice(["wind", "comm_loss", "temp_no_fly"]))
                    self._event_cooldown = now + 12.0
                self._write_snapshot()
                await self.broadcast()
            await asyncio.sleep(0.5)

    async def broadcast(self):
        if not self._subscribers:
            return
        text = json.dumps({"type": "state", **self.get_public_state()}, ensure_ascii=False)
        dead = []
        for ws in list(self._subscribers):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)

    def subscribe(self, ws):
        self._subscribers.add(ws)

    def unsubscribe(self, ws):
        self._subscribers.discard(ws)

    def _event(self, event_type: str, uav_id: Optional[str], task_id: Optional[str], message: str):
        row = {"timestamp": time.time(), "type": event_type, "uav_id": uav_id, "task_id": task_id, "message": message}
        self._log("events.jsonl", row)
        FLIGHT_LOGS.record_event(event_type, task_id, self.uavs.get(uav_id) if uav_id else None, message)

    def _log(self, filename: str, row: Dict):
        with (LOG_DIR / filename).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _write_snapshot(self):
        (RUNTIME / "state.json").write_text(json.dumps(self.get_public_state(), ensure_ascii=False, indent=2), encoding="utf-8")
