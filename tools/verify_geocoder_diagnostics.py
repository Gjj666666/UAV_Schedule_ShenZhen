#!/usr/bin/env python3
"""离线验证高德诊断状态；不会读取或调用真实 Key。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geocoder


def main() -> None:
    geocoder.AMAP_WEB_SERVICE_KEY = ""
    assert geocoder.search_amap_places("no-key-test") == []
    diagnostic = geocoder.get_last_geocoder_diagnostic()
    assert diagnostic["configured"] is False
    assert diagnostic["attempted"] is False

    geocoder.AMAP_WEB_SERVICE_KEY = "secret-test-key"
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "status": "1",
        "pois": [{
            "id": "test-1",
            "name": "Test POI",
            "location": "114.035529,22.609908",
            "cityname": "Shenzhen",
            "adcode": "440309",
        }],
    }).encode()
    with patch.object(geocoder, "urlopen", return_value=response):
        candidates = geocoder.search_amap_places("success-test")
    diagnostic = geocoder.get_last_geocoder_diagnostic()
    assert len(candidates) == 1
    assert diagnostic["success"] is True
    assert diagnostic["candidate_count"] == 1
    assert "secret-test-key" not in str(diagnostic)

    with patch.object(geocoder, "urlopen", side_effect=URLError("TLS test failure")):
        try:
            geocoder.search_amap_places("failure-test")
        except geocoder.GeocoderServiceError:
            pass
        else:
            raise AssertionError("连接失败时应抛出 GeocoderServiceError")
    diagnostic = geocoder.get_last_geocoder_diagnostic()
    assert diagnostic["attempted"] is True
    assert diagnostic["success"] is False
    assert "TLS test failure" in diagnostic["error"]
    assert "secret-test-key" not in str(diagnostic)
    print("Geocoder diagnostics verification passed.")


if __name__ == "__main__":
    main()
