#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳地点搜索服务：高德 POI 查询并统一为 WGS84。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_config import AMAP_WEB_SERVICE_KEY


AMAP_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
SHENZHEN_BOUNDS = (113.70, 22.40, 114.70, 22.90)
_DIAGNOSTIC_LOCK = Lock()
_LAST_DIAGNOSTIC: Dict = {
    "provider": "amap",
    "configured": bool(AMAP_WEB_SERVICE_KEY.strip()),
    "attempted": False,
    "success": False,
    "query": "",
    "candidate_count": 0,
    "info": "尚未发起高德地点查询",
    "error": "",
    "checked_at": None,
}


class GeocoderServiceError(RuntimeError):
    pass


def _set_diagnostic(**values) -> Dict:
    diagnostic = {
        "provider": "amap",
        "configured": bool(AMAP_WEB_SERVICE_KEY.strip()),
        "attempted": False,
        "success": False,
        "query": "",
        "candidate_count": 0,
        "info": "",
        "error": "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    diagnostic.update(values)
    with _DIAGNOSTIC_LOCK:
        _LAST_DIAGNOSTIC.clear()
        _LAST_DIAGNOSTIC.update(diagnostic)
    # 服务端也保留一行不含 Key 的明确输出，便于部署环境查看。
    state = "SUCCESS" if diagnostic["success"] else "FAILED"
    print(
        f"[AMAP_GEOCODER] {state} query={diagnostic['query']!r} "
        f"candidates={diagnostic['candidate_count']} info={diagnostic['info']!r} "
        f"error={diagnostic['error']!r}",
        flush=True,
    )
    return dict(diagnostic)


def get_last_geocoder_diagnostic() -> Dict:
    with _DIAGNOSTIC_LOCK:
        return dict(_LAST_DIAGNOSTIC)


def get_geocoder_status() -> Dict:
    return {
        "amap_configured": bool(AMAP_WEB_SERVICE_KEY.strip()),
        "primary_provider": "高德深圳 POI" if AMAP_WEB_SERVICE_KEY.strip() else "Cesium ion（前端回退）",
        "city": "深圳市",
        "output_coordinate_system": "WGS84",
    }


def _transform_lat(lon: float, lat: float) -> float:
    value = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat
    value += 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    value += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    value += (160.0 * math.sin(lat / 12.0 * math.pi) + 320.0 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return value


def _transform_lon(lon: float, lat: float) -> float:
    value = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon
    value += 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
    value += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(lon * math.pi) + 40.0 * math.sin(lon / 3.0 * math.pi)) * 2.0 / 3.0
    value += (150.0 * math.sin(lon / 12.0 * math.pi) + 300.0 * math.sin(lon / 30.0 * math.pi)) * 2.0 / 3.0
    return value


def gcj02_to_wgs84(lon: float, lat: float) -> Tuple[float, float]:
    """将高德返回的 GCJ-02 近似反算为项目统一使用的 WGS84。"""
    earth_radius = 6378245.0
    eccentricity = 0.00669342162296594323
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1.0 - eccentricity * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = d_lat * 180.0 / ((earth_radius * (1.0 - eccentricity)) / (magic * sqrt_magic) * math.pi)
    d_lon = d_lon * 180.0 / (earth_radius / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lon * 2.0 - (lon + d_lon), lat * 2.0 - (lat + d_lat)


def _text(value) -> str:
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "").strip()


def _in_shenzhen(lon: float, lat: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = SHENZHEN_BOUNDS
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def search_amap_places(query: str, limit: int = 8) -> List[Dict]:
    key = AMAP_WEB_SERVICE_KEY.strip()
    if not key:
        _set_diagnostic(
            configured=False,
            attempted=False,
            query=query.strip(),
            info="未配置高德 Web 服务 Key",
        )
        return []

    params = {
        "key": key,
        "keywords": query.strip(),
        "city": "深圳",
        "citylimit": "true",
        "offset": str(max(1, min(limit, 20))),
        "page": "1",
        "extensions": "all",
        "output": "json",
    }
    request = Request(
        f"{AMAP_SEARCH_URL}?{urlencode(params)}",
        headers={"User-Agent": "Shenzhen-UAV-Dispatch/1.0"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # HTTPError 可能包含带 Key 的完整请求 URL，因此这里只记录状态码。
        message = f"HTTP {exc.code}"
        _set_diagnostic(attempted=True, query=query.strip(), info="高德 HTTP 请求失败", error=message)
        raise GeocoderServiceError(f"高德地点服务连接失败：{message}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", None)
        message = str(reason or exc)
        _set_diagnostic(attempted=True, query=query.strip(), info="高德网络请求失败", error=message)
        raise GeocoderServiceError(f"高德地点服务连接失败：{message}") from exc

    if payload.get("status") != "1":
        message = payload.get("info") or "未知错误"
        error_code = payload.get("infocode") or ""
        safe_error = f"{message} ({error_code})" if error_code else str(message)
        _set_diagnostic(attempted=True, query=query.strip(), info="高德接口拒绝请求", error=safe_error)
        raise GeocoderServiceError(f"高德地点服务返回错误：{message}")

    candidates: List[Dict] = []
    seen = set()
    for poi in payload.get("pois") or []:
        location = _text(poi.get("location"))
        try:
            gcj_lon, gcj_lat = (float(part) for part in location.split(",", 1))
        except (TypeError, ValueError):
            continue
        lon, lat = gcj02_to_wgs84(gcj_lon, gcj_lat)
        adcode = _text(poi.get("adcode"))
        city = _text(poi.get("cityname"))
        if not (adcode.startswith("4403") or "深圳" in city or _in_shenzhen(lon, lat)):
            continue
        name = _text(poi.get("name")) or query.strip()
        dedupe_key = (name, round(lon, 6), round(lat, 6))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        district = _text(poi.get("adname"))
        address = _text(poi.get("address"))
        candidates.append({
            "id": _text(poi.get("id")) or f"amap-{len(candidates)}",
            "name": name,
            "address": " · ".join(part for part in (city, district, address) if part),
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "provider": "高德地图",
            "coordinate_system": "WGS84",
        })
    _set_diagnostic(
        attempted=True,
        success=True,
        query=query.strip(),
        candidate_count=len(candidates),
        info="高德接口调用成功",
    )
    return candidates
