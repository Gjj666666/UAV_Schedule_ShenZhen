#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳低空多无人机智能调度 Demo 后端。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from simulator import DispatchSystem, FLIGHT_LOGS, ROOT, UPLOAD_DIR
from rules import DEMO_RULES, validate_task
from building_store import BUILDING_STORE
from planner import BUILDING_VERTICAL_CLEARANCE_M, STANDARD_ALT_LAYERS
from ai_dispatcher import AIConfigError, AIServiceError, get_ai_status, parse_task_text, parse_tasks_text
from batch_tasks import BatchFileError, MAX_BATCH_TASKS, parse_task_file
from geocoder import (
    GeocoderServiceError,
    get_geocoder_status,
    get_last_geocoder_diagnostic,
    search_amap_places,
)
from weather_service import WEATHER_MONITOR

system = DispatchSystem()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(system.run())
    weather_task = asyncio.create_task(WEATHER_MONITOR.run(system.broadcast))
    try:
        yield
    finally:
        task.cancel()
        weather_task.cancel()


app = FastAPI(title="Shenzhen UAV Dispatch Demo", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


class Point(BaseModel):
    name: str = ""
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class TaskIn(BaseModel):
    origin: Point
    destination: Point
    delivery_type: Literal["MEDICAL", "EMERGENCY", "PARCEL", "COLD_CHAIN", "DOCUMENT"] = "PARCEL"
    priority: Literal["LOW", "NORMAL", "HIGH", "EMERGENCY"] = "NORMAL"
    payload_kg: float = Field(default=1.0, gt=0, le=5.0)
    deadline_minutes: int = Field(default=45, ge=10, le=240)
    cruise_alt: float = Field(default=80, ge=50, le=120)
    data_mode: Literal["shenzhen", "cesium", "custom"] = "shenzhen"


class BuildingRouteIn(BaseModel):
    start: Point
    end: Point
    buffer_m: float = Field(default=650, ge=100, le=2000)
    max_features: int = Field(default=1200, ge=100, le=2500)
    min_height: float = Field(default=3.0, ge=0, le=300)


class BuildingBBoxIn(BaseModel):
    min_lon: float = Field(ge=-180, le=180)
    min_lat: float = Field(ge=-90, le=90)
    max_lon: float = Field(ge=-180, le=180)
    max_lat: float = Field(ge=-90, le=90)
    min_height: float = Field(default=1.0, ge=0, le=300)
    max_features: int = Field(default=6000, ge=100, le=12000)


class FleetIn(BaseModel):
    count: int = Field(default=6, ge=1, le=1000)


class ControlIn(BaseModel):
    running: Optional[bool] = None
    speed_factor: Optional[float] = Field(default=None, ge=0.5, le=30)
    auto_events: Optional[bool] = None


class EventIn(BaseModel):
    event_type: Literal["wind", "comm_loss", "low_battery", "temp_no_fly"]
    uav_id: Optional[str] = None


class AIParseIn(BaseModel):
    text: str = Field(min_length=4, max_length=2000)


class AIBatchParseIn(BaseModel):
    text: str = Field(min_length=8, max_length=12000)


class BatchTasksIn(BaseModel):
    tasks: List[TaskIn] = Field(min_length=1, max_length=MAX_BATCH_TASKS)


class SimulatedWeatherIn(BaseModel):
    weather: str = Field(min_length=1, max_length=30)
    temperature_c: float = Field(default=27.0, ge=-60, le=70)
    humidity_percent: float = Field(default=70.0, ge=0, le=100)
    wind_direction: str = Field(default="东南", max_length=20)
    wind_power: str = Field(default="≤3", min_length=1, max_length=20)


class AirspaceApprovalIn(BaseModel):
    approved: bool


def _task_safety_errors(payload: dict) -> List[str]:
    errors = list(validate_task(payload))
    for label, point in (("起点", payload["origin"]), ("终点", payload["destination"])):
        restrictions = system.airspace_restrictions_at_point(point)
        if restrictions:
            names = "、".join(zone["name"] for zone in restrictions[:3])
            errors.append(f"{label}位于当前禁止进入的空域：{names}")
        buildings = BUILDING_STORE.buildings_at_point(point["lon"], point["lat"])
        if not buildings:
            continue
        highest = buildings[0]
        required_altitude = float(highest["height"]) + BUILDING_VERTICAL_CLEARANCE_M
        if required_altitude > max(STANDARD_ALT_LAYERS):
            errors.append(
                f"{label}“{point.get('name') or '未命名地点'}”位于 {highest['height']:.1f}m 建筑内，"
                f"加安全余量后超过 {max(STANDARD_ALT_LAYERS):.0f}m 最高航层；"
                "请改用附近开阔起降点"
            )
    return errors


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/state")
def state():
    return system.get_public_state()


@app.get("/api/dispatch/progress")
def dispatch_progress():
    """轻量进度接口；路径规划在线程运行时可持续响应。"""
    return system.get_dispatch_progress()


@app.get("/api/weather")
def weather_status():
    return WEATHER_MONITOR.get_state()


@app.post("/api/weather/refresh")
async def refresh_weather():
    weather = await asyncio.to_thread(WEATHER_MONITOR.refresh)
    await system.broadcast()
    return weather


@app.post("/api/weather/simulated")
async def set_simulated_weather(payload: SimulatedWeatherIn):
    try:
        weather = WEATHER_MONITOR.set_simulated_weather(**payload.model_dump())
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    await system.broadcast()
    return weather


@app.get("/api/airspace/zones")
def airspace_zones():
    return system.airspace_state()


@app.post("/api/airspace/zones/{zone_id}/approval")
async def set_airspace_approval(zone_id: str, payload: AirspaceApprovalIn):
    try:
        state = system.set_airspace_approval(zone_id, payload.approved)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await system.broadcast()
    return state


@app.get("/api/flight-logs")
def flight_log_list(limit: int = Query(default=50, ge=1, le=500)):
    logs = FLIGHT_LOGS.list_summaries(limit=limit)
    return {"count": len(logs), "logs": logs}


@app.get("/api/flight-logs/summary.csv")
def download_flight_log_summary():
    if not FLIGHT_LOGS.summary_path.exists():
        raise HTTPException(404, "当前还没有已完成任务的飞行日志汇总。")
    return FileResponse(
        FLIGHT_LOGS.summary_path,
        media_type="text/csv; charset=utf-8",
        filename="flight_summary.csv",
    )


@app.get("/api/flight-logs/{task_id}/download")
def download_flight_log(task_id: str):
    path = FLIGHT_LOGS.get_log_path(task_id)
    if not path:
        raise HTTPException(404, "没有找到该任务的已完成飞行日志。")
    return FileResponse(path, media_type="application/json", filename=path.name)


@app.get("/api/flight-logs/{task_id}")
def flight_log_detail(task_id: str):
    log = FLIGHT_LOGS.read_log(task_id)
    if log is None:
        raise HTTPException(404, "没有找到该任务的已完成飞行日志。")
    return log


@app.post("/api/fleet/init")
def init_fleet(body: FleetIn):
    if system.get_dispatch_progress()["busy"]:
        raise HTTPException(409, "调度方案正在计算，请等待当前规划结束后再初始化机队。")
    if any(t["status"] in {"ASSIGNED", "IN_PROGRESS"} for t in system.tasks.values()):
        raise HTTPException(409, "有任务正在执行，请先重置系统再修改无人机数量。")
    system.init_fleet(body.count)
    return system.get_public_state()


@app.post("/api/tasks")
def create_task(body: TaskIn):
    payload = body.model_dump()
    if not BUILDING_STORE.available:
        raise HTTPException(503, "深圳建筑数据不可用，无法进行安全航线规划。")
    errors = _task_safety_errors(payload)
    if errors:
        raise HTTPException(400, "；".join(errors))
    return system.create_task(payload)


@app.post("/api/tasks/batch/parse-file")
async def parse_batch_task_file(file: UploadFile = File(...)):
    filename = Path(file.filename or "").name
    if not filename.lower().endswith((".xlsx", ".csv", ".json")):
        raise HTTPException(400, "仅支持 .xlsx、.csv、.json 任务文件。")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "批量任务文件不能超过 5MB。")
    try:
        return parse_task_file(filename, raw)
    except BatchFileError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks/batch")
async def create_tasks_batch(body: BatchTasksIn):
    if not BUILDING_STORE.available:
        raise HTTPException(503, "深圳建筑数据不可用，无法进行安全航线规划。")
    payloads = [task.model_dump() for task in body.tasks]
    invalid = []
    for index, payload in enumerate(payloads, start=1):
        errors = _task_safety_errors(payload)
        if errors:
            invalid.append({"index": index, "errors": errors})
    if invalid:
        raise HTTPException(400, {"message": "批量任务安全校验未通过，未创建任何任务。", "invalid": invalid})
    created = [system.create_task(payload) for payload in payloads]
    await system.broadcast()
    return {
        "ok": True,
        "created_count": len(created),
        "task_ids": [task["id"] for task in created],
        "tasks": created,
    }


@app.post("/api/tasks/batch/validate")
def validate_tasks_batch(body: BatchTasksIn):
    if not BUILDING_STORE.available:
        raise HTTPException(503, "深圳建筑数据不可用，无法进行批量安全校验。")
    results = []
    for index, task in enumerate(body.tasks):
        errors = _task_safety_errors(task.model_dump())
        results.append({"index": index, "valid": not errors, "errors": errors})
    return {
        "total": len(results),
        "valid_count": sum(item["valid"] for item in results),
        "invalid_count": sum(not item["valid"] for item in results),
        "results": results,
    }


@app.get("/api/ai/status")
def ai_status():
    return get_ai_status()


@app.post("/api/ai/parse-task")
async def ai_parse_task(body: AIParseIn):
    try:
        return await asyncio.to_thread(parse_task_text, body.text)
    except AIConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    except AIServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/ai/parse-tasks")
async def ai_parse_tasks(body: AIBatchParseIn):
    try:
        return await asyncio.to_thread(parse_tasks_text, body.text)
    except AIConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    except AIServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/geocode/status")
def geocode_status():
    return get_geocoder_status()


@app.get("/api/geocode/diagnostics")
def geocode_diagnostics():
    return get_last_geocoder_diagnostic()


@app.get("/api/geocode/search")
async def geocode_search(q: str = Query(min_length=1, max_length=120)):
    try:
        candidates = await asyncio.to_thread(search_amap_places, q)
        return {
            "query": q,
            "provider": "amap" if get_geocoder_status()["amap_configured"] else "none",
            "candidates": candidates,
            "diagnostic": get_last_geocoder_diagnostic(),
        }
    except GeocoderServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/rules")
def rules():
    return {"profile": "SHENZHEN_SIM_DEMO", "rules": DEMO_RULES}


@app.get("/api/buildings/stats")
def building_stats():
    return BUILDING_STORE.stats()


@app.post("/api/buildings/near-route")
def buildings_near_route(body: BuildingRouteIn):
    if not BUILDING_STORE.available:
        raise HTTPException(503, "深圳建筑数据或空间索引未找到。")
    return BUILDING_STORE.geojson_near_route(
        body.start.model_dump(),
        body.end.model_dump(),
        buffer_m=body.buffer_m,
        max_features=body.max_features,
        min_height=body.min_height,
    )


@app.post("/api/buildings/in-bbox")
def buildings_in_bbox(body: BuildingBBoxIn):
    if not BUILDING_STORE.available:
        raise HTTPException(503, "深圳建筑数据或空间索引未找到。")
    if body.min_lon >= body.max_lon or body.min_lat >= body.max_lat:
        raise HTTPException(400, "建筑视野 bbox 无效。")
    return BUILDING_STORE.geojson_in_bbox(
        body.min_lon, body.min_lat, body.max_lon, body.max_lat,
        min_height=body.min_height, max_features=body.max_features,
    )


@app.post("/api/control")
def control(body: ControlIn):
    system.set_control(body.running, body.speed_factor, body.auto_events)
    return system.get_public_state()


@app.post("/api/reset")
def reset():
    if system.get_dispatch_progress()["busy"]:
        raise HTTPException(409, "调度方案正在计算，请等待当前规划结束后再重置。")
    system.reset_runtime()
    return system.get_public_state()


@app.post("/api/events")
def event(body: EventIn):
    return system.trigger_event(body.event_type, body.uav_id)


@app.post("/api/data/upload")
async def upload_data(
    file: UploadFile = File(...),
    kind: Literal["auto", "building", "no_fly", "landing_site"] = Query("auto"),
):
    if not file.filename.lower().endswith((".geojson", ".json")):
        raise HTTPException(400, "第一版仅支持 GeoJSON/JSON。")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "Demo 单文件限制 20MB。大型深圳建筑数据建议后续转 3D Tiles/PostGIS。")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"JSON 解析失败：{exc}")
    safe_name = Path(file.filename).name
    (UPLOAD_DIR / safe_name).write_bytes(raw)
    return system.add_custom_geojson(obj, kind)


@app.get("/api/data/custom")
def get_custom_data():
    return system.custom_geojson()


@app.delete("/api/data/custom")
def clear_custom_data():
    system.clear_custom_data()
    return {"ok": True}


@app.websocket("/ws/telemetry")
async def telemetry(ws: WebSocket):
    await ws.accept()
    system.subscribe(ws)
    try:
        await ws.send_text(json.dumps({"type": "state", **system.get_public_state()}, ensure_ascii=False))
        while True:
            # 前端无需不断发消息；receive 保持连接，同时允许 ping 文本。
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        system.unsubscribe(ws)
