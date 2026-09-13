(() => {
  const $ = (id) => document.getElementById(id);
  const appConfig = window.SHENZHEN_UAV_CONFIG || {};
  const configuredIonToken = String(appConfig.cesiumIonToken || "").trim();
  if (configuredIonToken) Cesium.Ion.defaultAccessToken = configuredIonToken;

  const viewer = new Cesium.Viewer("cesiumContainer", {
    baseLayer: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    animation: false,
    timeline: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    baseLayerPicker: false,
    navigationHelpButton: false,
    infoBox: false,
    selectionIndicator: false,
    fullscreenButton: false,
  });
  if (configuredIonToken) $("ionToken").value = configuredIonToken;
  // 地表底图固定为 Cesium ion World Imagery，并与地点搜索和 OSM 3D Buildings 共用 ion Token。
  let baseMapLayer = null;
  viewer.scene.globe.depthTestAgainstTerrain = true;
  viewer.scene.fog.enabled = true;

  const SHENZHEN = { lon: 114.0579, lat: 22.5431 };
  const SHENZHEN_BOUNDS = { minLon: 113.70, minLat: 22.40, maxLon: 114.70, maxLat: 22.90 };
  const COLORS = ["#ffd166", "#4cc9f0", "#90be6d", "#f8961e", "#c77dff", "#ff758f", "#48cae4", "#b8f2e6"];
  // 批量地点解析采用低频串行请求，避免免费额度较低时触发高德账号级 QPS 限流。
  const AMAP_MIN_REQUEST_INTERVAL_MS = 400;
  const AMAP_RATE_LIMIT_RETRY_DELAYS_MS = [1200, 2500, 5000];
  let lastAmapRequestStartedAt = 0;
  let amapRequestQueue = Promise.resolve();
  let osmBuildings = null;
  let customEntities = [];
  let airspaceEntities = [];
  let airspaceRenderSignature = "";
  // 本地建筑不再只沿任务航线加载，而是按当前 Cesium 视野动态流式加载。
  // Map<record_id, Entity[]> 便于视野变化时只保留当前区域建筑。
  const localBuildingEntities = new Map();
  // 同步保存已加载建筑的轻量包围盒，供跟随相机做屋顶高度避让。
  const localBuildingVolumes = new Map();
  let buildingViewBusy = false;
  let lastBuildingViewKey = "";
  let buildingReloadTimer = null;
  let ws = null;
  let currentState = { tasks: [], uavs: [], running: false };
  let aiTaskDraft = null;
  let batchTaskDrafts = [];
  const uavGraphics = new Map();
  const taskRouteGraphics = new Map();
  const batteryStationGraphics = new Map();
  const uavBaseGraphics = new Map();
  let followedUavId = "";

  function focusShenzhen() {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(SHENZHEN.lon, SHENZHEN.lat, 14500),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(-48), roll: 0 },
      duration: 1.4,
    });
  }
  focusShenzhen();


  function setBaseMapStatus(text, cls = "status") {
    const el = $("baseMapStatus");
    if (!el) return;
    el.textContent = text;
    el.className = cls;
  }

  async function loadCesiumIonBaseMap() {
    setBaseMapStatus("正在加载 Cesium ion World Imagery…");
    try {
      const token = configuredIonToken || $("ionToken").value.trim();
      if (!token) throw new Error("未配置 Cesium ion Token，请先在 web/config.js 中填写 cesiumIonToken");
      Cesium.Ion.defaultAccessToken = token;
      viewer.imageryLayers.removeAll(false);
      const provider = await Cesium.createWorldImageryAsync({
        style: Cesium.IonWorldImageryStyle.AERIAL,
      });
      provider.errorEvent.addEventListener((error) => {
        console.error("Cesium ion imagery error", error);
        setBaseMapStatus("Cesium ion 地表影像瓦片加载失败，请检查 Token、网络或 ion 配额。", "status bad");
      });
      baseMapLayer = new Cesium.ImageryLayer(provider);
      viewer.imageryLayers.add(baseMapLayer);
      setBaseMapStatus("Cesium ion World Imagery 已自动加载。", "status ok");
      viewer.scene.requestRender();
    } catch (e) {
      console.error("Base map load failed", e);
      setBaseMapStatus(`Cesium ion 地表影像加载失败：${e.message}`, "status bad");
    }
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, options);
    let data;
    try { data = await resp.json(); } catch { data = {}; }
    if (!resp.ok) {
      const detail = data.detail || data.message || `HTTP ${resp.status}`;
      const message = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
      throw new Error(message);
    }
    return data;
  }

  function wait(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  }

  function isAmapRateLimitError(error) {
    const message = String(error?.message || error || "");
    return /(?:10004|10014|10015|10019|10020|10021|10023|10029)|(?:C(?:U|K|I|IK)?QPS_HAS_EXCEEDED_THE_LIMIT)|(?:QPS[^；。]*(?:超限|limit))/i.test(message);
  }

  async function pacedAmapRequest(path) {
    const request = amapRequestQueue.then(async () => {
      const elapsed = Date.now() - lastAmapRequestStartedAt;
      const delayMs = Math.max(0, AMAP_MIN_REQUEST_INTERVAL_MS - elapsed);
      if (delayMs > 0) await wait(delayMs);
      lastAmapRequestStartedAt = Date.now();
      return api(path);
    });
    // 即使某次请求失败，队列也要继续工作，后续请求仍按相同间隔执行。
    amapRequestQueue = request.catch(() => undefined);
    return request;
  }

  async function requestAmapCandidates(text) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await pacedAmapRequest(`/api/geocode/search?q=${encodeURIComponent(text)}`);
      } catch (error) {
        if (!isAmapRateLimitError(error) || attempt >= AMAP_RATE_LIMIT_RETRY_DELAYS_MS.length) throw error;
        const delayMs = AMAP_RATE_LIMIT_RETRY_DELAYS_MS[attempt];
        setGeocodeCallStatus(
          `[高德触发限流] 查询“${text}”将在 ${(delayMs / 1000).toFixed(1)} 秒后自动重试（${attempt + 1}/${AMAP_RATE_LIMIT_RETRY_DELAYS_MS.length}）。`,
          "status"
        );
        await wait(delayMs);
      }
    }
  }

  function parseCoordinateText(text) {
    const m = text.trim().match(/^\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*$/);
    if (!m) return null;
    const lon = Number(m[1]), lat = Number(m[2]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90) return null;
    return { name: `${lon.toFixed(6)},${lat.toFixed(6)}`, lon, lat };
  }

  function isInShenzhen(point) {
    return point.lon >= SHENZHEN_BOUNDS.minLon && point.lon <= SHENZHEN_BOUNDS.maxLon
      && point.lat >= SHENZHEN_BOUNDS.minLat && point.lat <= SHENZHEN_BOUNDS.maxLat;
  }

  function cesiumResultToCandidate(result, index) {
    const destination = result.destination;
    let carto;
    try {
      if (destination && Number.isFinite(destination.west) && Number.isFinite(destination.east)) {
        carto = Cesium.Rectangle.center(destination);
      } else {
        carto = Cesium.Cartographic.fromCartesian(destination);
      }
    } catch {
      return null;
    }
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
    return {
      id: `cesium-${index}`,
      name: result.displayName || "未命名地点",
      address: "Cesium ion 地理编码结果",
      lon,
      lat,
      provider: "Cesium ion",
      coordinate_system: "WGS84",
    };
  }

  async function searchCesiumCandidates(text) {
    const token = $("ionToken").value.trim();
    if (!token) return [];
    Cesium.Ion.defaultAccessToken = token;
    const service = new Cesium.IonGeocoderService({ scene: viewer.scene, accessToken: token });
    const biasedQuery = /深圳/.test(text) ? text : `深圳市 ${text}`;
    let results = await service.geocode(biasedQuery, Cesium.GeocodeType.SEARCH);
    if ((!results || !results.length) && biasedQuery !== text) {
      results = await service.geocode(text, Cesium.GeocodeType.SEARCH);
    }
    const candidates = (results || []).map(cesiumResultToCandidate).filter(Boolean);
    const shenzhenCandidates = candidates.filter(isInShenzhen);
    return (shenzhenCandidates.length ? shenzhenCandidates : []).slice(0, 8);
  }

  async function searchPlaceCandidates(text) {
    let amapError = null;
    let amapDiagnostic = null;
    try {
      const data = await requestAmapCandidates(text);
      amapDiagnostic = data.diagnostic || null;
      if (amapDiagnostic?.success) {
        setGeocodeCallStatus(
          `[高德实际调用成功] 查询“${text}”，返回 ${Number(amapDiagnostic.candidate_count || 0)} 个深圳候选。`,
          "status ok"
        );
      } else if (!amapDiagnostic?.configured) {
        setGeocodeCallStatus("[高德未调用] 未配置高德 Web 服务 Key，正在使用 Cesium ion。", "status");
      }
      if (Array.isArray(data.candidates) && data.candidates.length) return data.candidates;
    } catch (error) {
      amapError = error;
      setGeocodeCallStatus(`[高德实际调用失败] ${error.message}；正在回退 Cesium ion。`, "status bad");
      console.warn("Amap geocoder unavailable; falling back to Cesium ion.", error);
    }

    const cesiumCandidates = await searchCesiumCandidates(text);
    if (cesiumCandidates.length) {
      if (amapError) {
        setGeocodeCallStatus(
          `[高德实际调用失败] 已回退 Cesium ion，并返回 ${cesiumCandidates.length} 个候选。`,
          "status bad"
        );
      } else if (amapDiagnostic?.success) {
        setGeocodeCallStatus("[高德调用成功但无匹配] 已回退 Cesium ion 返回候选。", "status");
      }
      return cesiumCandidates;
    }
    if (amapError) {
      throw new Error(`${amapError.message}；Cesium ion 也没有返回深圳范围内的候选地点。`);
    }
    if (!$("ionToken").value.trim()) {
      throw new Error("未配置高德 Web 服务 Key，也没有 Cesium ion Token；请配置其中一个，或使用地图选点。 ");
    }
    throw new Error(`没有搜索到深圳范围内的地点：${text}；可补充区名、街道，或使用地图选点。`);
  }

  let pendingLocationPicker = null;

  function settleLocationPicker(candidate, error = null) {
    if (!pendingLocationPicker) return;
    const pending = pendingLocationPicker;
    pendingLocationPicker = null;
    const dialog = $("locationPickerDialog");
    if (dialog.open) dialog.close();
    if (error) pending.reject(error);
    else pending.resolve(candidate);
  }

  function chooseLocationCandidate(candidates, role, query) {
    if (!candidates.length) throw new Error(`没有可供确认的${role}候选地点。`);
    const dialog = $("locationPickerDialog");
    $("locationPickerTitle").textContent = `确认${role}`;
    $("locationPickerQuery").textContent = `输入：${query} · 找到 ${candidates.length} 个深圳候选结果`;
    const list = $("locationCandidateList");
    list.textContent = "";
    for (const candidate of candidates) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "location-candidate";
      const name = document.createElement("strong");
      name.textContent = candidate.name;
      const address = document.createElement("span");
      address.textContent = candidate.address || "地址信息暂缺";
      const coordinates = document.createElement("span");
      coordinates.textContent = `${candidate.provider} · ${Number(candidate.lon).toFixed(6)}, ${Number(candidate.lat).toFixed(6)}`;
      button.append(name, address, coordinates);
      button.addEventListener("click", () => settleLocationPicker(candidate));
      list.appendChild(button);
    }
    return new Promise((resolve, reject) => {
      if (pendingLocationPicker) settleLocationPicker(null, new Error("地点选择已被新的操作替代。"));
      pendingLocationPicker = { resolve, reject };
      if (typeof dialog.showModal === "function") dialog.showModal();
      else {
        pendingLocationPicker = null;
        resolve(candidates[0]);
      }
    });
  }

  $("locationPickerCancel").addEventListener("click", () => {
    settleLocationPicker(null, new Error("已取消地点选择。"));
  });
  $("locationPickerDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    settleLocationPicker(null, new Error("已取消地点选择。"));
  });

  async function geocodePlace(text, role) {
    const query = String(text || "").trim();
    if (!query) throw new Error(`请填写${role}。`);
    const direct = parseCoordinateText(query);
    if (direct) {
      setGeocodeCallStatus(`[无需调用高德] ${role}使用了直接输入或地图选取的 WGS84 坐标。`, "status ok");
      return direct;
    }
    const candidates = await searchPlaceCandidates(query);
    const selected = await chooseLocationCandidate(candidates, role, query);
    return {
      name: selected.name,
      lon: Number(selected.lon),
      lat: Number(selected.lat),
    };
  }

  let mapPickTarget = null;
  const mapPickMarkers = { origin: null, destination: null };
  const mapPickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

  function showMapPickMarker(kind, point) {
    if (mapPickMarkers[kind]) viewer.entities.remove(mapPickMarkers[kind]);
    const isOrigin = kind === "origin";
    mapPickMarkers[kind] = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(point.lon, point.lat, 8),
      point: {
        pixelSize: 12,
        color: isOrigin ? Cesium.Color.LIME : Cesium.Color.ORANGE,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: isOrigin ? "地图选取起点" : "地图选取终点",
        font: "13px Microsoft YaHei",
        fillColor: Cesium.Color.WHITE,
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString("#0c1726").withAlpha(0.82),
        pixelOffset: new Cesium.Cartesian2(0, -24),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
  }

  function beginMapPick(kind) {
    const isOrigin = kind === "origin";
    stopFollowing();
    mapPickTarget = {
      kind,
      inputId: isOrigin ? "originInput" : "destInput",
      label: isOrigin ? "起点" : "终点",
    };
    viewer.scene.canvas.style.cursor = "crosshair";
    const status = $("taskFormStatus");
    status.className = "status ok";
    status.textContent = `地图选取${mapPickTarget.label}：请在地图目标位置单击。`;
  }

  mapPickHandler.setInputAction((movement) => {
    if (!mapPickTarget) return;
    const ray = viewer.camera.getPickRay(movement.position);
    const cartesian = ray && viewer.scene.globe.pick(ray, viewer.scene);
    if (!cartesian) {
      const status = $("taskFormStatus");
      status.className = "status bad";
      status.textContent = "没有取得地图坐标，请点击地球表面后重试。";
      return;
    }
    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const point = {
      lon: Cesium.Math.toDegrees(carto.longitude),
      lat: Cesium.Math.toDegrees(carto.latitude),
    };
    const target = mapPickTarget;
    mapPickTarget = null;
    viewer.scene.canvas.style.cursor = "";
    $(target.inputId).value = `${point.lon.toFixed(7)},${point.lat.toFixed(7)}`;
    showMapPickMarker(target.kind, point);
    const status = $("taskFormStatus");
    status.className = "status ok";
    status.textContent = `${target.label}已通过地图选取：${point.lon.toFixed(6)}, ${point.lat.toFixed(6)}`;
    setGeocodeCallStatus("[无需调用高德] 已通过 Cesium 地图直接选取 WGS84 坐标。", "status ok");
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  function setAiStatus(text, cls = "status") {
    const el = $("aiServiceStatus");
    el.textContent = text;
    el.className = cls;
  }

  async function refreshAiStatus() {
    try {
      const status = await api("/api/ai/status");
      const ready = Boolean(status.configured && status.sdk_available);
      $("aiParseTaskBtn").disabled = !ready;
      $("batchAiParseBtn").disabled = !ready;
      if (ready) {
        setAiStatus(`AI 已就绪 · ${status.model} · 地点仍由地图服务确认`, "status ok");
      } else if (!status.sdk_available) {
        setAiStatus("AI SDK 未安装；请重新安装 requirements.txt。手动任务功能不受影响。", "status bad");
      } else {
        setAiStatus("未配置 OPENAI_API_KEY；手动任务功能仍可正常使用。", "status bad");
      }
    } catch (e) {
      $("aiParseTaskBtn").disabled = true;
      $("batchAiParseBtn").disabled = true;
      setAiStatus(`AI 服务状态读取失败：${e.message}`, "status bad");
    }
  }

  async function refreshGeocoderStatus() {
    const status = $("taskFormStatus");
    try {
      const service = await api("/api/geocode/status");
      if (service.amap_configured) {
        status.className = "status ok";
        status.textContent = "地点服务：高德深圳 POI 优先，Cesium ion 自动备用；创建任务时需确认候选地点。";
      } else if (configuredIonToken || $("ionToken").value.trim()) {
        status.className = "status";
        status.textContent = "地点服务：未配置高德 Key，当前使用 Cesium ion 深圳候选搜索；也可地图选点。";
      } else {
        status.className = "status bad";
        status.textContent = "地点服务尚未配置；请填写高德 Key 或 Cesium ion Token，也可直接地图选点。";
      }
    } catch (error) {
      status.className = "status bad";
      status.textContent = `地点服务状态读取失败：${error.message}`;
    }
  }

  function setGeocodeCallStatus(text, cls = "status") {
    const output = $("geocodeCallStatus");
    output.textContent = text;
    output.className = cls;
  }

  function diagnosticMessage(diagnostic) {
    if (!diagnostic) return "没有取得诊断信息。";
    const time = diagnostic.checked_at ? new Date(diagnostic.checked_at).toLocaleString() : "尚未检测";
    if (diagnostic.success) {
      return `[高德实际调用成功] 查询“${diagnostic.query}”，候选 ${diagnostic.candidate_count} 个，时间 ${time}。`;
    }
    if (!diagnostic.configured) return "[高德未调用] app_config.py 中尚未配置 Web 服务 Key。";
    if (!diagnostic.attempted) return "[高德尚未调用] Key 已读取，但还没有发起实际网络请求。";
    return `[高德实际调用失败] ${diagnostic.info}${diagnostic.error ? `：${diagnostic.error}` : ""}，时间 ${time}。`;
  }

  async function testGeocoderCall() {
    const button = $("testGeocoderBtn");
    button.disabled = true;
    setGeocodeCallStatus("正在实际调用高德搜索“深圳北站”…");
    try {
      const result = await api("/api/geocode/search?q=%E6%B7%B1%E5%9C%B3%E5%8C%97%E7%AB%99");
      setGeocodeCallStatus(diagnosticMessage(result.diagnostic), result.diagnostic?.success ? "status ok" : "status bad");
    } catch (error) {
      try {
        const diagnostic = await api("/api/geocode/diagnostics");
        setGeocodeCallStatus(diagnosticMessage(diagnostic), "status bad");
      } catch {
        setGeocodeCallStatus(`[高德检测失败] ${error.message}`, "status bad");
      }
    } finally {
      button.disabled = false;
    }
  }

  function applyAiDraft(draft) {
    $("originInput").value = draft.origin_text;
    $("destInput").value = draft.destination_text;
    $("deliveryType").value = draft.delivery_type;
    $("priority").value = draft.priority;
    $("payloadKg").value = Number(draft.payload_kg);
    $("deadline").value = Number(draft.deadline_minutes);
    $("cruiseAlt").value = String(draft.cruise_alt);
  }

  function renderAiDraft(draft, model) {
    const assumptions = (draft.assumptions || []).length
      ? `<br><b>默认/假设：</b>${draft.assumptions.map(escapeHtml).join("；")}`
      : "";
    const preview = $("aiDraftPreview");
    preview.innerHTML = `
      <b>${escapeHtml(draft.origin_text)}</b> → <b>${escapeHtml(draft.destination_text)}</b><br>
      类型 ${escapeHtml(draft.delivery_type)} · 优先级 ${escapeHtml(draft.priority)} · ${Number(draft.payload_kg).toFixed(1)}kg<br>
      时限 ${Number(draft.deadline_minutes)}min · 首选航高 ${Number(draft.cruise_alt)}m · 置信度 ${escapeHtml(draft.confidence)}
      <div class="ai-note">${escapeHtml(draft.task_summary)}</div>${assumptions}
      <div class="muted">模型：${escapeHtml(model)}；草案已填入下方手动表单，可以继续修改。</div>`;
    preview.hidden = false;
  }

  async function parseAiTask() {
    const text = $("aiTaskInput").value.trim();
    if (text.length < 4) throw new Error("请先输入完整的配送任务描述。");
    $("aiParseTaskBtn").disabled = true;
    $("aiCreateTaskBtn").disabled = true;
    setAiStatus("大模型正在理解任务…");
    try {
      const result = await api("/api/ai/parse-task", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ text }),
      });
      aiTaskDraft = result.draft;
      applyAiDraft(aiTaskDraft);
      renderAiDraft(aiTaskDraft, result.model);
      $("aiCreateTaskBtn").disabled = false;
      setAiStatus("AI 草案已生成；请检查地点和参数后确认创建。", "status ok");
    } finally {
      $("aiParseTaskBtn").disabled = false;
    }
  }

  function setBatchStatus(text, cls = "status") {
    const el = $("batchTaskStatus");
    el.textContent = text;
    el.className = cls;
  }

  function validResolvedPoint(point) {
    return point && Number.isFinite(Number(point.lon)) && Number.isFinite(Number(point.lat));
  }

  function batchDraftToPayload(draft) {
    return {
      origin: draft.origin,
      destination: draft.destination,
      delivery_type: draft.delivery_type,
      priority: draft.priority,
      payload_kg: draft.payload_kg,
      deadline_minutes: draft.deadline_minutes,
      cruise_alt: draft.cruise_alt,
      data_mode: draft.data_mode || "shenzhen",
    };
  }

  async function mapWithConcurrency(items, limit, worker) {
    const results = new Array(items.length);
    let cursor = 0;
    async function runWorker() {
      while (cursor < items.length) {
        const index = cursor++;
        results[index] = await worker(items[index], index);
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, runWorker));
    return results;
  }

  async function prepareBatchDrafts(rawDrafts, sourceLabel) {
    batchTaskDrafts = [];
    $("batchCreateBtn").disabled = true;
    const placeCache = new Map();
    let completed = 0;

    async function resolvePlace(text, role) {
      const query = String(text || "").trim();
      const direct = parseCoordinateText(query);
      if (direct) return { point: direct, warning: "使用直接输入的 WGS84 坐标" };
      const key = query.toLowerCase();
      if (!placeCache.has(key)) placeCache.set(key, searchPlaceCandidates(query));
      const candidates = await placeCache.get(key);
      if (!candidates.length) throw new Error(`${role}“${query}”没有深圳候选结果`);
      const selected = candidates[0];
      return {
        point: { name: selected.name, lon: Number(selected.lon), lat: Number(selected.lat) },
        warning: `${role}“${query}”自动采用首个候选：${selected.name}`,
      };
    }

    setBatchStatus(`${sourceLabel}已读取，正在解析地点 0/${rawDrafts.length}…`);
    batchTaskDrafts = await mapWithConcurrency(rawDrafts, 1, async (source, index) => {
      const draft = { ...source };
      draft.source_row = source.source_row ?? index + 1;
      draft.errors = [...(source.errors || [])];
      draft.warnings = [...(source.warnings || source.assumptions || [])];
      draft.data_mode = source.data_mode || "shenzhen";
      draft.payload_kg = Number(source.payload_kg ?? 1);
      draft.deadline_minutes = Number(source.deadline_minutes ?? 45);
      draft.cruise_alt = Number(source.cruise_alt ?? 80);
      if (!(draft.payload_kg > 0 && draft.payload_kg <= 5)) draft.errors.push("重量必须大于0且不超过5kg");
      if (!(draft.deadline_minutes >= 10 && draft.deadline_minutes <= 240)) draft.errors.push("时限必须在10～240分钟之间");
      if (!(draft.cruise_alt >= 50 && draft.cruise_alt <= 120)) draft.errors.push("巡航高度必须在50～120m之间");

      if (!draft.errors.length) {
        try {
          if (!validResolvedPoint(draft.origin)) {
            if (!String(draft.origin_text || "").trim()) throw new Error("缺少起点");
            const resolved = await resolvePlace(draft.origin_text, "起点");
            draft.origin = resolved.point;
            draft.warnings.push(resolved.warning);
          } else {
            draft.origin = { ...draft.origin, lon: Number(draft.origin.lon), lat: Number(draft.origin.lat) };
          }
          if (!validResolvedPoint(draft.destination)) {
            if (!String(draft.destination_text || "").trim()) throw new Error("缺少终点");
            const resolved = await resolvePlace(draft.destination_text, "终点");
            draft.destination = resolved.point;
            draft.warnings.push(resolved.warning);
          } else {
            draft.destination = { ...draft.destination, lon: Number(draft.destination.lon), lat: Number(draft.destination.lat) };
          }
        } catch (error) {
          draft.errors.push(error.message);
        }
      }
      completed += 1;
      setBatchStatus(`${sourceLabel}地点解析中 ${completed}/${rawDrafts.length}…`);
      return draft;
    });
    const candidates = batchTaskDrafts.filter(
      draft => !draft.errors.length && validResolvedPoint(draft.origin) && validResolvedPoint(draft.destination),
    );
    if (candidates.length) {
      setBatchStatus(`${sourceLabel}地点解析完成，正在执行建筑和任务规则安全校验…`);
      const validation = await api("/api/tasks/batch/validate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ tasks: candidates.map(batchDraftToPayload) }),
      });
      for (const result of validation.results || []) {
        if (!result.valid && candidates[result.index]) {
          candidates[result.index].errors.push(...(result.errors || ["后端安全校验失败"]));
        }
      }
    }
    renderBatchTaskPreview();
  }

  function renderBatchTaskPreview() {
    const preview = $("batchTaskPreview");
    const validCount = batchTaskDrafts.filter(draft => !draft.errors.length && validResolvedPoint(draft.origin) && validResolvedPoint(draft.destination)).length;
    const invalidCount = batchTaskDrafts.length - validCount;
    preview.innerHTML = `<table>
      <thead><tr><th>序号</th><th>路线</th><th>任务参数</th><th>校验结果</th></tr></thead>
      <tbody>${batchTaskDrafts.map((draft, index) => {
        const origin = validResolvedPoint(draft.origin)
          ? `${escapeHtml(draft.origin.name || draft.origin_text || "起点")}<br><span class="muted">${Number(draft.origin.lon).toFixed(6)}, ${Number(draft.origin.lat).toFixed(6)}</span>`
          : escapeHtml(draft.origin_text || "--");
        const destination = validResolvedPoint(draft.destination)
          ? `${escapeHtml(draft.destination.name || draft.destination_text || "终点")}<br><span class="muted">${Number(draft.destination.lon).toFixed(6)}, ${Number(draft.destination.lat).toFixed(6)}</span>`
          : escapeHtml(draft.destination_text || "--");
        const messages = draft.errors.length
          ? `<span class="batch-error">${draft.errors.map(escapeHtml).join("；")}</span>`
          : `<span class="batch-ok">有效</span>${draft.warnings.length ? `<br><span class="batch-warning">${draft.warnings.map(escapeHtml).join("；")}</span>` : ""}`;
        return `<tr><td>${escapeHtml(draft.source_row ?? index + 1)}</td><td>${origin}<br>↓<br>${destination}</td><td>${escapeHtml(draft.delivery_type)} · ${escapeHtml(draft.priority)}<br>${Number(draft.payload_kg).toFixed(1)}kg · ${Number(draft.deadline_minutes)}min · ${Number(draft.cruise_alt)}m</td><td>${messages}</td></tr>`;
      }).join("")}</tbody></table>`;
    preview.hidden = false;
    $("batchCreateBtn").disabled = validCount === 0;
    setBatchStatus(
      `预览完成：共 ${batchTaskDrafts.length} 条，有效 ${validCount} 条，异常 ${invalidCount} 条。确认地点和参数后可批量初始化。`,
      invalidCount ? "status" : "status ok",
    );
  }

  async function parseBatchTaskFile() {
    const file = $("batchTaskFile").files[0];
    if (!file) throw new Error("请先选择 XLSX、CSV 或 JSON 任务文件。");
    $("batchParseFileBtn").disabled = true;
    try {
      setBatchStatus(`正在读取 ${file.name}…`);
      const formData = new FormData();
      formData.append("file", file);
      const result = await api("/api/tasks/batch/parse-file", { method: "POST", body: formData });
      await prepareBatchDrafts(result.drafts || [], `${file.name}（${result.file_type.toUpperCase()}）`);
    } finally {
      $("batchParseFileBtn").disabled = false;
    }
  }

  async function parseBatchAiTasks() {
    const text = $("batchAiTaskInput").value.trim();
    if (text.length < 8) throw new Error("请至少输入两条完整的飞行任务描述。");
    $("batchAiParseBtn").disabled = true;
    try {
      setBatchStatus("大模型正在拆分并解析多条任务…");
      const result = await api("/api/ai/parse-tasks", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ text }),
      });
      await prepareBatchDrafts(result.drafts || [], `AI ${result.model}`);
    } finally {
      $("batchAiParseBtn").disabled = false;
    }
  }

  async function createBatchTasks() {
    const validDrafts = batchTaskDrafts.filter(draft => !draft.errors.length && validResolvedPoint(draft.origin) && validResolvedPoint(draft.destination));
    if (!validDrafts.length) throw new Error("没有可初始化的有效任务。");
    $("batchCreateBtn").disabled = true;
    setBatchStatus(`正在批量安全校验并初始化 ${validDrafts.length} 条任务…`);
    try {
      const result = await api("/api/tasks/batch", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          tasks: validDrafts.map(batchDraftToPayload),
        }),
      });
      await pollState();
      setBatchStatus(`成功批量初始化 ${result.created_count} 条任务，已加入调度队列。`, "status ok");
      batchTaskDrafts = [];
      $("batchTaskPreview").hidden = true;
      viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(SHENZHEN.lon, SHENZHEN.lat, 52000), duration: 1.1 });
    } catch (error) {
      $("batchCreateBtn").disabled = false;
      throw error;
    }
  }

  function downloadBatchTemplate() {
    const csv = [
      "起点,起点经度,起点纬度,终点,终点经度,终点纬度,配送类型,优先级,重量kg,时限分钟,巡航高度,数据模式",
      "深圳北站,114.024436,22.612652,人才公园,113.938252,22.515095,医疗物资,紧急,2,30,80,深圳",
      "宝安中心,,,深圳市民中心,,,文件,高,1,45,80,深圳",
    ].join("\r\n");
    const url = URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "深圳无人机批量任务模板.csv";
    link.click();
    URL.revokeObjectURL(url);
  }

  async function loadOsmBuildings() {
    const token = $("ionToken").value.trim();
    if (!token) throw new Error("请先输入 Cesium ion Token。 ");
    Cesium.Ion.defaultAccessToken = token;
    if (!osmBuildings) {
      osmBuildings = await Cesium.createOsmBuildingsAsync();
      viewer.scene.primitives.add(osmBuildings);
    }
    osmBuildings.show = $("dataMode").value === "cesium";
  }

  function clearCustomVisuals() {
    for (const e of customEntities) viewer.entities.remove(e);
    customEntities = [];
  }

  function clearLocalBuildingVisuals() {
    for (const entities of localBuildingEntities.values()) {
      for (const e of entities) viewer.entities.remove(e);
    }
    localBuildingEntities.clear();
    localBuildingVolumes.clear();
    lastBuildingViewKey = "";
  }

  function heightColor(height) {
    if (height >= 120) return Cesium.Color.fromCssColorString("#8b5cf6").withAlpha(0.82);
    if (height >= 80) return Cesium.Color.fromCssColorString("#5676a6").withAlpha(0.80);
    if (height >= 40) return Cesium.Color.fromCssColorString("#678ca5").withAlpha(0.76);
    return Cesium.Color.fromCssColorString("#7895a6").withAlpha(0.68);
  }

  function addLocalBuildingPolygon(coords, props) {
    if (!coords || !coords[0] || coords[0].length < 3) return null;
    const flat = [];
    for (const p of coords[0]) flat.push(Number(p[0]), Number(p[1]));
    const height = Math.max(1, Number(props.height || 0));
    return viewer.entities.add({
      name: `${props.name || "深圳建筑"} · ${height.toFixed(1)}m`,
      polygon: {
        hierarchy: Cesium.Cartesian3.fromDegreesArray(flat),
        height: 0,
        extrudedHeight: height,
        material: heightColor(height),
        // 大量建筑时逐栋轮廓线非常耗性能，只给较高建筑画轮廓。
        outline: height >= 60,
        outlineColor: Cesium.Color.fromCssColorString("#b7d3e6").withAlpha(0.55),
      },
    });
  }

  function localBuildingVolume(coords, height) {
    const ring = coords && coords[0];
    if (!ring || !ring.length) return null;
    let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
    for (const point of ring) {
      const lon = Number(point[0]), lat = Number(point[1]);
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
      minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon);
      minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
    }
    if (![minLon, minLat, maxLon, maxLat].every(Number.isFinite)) return null;
    return { minLon, minLat, maxLon, maxLat, height: Math.max(1, Number(height || 0)) };
  }

  function renderVisibleBuildingGeojson(fc) {
    const features = (fc && fc.features) || [];
    const incoming = new Set();

    for (const f of features) {
      const props = f.properties || {};
      const rid = String(props.record_id || props.name || "");
      if (!rid) continue;
      incoming.add(rid);
      if (localBuildingEntities.has(rid)) continue;

      const entities = [];
      const volumes = [];
      const g = f.geometry || {};
      if (g.type === "Polygon") {
        const e = addLocalBuildingPolygon(g.coordinates, props);
        if (e) entities.push(e);
        const volume = localBuildingVolume(g.coordinates, props.height);
        if (volume) volumes.push(volume);
      } else if (g.type === "MultiPolygon") {
        for (const poly of g.coordinates || []) {
          const e = addLocalBuildingPolygon(poly, props);
          if (e) entities.push(e);
          const volume = localBuildingVolume(poly, props.height);
          if (volume) volumes.push(volume);
        }
      }
      if (entities.length) {
        localBuildingEntities.set(rid, entities);
        localBuildingVolumes.set(rid, volumes);
      }
    }

    // 当前模式采用“视野驻留”：离开视野的建筑立即释放，避免浏览器长期累计几十万 Entity。
    for (const [rid, entities] of [...localBuildingEntities.entries()]) {
      if (incoming.has(rid)) continue;
      for (const e of entities) viewer.entities.remove(e);
      localBuildingEntities.delete(rid);
      localBuildingVolumes.delete(rid);
    }
    setDataVisibility();
  }

  function cameraViewRectangle() {
    const rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
    if (!rect) return null;
    const west = Cesium.Math.toDegrees(rect.west);
    const south = Cesium.Math.toDegrees(rect.south);
    const east = Cesium.Math.toDegrees(rect.east);
    const north = Cesium.Math.toDegrees(rect.north);
    if (![west, south, east, north].every(Number.isFinite)) return null;
    // 深圳不存在跨日期变更线问题；异常超大视野直接忽略。
    if (east <= west || north <= south || east - west > 2 || north - south > 2) return null;
    return { west, south, east, north };
  }

  function buildingLodForCamera() {
    const carto = viewer.camera.positionCartographic;
    const h = Math.max(0, Number(carto && carto.height || 0));
    // 近距离时加载 Height>=1m 的全部建筑；城市总览时逐级过滤低矮建筑。
    // 这样 74.5 万栋都“可加载”，但不会一次性把全市建筑塞进浏览器。
    if (h <= 1800) return { minHeight: 1, maxFeatures: 6000 };
    if (h <= 3500) return { minHeight: 3, maxFeatures: 6000 };
    if (h <= 6500) return { minHeight: 8, maxFeatures: 5500 };
    return { minHeight: 20, maxFeatures: 5000 };
  }

  async function loadVisibleBuildings({ force = false } = {}) {
    if ($("dataMode").value === "cesium" || buildingViewBusy) return;
    const rect = cameraViewRectangle();
    if (!rect) return;
    const lod = buildingLodForCamera();
    const key = [rect.west, rect.south, rect.east, rect.north]
      .map(v => v.toFixed(3)).join(":") + `:${lod.minHeight}`;
    if (!force && key === lastBuildingViewKey) return;

    buildingViewBusy = true;
    try {
      const fc = await api("/api/buildings/in-bbox", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          min_lon: rect.west,
          min_lat: rect.south,
          max_lon: rect.east,
          max_lat: rect.north,
          min_height: lod.minHeight,
          max_features: lod.maxFeatures,
        }),
      });
      lastBuildingViewKey = key;
      renderVisibleBuildingGeojson(fc);
      const meta = fc.meta || {};
      const trunc = meta.truncated ? `；当前视野建筑较多，显示 ${Number(meta.returned || 0).toLocaleString()}/${Number(meta.matched || 0).toLocaleString()} 栋` : `；当前视野 ${Number(meta.returned || 0).toLocaleString()} 栋`;
      $("dataStatus").textContent = `全深圳建筑动态流式加载${trunc}；Height ≥ ${Number(meta.min_height || lod.minHeight).toFixed(0)}m。缩放靠近后会自动加载全部低矮建筑。`;
      $("dataStatus").className = "status ok";
    } catch (e) {
      $("dataStatus").textContent = `当前视野建筑加载失败：${e.message}`;
      $("dataStatus").className = "status bad";
    } finally {
      buildingViewBusy = false;
    }
  }

  function scheduleVisibleBuildings(delay = 180) {
    if (buildingReloadTimer) clearTimeout(buildingReloadTimer);
    buildingReloadTimer = setTimeout(() => loadVisibleBuildings().catch(() => {}), delay);
  }

  async function refreshBuildingStats() {
    try {
      const s = await api("/api/buildings/stats");
      const el = $("buildingDatasetStatus");
      if (!s.available) { el.textContent = "深圳建筑数据集未找到。"; el.className = "status bad"; return; }
      el.textContent = `深圳建筑库：${Number(s.count || 0).toLocaleString()} 栋 · Height ${Number(s.min_height).toFixed(1)}–${Number(s.max_height).toFixed(1)}m · ${s.crs || "EPSG:4326"}`;
      el.className = "status ok";
    } catch (e) {
      $("buildingDatasetStatus").textContent = `建筑数据集读取失败：${e.message}`;
      $("buildingDatasetStatus").className = "status bad";
    }
  }

  function addPolygonVisual(coords, props) {
    if (!coords || !coords[0] || coords[0].length < 3) return;
    const flat = [];
    for (const p of coords[0]) flat.push(Number(p[0]), Number(p[1]));
    const kind = String(props.kind || "other").toLowerCase();
    if (kind === "building") {
      const h = Math.max(3, Number(props.height || props.building_height || 35));
      const e = viewer.entities.add({
        name: props.name || "自定义建筑",
        polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(flat),
          height: 0,
          extrudedHeight: h,
          material: Cesium.Color.fromCssColorString("#6689a8").withAlpha(0.70),
          outline: true,
          outlineColor: Cesium.Color.fromCssColorString("#a8c6df"),
        },
      });
      customEntities.push(e);
    } else if (kind === "no_fly") {
      const e = viewer.entities.add({
        name: props.name || "禁飞区",
        polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(flat),
          height: 0,
          extrudedHeight: Number(props.max_altitude || 140),
          material: Cesium.Color.RED.withAlpha(0.26),
          outline: true,
          outlineColor: Cesium.Color.fromCssColorString("#ff5964"),
        },
      });
      customEntities.push(e);
    }
  }

  function renderCustomGeojson(fc) {
    clearCustomVisuals();
    const features = (fc && fc.features) || [];
    for (const f of features) {
      const props = f.properties || {};
      const g = f.geometry || {};
      if (g.type === "Polygon") addPolygonVisual(g.coordinates, props);
      else if (g.type === "MultiPolygon") {
        for (const poly of g.coordinates || []) addPolygonVisual(poly, props);
      } else if (g.type === "Point" && String(props.kind).toLowerCase() === "landing_site") {
        const [lon, lat] = g.coordinates || [];
        if (Number.isFinite(Number(lon)) && Number.isFinite(Number(lat))) {
          const e = viewer.entities.add({
            name: props.name || "起降点",
            position: Cesium.Cartesian3.fromDegrees(Number(lon), Number(lat), 3),
            point: { pixelSize: 13, color: Cesium.Color.LIME, outlineColor: Cesium.Color.WHITE, outlineWidth: 2 },
            label: { text: props.name || "起降点", pixelOffset: new Cesium.Cartesian2(0, -22), fillColor: Cesium.Color.WHITE, showBackground: true },
          });
          customEntities.push(e);
        }
      }
    }
    setDataVisibility();
  }

  function setDataVisibility() {
    const mode = $("dataMode").value;
    const local = mode !== "cesium";
    if (osmBuildings) osmBuildings.show = mode === "cesium";
    for (const entities of localBuildingEntities.values()) for (const e of entities) e.show = local;
    for (const e of customEntities) e.show = mode === "custom";
  }

  async function refreshCustomData() {
    const fc = await api("/api/data/custom");
    renderCustomGeojson(fc);
  }

  function clearAirspaceVisuals() {
    for (const entity of airspaceEntities) viewer.entities.remove(entity);
    airspaceEntities = [];
  }

  function airspacePolygonRings(zone) {
    const geometry = zone?.geometry || {};
    if (geometry.type === "Polygon") return geometry.coordinates || [];
    if (geometry.type === "MultiPolygon") return (geometry.coordinates || []).map(poly => poly?.[0]).filter(Boolean);
    return [];
  }

  function updateAirspaceLayer(airspace = {}) {
    const zones = (airspace.zones || []).filter(zone => zone.active);
    const signature = zones.map(zone => `${zone.id}:${zone.status}`).join("|");
    if (signature === airspaceRenderSignature) return;
    airspaceRenderSignature = signature;
    clearAirspaceVisuals();

    for (const zone of zones) {
      const hard = zone.category === "HARD";
      const approved = Boolean(zone.approved);
      const fill = hard
        ? Cesium.Color.fromCssColorString("#ff3344").withAlpha(0.30)
        : Cesium.Color.fromCssColorString(approved ? "#6fd08c" : "#ff9d3c").withAlpha(approved ? 0.09 : 0.22);
      const outline = Cesium.Color.fromCssColorString(hard ? "#ff3344" : (approved ? "#7ee2a0" : "#ffad4d"));
      const rings = airspacePolygonRings(zone);
      rings.forEach((ring, index) => {
        if (!ring || ring.length < 4) return;
        const flat = ring.flatMap(point => [Number(point[0]), Number(point[1])]);
        const description = `
          <div><b>${escapeHtml(zone.name)}</b></div>
          <div>${escapeHtml(zone.description || "")}</div>
          <div>状态：${hard ? "绝对禁飞" : (approved ? "已审批，可进入规划" : "未审批，禁止进入")}</div>
          <div>边界：${escapeHtml(zone.boundary_note || "")}</div>
          ${zone.source_url ? `<div><a href="${escapeHtml(zone.source_url)}" target="_blank" rel="noopener">查看官方依据</a></div>` : ""}`;
        const entity = viewer.entities.add({
          name: zone.name,
          description,
          polygon: {
            hierarchy: Cesium.Cartesian3.fromDegreesArray(flat),
            height: 0,
            extrudedHeight: hard ? 180 : 150,
            material: fill,
            outline: true,
            outlineColor: outline,
          },
        });
        airspaceEntities.push(entity);

        if (index === 0) {
          const unique = ring.slice(0, -1);
          const lon = unique.reduce((sum, point) => sum + Number(point[0]), 0) / unique.length;
          const lat = unique.reduce((sum, point) => sum + Number(point[1]), 0) / unique.length;
          airspaceEntities.push(viewer.entities.add({
            position: Cesium.Cartesian3.fromDegrees(lon, lat, hard ? 190 : 160),
            label: {
              text: `${hard ? "禁飞" : (approved ? "已审批" : "待审批")} · ${zone.name}`,
              font: "11px Microsoft YaHei",
              fillColor: Cesium.Color.WHITE,
              outlineColor: Cesium.Color.BLACK,
              outlineWidth: 3,
              style: Cesium.LabelStyle.FILL_AND_OUTLINE,
              showBackground: true,
              backgroundColor: outline.withAlpha(0.68),
              pixelOffset: new Cesium.Cartesian2(0, -8),
              distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 70000),
            },
          }));
        }
      });
    }

    const panel = $("airspaceApprovalPanel");
    const hardCount = zones.filter(zone => zone.category === "HARD").length;
    const controlled = zones.filter(zone => zone.category === "CONTROLLED");
    const unapprovedCount = controlled.filter(zone => !zone.approved).length;
    $("airspaceSummary").textContent = `禁飞 ${hardCount} · 待审批 ${unapprovedCount}`;
    panel.innerHTML = zones.map(zone => {
      const hard = zone.category === "HARD";
      const statusControl = hard
        ? `<span class="airspace-badge hard">绝对禁飞</span>`
        : `<select data-airspace-zone="${escapeHtml(zone.id)}" aria-label="${escapeHtml(zone.name)}审批状态">
            <option value="false" ${zone.approved ? "" : "selected"}>未审批</option>
            <option value="true" ${zone.approved ? "selected" : ""}>已审批</option>
          </select>`;
      return `<div class="airspace-row">
        <div class="airspace-row-head"><span class="airspace-row-name">${escapeHtml(zone.name)}</span>${statusControl}</div>
        <div class="airspace-row-meta">${escapeHtml(zone.boundary_note || zone.description || "")}${zone.source_url ? ` · <a class="airspace-source" href="${escapeHtml(zone.source_url)}" target="_blank" rel="noopener">官方依据</a>` : ""}</div>
      </div>`;
    }).join("") || `<div class="airspace-empty">当前没有生效的空域区域。</div>`;
  }

  function priorityClass(p) {
    return ({ EMERGENCY: "emergency", HIGH: "high", NORMAL: "normal", LOW: "low" })[p] || "normal";
  }
  function priorityText(p) {
    return ({ EMERGENCY: "紧急", HIGH: "高", NORMAL: "普通", LOW: "低" })[p] || p;
  }
  function statusText(s) {
    return ({ WAITING: "等待", WAITING_BLOCKED: "安全等待", WAITING_HANDOVER: "等待接驳", HANDOVER_ASSIGNED: "前往接驳", CARGO_RECOVERY: "携货恢复", CARGO_EMERGENCY: "携货应急", ASSIGNED: "前往取货", IN_PROGRESS: "配送中", COMPLETED: "完成", FAILED: "失败", PLANNING_FAILED: "规划失败" })[s] || s;
  }
  function cargoText(t) {
    return ({ WAITING_PICKUP: "待取货", IN_TRANSIT: "货物在途", IN_RECOVERY: "货物随原机恢复", WAITING_HANDOVER: "货物等待接驳", DELIVERED: "已送达" })[t.cargo_status] || "";
  }
  function taskPickupLabel(t) {
    if (t.cargo_status === "WAITING_HANDOVER" && t.cargo_location?.name) return t.cargo_location.name;
    if ((t.handover_history?.length || 0) > 0 && t.handover_station) return t.handover_station.label || t.handover_station.name || "货物交接点";
    return t.origin?.name || "起点";
  }
  function uavStateText(u) {
    if (u.phase === "TO_PICKUP") return "去取货";
    if (u.phase === "TO_HANDOVER") return "去货物交接点";
    if (u.phase === "DELIVERING") return "配送中";
    if (u.phase === "TO_CARGO_HANDOVER") return `携货去交接${u.handover_target?.name ? ` · ${u.handover_target.name}` : ""}`;
    if (u.phase === "TO_SWAP") return `去换电${u.swap_station?.name ? ` · ${u.swap_station.name}` : ""}`;
    if (u.phase === "SWAPPING") return `换电中 · ${Math.ceil(Number(u.swap_remaining_s || 0))}秒`;
    if (u.phase === "RETURNING") return `返回基地${u.recovery_target?.label ? ` · ${u.recovery_target.label}` : ""}`;
    if (u.phase === "TO_RECOVERY_SWAP") return `恢复换电${u.swap_station?.name ? ` · ${u.swap_station.name}` : ""}`;
    if (u.phase === "RECOVERY_SWAPPING") return `恢复换电中 · ${Math.ceil(Number(u.swap_remaining_s || 0))}秒`;
    if (u.phase === "BASE_CHARGING") return `基地充电 · ${Math.ceil(Number(u.charge_remaining_s || 0))}秒`;
    if (u.phase === "BASE_IDLE") return "基地满电待命";
    if (u.phase === "SAFE_HOLD") return "安全悬停 · 等待重规划";
    if (u.phase === "SAFE_GROUNDED") return "安全停放 · 等待重规划";
    if (u.phase === "SAFE_CARGO_HOLD") return "携货安全悬停 · 等待重规划";
    if (u.phase === "SAFE_CARGO_GROUNDED") return "携货安全停放 · 等待处理";
    if (u.phase === "HOLD") return "悬停";
    return u.state === "IDLE" ? "空闲" : u.state;
  }

  function weatherIcon(weather) {
    if (!weather?.available) return "⚠️";
    const text = String(weather.weather || "");
    if (/雷|冰雹|龙卷/.test(text)) return "⛈️";
    if (/雪/.test(text)) return "🌨️";
    if (/雨/.test(text)) return "🌧️";
    if (/雾|霾|沙尘/.test(text)) return "🌫️";
    if (/风|台风/.test(text)) return "💨";
    if (/阴/.test(text)) return "☁️";
    if (/云/.test(text)) return "⛅";
    if (/晴/.test(text)) return "☀️";
    return "🌤️";
  }

  function displayWeatherNumber(value, suffix) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(Number.isInteger(number) ? 0 : 1)}${suffix}` : "--";
  }

  function updateWeatherCard(weather = {}) {
    const card = $("weatherCard");
    if (!card) return;
    const available = Boolean(weather.available);
    const allowed = weather.dispatch_allowed !== false;
    const flightAction = String(weather.flight_action || "MONITOR_ONLY");
    const flightActionLabel = {
      NORMAL: "在途正常",
      CONTINUE_CAUTION: "在途减速复核",
      RECOVER: "在途应急撤离",
      MONITOR_ONLY: "在途仅监控",
    }[flightAction] || "在途状态未知";
    card.classList.toggle("weather-loading", !weather.fetched_at);
    card.classList.toggle("weather-paused", !allowed);
    card.classList.toggle("weather-unavailable", !available);
    card.classList.toggle("weather-simulated", weather.source_mode === "simulated");
    const simulationControls = $("weatherSimulationControls");
    if (simulationControls) simulationControls.hidden = weather.source_mode !== "simulated";
    $("weatherIcon").textContent = weatherIcon(weather);
    $("weatherCity").textContent = `${weather.city || "深圳市"}${weather.source_mode === "simulated" ? "模拟天气" : "实时天气"}`;

    const badge = $("weatherDispatchBadge");
    badge.textContent = flightAction === "RECOVER" ? "应急撤离" : (allowed ? "允许派遣" : "暂停新派遣");
    badge.className = `weather-badge ${allowed ? "safe" : "paused"}`;

    $("weatherSummary").textContent = available
      ? `${weather.weather || "天气未知"} · ${displayWeatherNumber(weather.temperature_c, "℃")}`
      : (weather.enabled === false ? "天气联动已关闭" : "天气数据不可用");
    const wind = `${weather.wind_direction ? `${weather.wind_direction}风` : ""}${weather.wind_power ? ` ${weather.wind_power}级` : ""}`.trim();
    $("weatherDetails").textContent = available
      ? `湿度 ${displayWeatherNumber(weather.humidity_percent, "%")} · ${wind || "风力 --"} · ${flightActionLabel}`
      : (weather.error || "等待后端天气模块返回数据");

    const statusParts = [weather.pause_reason || weather.dispatch_status, weather.flight_action_reason]
      .filter((value, index, values) => value && values.indexOf(value) === index);
    const statusText = statusParts.join(" · ");
    const sourceTime = weather.report_time
      ? `${weather.source_mode === "simulated" ? "模拟设置" : "发布"} ${weather.report_time}`
      : "发布时间未知";
    $("weatherUpdatedAt").textContent = `${sourceTime}${statusText ? ` · ${statusText}` : ""}`;
  }

  function updateFollowSelector(state) {
    const select = $("followUav");
    if (!select) return;
    const desired = followedUavId || select.value || "";
    select.innerHTML = `<option value="">不跟随 / 自由视角</option>` + state.uavs
      .map(u => `<option value="${u.id}">${u.id} · ${uavStateText(u)} · ${Number(u.alt).toFixed(0)}m</option>`).join("");
    if ([...select.options].some(o => o.value === desired)) select.value = desired;
    else select.value = "";
  }

  function setFollowStatus(text, cls = "status") {
    const el = $("followStatus");
    if (!el) return;
    el.textContent = text;
    el.className = cls;
  }

  function normalizeAngle(rad) {
    let x = rad % (Math.PI * 2);
    if (x < 0) x += Math.PI * 2;
    return x;
  }

  function shortestAngleDelta(from, to) {
    let d = normalizeAngle(to) - normalizeAngle(from);
    if (d > Math.PI) d -= Math.PI * 2;
    if (d < -Math.PI) d += Math.PI * 2;
    return d;
  }

  function bearingRadians(a, b) {
    const lat1 = Cesium.Math.toRadians(a.lat), lat2 = Cesium.Math.toRadians(b.lat);
    const dLon = Cesium.Math.toRadians(b.lon - a.lon);
    const y = Math.sin(dLon) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
    if (Math.abs(x) + Math.abs(y) < 1e-10) return null;
    return normalizeAngle(Math.atan2(y, x));
  }

  const followCamera = {
    azimuth: Math.PI,
    elevation: Math.atan2(90, 185),
    distance: Math.hypot(185, 90),
  };
  let followDrag = null;

  function resetFollowCamera() {
    followCamera.azimuth = Math.PI;
    followCamera.elevation = Math.atan2(90, 185);
    followCamera.distance = Math.hypot(185, 90);
  }

  function setFollowCameraControls(active) {
    const controller = viewer.scene.screenSpaceCameraController;
    controller.enableRotate = !active;
    controller.enableTranslate = !active;
    controller.enableZoom = !active;
    controller.enableTilt = !active;
    controller.enableLook = !active;
    viewer.scene.canvas.style.cursor = active ? "grab" : "";
  }

  function stopFollowing() {
    followedUavId = "";
    followDrag = null;
    setFollowCameraControls(false);
    // 自定义追尾相机使用 camera.lookAt；退出跟随后恢复世界坐标系。
    try { viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY); } catch {}
    const select = $("followUav");
    if (select) select.value = "";
    setFollowStatus("自由视角；也可以点击右侧无人机列表快速跟随。");
    scheduleVisibleBuildings(50);
  }

  function followUav(uavId) {
    if (!uavId) { stopFollowing(); return; }
    const idx = currentState.uavs.findIndex(u => u.id === uavId);
    if (idx < 0) {
      stopFollowing();
      setFollowStatus(`未找到 ${uavId}。`, "status bad");
      return;
    }
    const u = currentState.uavs[idx];
    ensureUavGraphic(u, idx);
    followedUavId = uavId;
    resetFollowCamera();
    setFollowCameraControls(true);
    // 不再使用 viewer.trackedEntity。摄像机位置与航向由 preRender 每帧更新，
    // 因此 UAV 转弯时镜头也会真正跟着转。
    const select = $("followUav");
    if (select && [...select.options].some(o => o.value === uavId)) select.value = uavId;
    setFollowStatus(`跟随 ${uavId} · 左键拖动可 360° 环绕 · 滚轮缩放`, "status ok");
    scheduleVisibleBuildings(80);
  }

  function dispatchLegSummary(plan) {
    const definitions = [
      ["takeoff_to_pickup", "当前位置→取货"],
      ["pickup_to_destination", "取货→终点"],
      ["cargo_recovery", "携货恢复→交接点"],
    ];
    const summaries = [];
    for (const [key, label] of definitions) {
      const leg = plan?.[key];
      if (!leg?.route?.length) continue;
      const distance = Number(leg.route_length_m);
      const distanceText = leg.route_length_m != null && Number.isFinite(distance) ? ` ${(distance / 1000).toFixed(2)}km` : "";
      const swaps = (leg.swap_stops || []).map(stop => stop.name).filter(Boolean);
      summaries.push(`${label}${distanceText}${swaps.length ? ` · 经${swaps.join("、")}换电` : ""}`);
    }
    return summaries;
  }

  function uavRouteProgress(uav) {
    const route = uav?.route || [];
    if (route.length < 2) return 0;
    const progress = (Number(uav.route_index || 0) + Number(uav.segment_t || 0)) / (route.length - 1);
    return Math.max(0, Math.min(100, progress * 100));
  }

  function updateDispatchRoutePanel(state) {
    const panel = $("dispatchRoutePanel");
    const summary = $("dispatchRouteSummary");
    if (!panel || !summary) return;
    const terminal = new Set(["COMPLETED", "FAILED", "PLANNING_FAILED"]);
    const tasks = state.tasks
      .filter(task => !terminal.has(task.status))
      .sort((a, b) => Number(Boolean(b.route_plan)) - Number(Boolean(a.route_plan)) || Number(a.created_at || 0) - Number(b.created_at || 0));
    const plannedCount = tasks.filter(task => task.route_plan).length;
    const waitingCount = tasks.length - plannedCount;
    const weatherPaused = state.weather?.dispatch_allowed === false;
    panel.classList.toggle("paused", weatherPaused || waitingCount > 0);
    summary.classList.toggle("paused", weatherPaused || waitingCount > 0);
    summary.textContent = weatherPaused
      ? `天气暂停新派遣 · 执行 ${plannedCount} · 等待 ${waitingCount}`
      : (tasks.length ? `执行 ${plannedCount} · 等待 ${waitingCount}` : "当前空闲");

    if (!tasks.length) {
      panel.innerHTML = `<div class="dispatch-route-empty">当前没有需要调度或执行的任务路线。</div>`;
      return;
    }

    const visibleTasks = tasks.slice(0, 20);
    panel.innerHTML = visibleTasks.map(task => {
      const plan = task.route_plan;
      const uav = state.uavs.find(item => item.id === task.assigned_uav);
      const progress = plan && uav ? uavRouteProgress(uav) : 0;
      const legs = plan ? dispatchLegSummary(plan) : [];
      const blocked = ["WAITING_BLOCKED", "CARGO_EMERGENCY"].includes(task.status);
      const itemClass = plan ? "" : (blocked ? "blocked" : "waiting");
      const stage = uav ? uavStateText(uav) : statusText(task.status);
      const routeMeta = plan
        ? `路线 v${Number(plan.revision || task.route_revision || 1)} · ${Number(task.selected_altitude || 0).toFixed(0)}m航层${Number(task.planned_swap_count || 0) ? ` · 换电${Number(task.planned_swap_count)}次` : ""}`
        : escapeHtml(weatherPaused
          ? `天气暂停派遣：${state.weather?.pause_reason || state.weather?.dispatch_status || "等待天气恢复"}`
          : (task.message || "等待分配无人机"));
      const reason = plan?.reason || task.message || "调度路线已生成";
      return `
        <div class="dispatch-route-item ${itemClass}" data-dispatch-task="${escapeHtml(task.id)}">
          <div class="dispatch-route-top">
            <span class="dispatch-route-id">${escapeHtml(task.id)} · ${escapeHtml(task.assigned_uav || "待分配")}</span>
            <span class="dispatch-route-state">${escapeHtml(stage)}</span>
          </div>
          <div class="dispatch-route-path">${escapeHtml(taskPickupLabel(task))} → ${escapeHtml(task.destination?.name || "终点")}</div>
          <div class="dispatch-route-legs">${legs.length ? legs.map(escapeHtml).join("<br>") : routeMeta}</div>
          ${plan ? `<div class="dispatch-route-progress"><i style="width:${progress.toFixed(1)}%"></i></div>` : ""}
          ${plan ? `<div class="dispatch-route-meta">
            <span>${plan ? escapeHtml(routeMeta) : escapeHtml(reason)}</span>
            <button class="dispatch-route-focus" type="button" data-focus-dispatch-route="${escapeHtml(task.id)}">地图定位</button>
          </div>` : ""}
          ${plan ? `<div class="dispatch-route-meta"><span>${escapeHtml(reason)}</span><span>当前航段 ${progress.toFixed(0)}%</span></div>` : ""}
        </div>`;
    }).join("") + (tasks.length > visibleTasks.length
      ? `<div class="dispatch-route-empty">另有 ${tasks.length - visibleTasks.length} 条任务，可在任务队列中查看。</div>`
      : "");
  }

  function focusDispatchRoute(taskId) {
    const task = currentState.tasks.find(item => item.id === taskId);
    const plan = task?.route_plan;
    if (!plan) return;
    const route = ["takeoff_to_pickup", "pickup_to_destination", "cargo_recovery"]
      .flatMap(key => plan[key]?.route || []);
    const positions = routePositions(route);
    if (!positions.length) return;
    stopFollowing();
    const sphere = Cesium.BoundingSphere.fromPoints(positions);
    viewer.camera.flyToBoundingSphere(sphere, {
      duration: 1.0,
      offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-52), Math.max(500, sphere.radius * 2.8)),
    });
    scheduleVisibleBuildings(100);
  }

  function updateTables(state) {
    $("mUavs").textContent = state.uavs.length;
    $("mBusy").textContent = state.uavs.filter(u => u.state === "BUSY").length;
    $("mWaiting").textContent = state.tasks.filter(t => ["WAITING", "WAITING_BLOCKED", "WAITING_HANDOVER", "CARGO_EMERGENCY"].includes(t.status)).length;
    $("mDone").textContent = state.tasks.filter(t => t.status === "COMPLETED").length;
    updateWeatherCard(state.weather || {});
    updateAirspaceLayer(state.airspace || {});
    updateDispatchRoutePanel(state);
    updateFollowSelector(state);

    $("taskRows").innerHTML = state.tasks.slice().sort((a,b) => b.created_at - a.created_at).map(t => `
      <tr title="${escapeHtml(t.message || "")}">
        <td><b>${t.id}</b><br><span class="muted">${escapeHtml(taskPickupLabel(t))} → ${escapeHtml(t.destination.name || "终点")}</span>${cargoText(t) ? `<br><span class="muted">${escapeHtml(cargoText(t))}</span>` : ""}</td>
        <td>${escapeHtml(t.delivery_label)}<br><span class="tag ${priorityClass(t.priority)}">${priorityText(t.priority)}</span></td>
        <td>${t.assigned_uav || "--"}${t.selected_altitude ? `<br><span class="muted">航层 ${Number(t.selected_altitude).toFixed(0)}m · 建筑 ${Number(t.buildings_considered || 0).toLocaleString()}</span>` : ""}${t.route_plan ? `<br><span class="muted">线路 v${Number(t.route_plan.revision || t.route_revision || 1)}${Number(t.planned_swap_count || 0) ? ` · 预计换电 ${Number(t.planned_swap_count)} 次` : ""}</span>` : ""}${t.estimated_energy_percent != null ? `<br><span class="muted">预计耗电 ${Number(t.estimated_energy_percent).toFixed(1)}%</span>` : ""}</td>
        <td>${statusText(t.status)}${["PLANNING_FAILED", "WAITING_BLOCKED", "CARGO_EMERGENCY"].includes(t.status) ? `<br><span class="planning-error">${escapeHtml(t.message || "未找到安全航线")}</span>` : (t.status === "WAITING_HANDOVER" ? `<br><span class="muted">${escapeHtml(t.message || "等待接驳")}</span>` : (t.blocking_buildings ? `<br><span class="muted">阻挡楼 ${Number(t.blocking_buildings).toLocaleString()}</span>` : ""))}</td>
      </tr>`).join("") || `<tr><td colspan="4">暂无任务</td></tr>`;

    $("uavRows").innerHTML = state.uavs.map(u => `
      <tr data-uav="${u.id}" title="${escapeHtml(u.last_event || "")}">
        <td><b>${u.id}</b><br><span class="muted">${u.task_id || u.base}</span></td>
        <td>${uavStateText(u)}</td>
        <td>${Number(u.battery).toFixed(1)}%</td>
        <td>${Number(u.alt).toFixed(0)}m</td>
      </tr>`).join("");

    const old = $("eventUav").value;
    $("eventUav").innerHTML = `<option value="">自动选择飞行中的无人机</option>` + state.uavs
      .filter(u => u.state === "BUSY" && u.task_id)
      .map(u => `<option value="${u.id}">${u.id} · ${uavStateText(u)}</option>`).join("");
    if ([...$("eventUav").options].some(o => o.value === old)) $("eventUav").value = old;

    updateFlightLogTable(state.flight_logs || []);
  }

  function formatLogDuration(seconds) {
    const value = Math.max(0, Number(seconds) || 0);
    if (value < 60) return `${value.toFixed(1)}秒`;
    return `${Math.floor(value / 60)}分${Math.round(value % 60)}秒`;
  }

  function updateFlightLogTable(logs) {
    const rows = $("flightLogRows");
    if (!rows) return;
    rows.innerHTML = logs.map(log => {
      const taskId = String(log.task_id || "");
      const detailUrl = `/api/flight-logs/${encodeURIComponent(taskId)}`;
      const downloadUrl = `${detailUrl}/download`;
      return `
        <tr>
          <td><b>${escapeHtml(taskId)}</b><br><span class="muted">${escapeHtml(log.origin || "起点")} → ${escapeHtml(log.destination || "终点")}</span></td>
          <td>${(Number(log.actual_distance_m || 0) / 1000).toFixed(2)}km<br><span class="muted">耗电 ${Number(log.gross_energy_used_percent || 0).toFixed(1)}%</span></td>
          <td>${escapeHtml(log.uav_ids || "--")}<br><span class="muted">${formatLogDuration(log.duration_seconds)} · 换电 ${Number(log.swap_count || 0)} 次</span></td>
          <td><a class="flight-log-download" href="${downloadUrl}" download>下载 JSON</a><br><a class="flight-log-download muted" href="${detailUrl}" target="_blank" rel="noopener">查看</a></td>
        </tr>`;
    }).join("") || `<tr><td colspan="4">暂无已完成飞行日志</td></tr>`;
    const summaryLink = $("downloadFlightSummary");
    if (summaryLink) {
      summaryLink.classList.toggle("disabled", logs.length === 0);
      summaryLink.setAttribute("aria-disabled", logs.length === 0 ? "true" : "false");
    }
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  }

  function routeSignature(route) {
    if (!route || !route.length) return "0";
    const a = route[0], b = route[route.length - 1];
    return `${route.length}:${a.lon.toFixed(4)}:${a.lat.toFixed(4)}:${b.lon.toFixed(4)}:${b.lat.toFixed(4)}:${route.map(p => Math.round(p.alt || 0)).join("-")}`;
  }

  function taskColor(taskId) {
    let hash = 0;
    for (const ch of String(taskId || "")) hash = ((hash * 31) + ch.charCodeAt(0)) >>> 0;
    return Cesium.Color.fromCssColorString(COLORS[hash % COLORS.length]);
  }

  function routePositions(route) {
    return (route || []).map((p) => {
      const lon = Number(p.lon), lat = Number(p.lat), alt = Number(p.alt ?? 80);
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
      return Cesium.Cartesian3.fromDegrees(lon, lat, Math.max(2, Number.isFinite(alt) ? alt : 80));
    }).filter(Boolean);
  }

  function removeTaskRouteGraphic(taskId) {
    const graphic = taskRouteGraphics.get(taskId);
    if (!graphic) return;
    for (const entity of graphic.entities) viewer.entities.remove(entity);
    taskRouteGraphics.delete(taskId);
  }

  function addTaskRouteMarker(entities, task, point, label, color, suffix) {
    if (!point) return;
    const lon = Number(point.lon), lat = Number(point.lat), alt = Number(point.alt ?? 5);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
    entities.push(viewer.entities.add({
      id: `task-route-${task.id}-${suffix}-${task.route_plan?.revision ?? task.route_revision ?? 0}`,
      name: `${task.id} ${label}`,
      position: Cesium.Cartesian3.fromDegrees(lon, lat, Math.max(5, Number.isFinite(alt) ? alt : 5)),
      point: {
        pixelSize: 10,
        color,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
        disableDepthTestDistance: 6000,
      },
      label: {
        text: `${task.id} · ${label}`,
        font: "12px sans-serif",
        pixelOffset: new Cesium.Cartesian2(0, -22),
        fillColor: Cesium.Color.WHITE,
        showBackground: true,
        backgroundColor: new Cesium.Color(.03, .07, .12, .82),
        disableDepthTestDistance: 6000,
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 18000),
      },
    }));
  }

  function updateTaskRoutes(tasks) {
    const presentIds = new Set((tasks || []).map(t => t.id));
    for (const taskId of taskRouteGraphics.keys()) {
      if (!presentIds.has(taskId)) removeTaskRouteGraphic(taskId);
    }

    for (const task of tasks || []) {
      const plan = task.route_plan;
      const pickupRoute = plan?.takeoff_to_pickup?.route || [];
      const deliveryRoute = plan?.pickup_to_destination?.route || [];
      const cargoRecoveryRoute = plan?.cargo_recovery?.route || [];
      if (!plan || (pickupRoute.length < 2 && deliveryRoute.length < 2 && cargoRecoveryRoute.length < 2)) {
        if (task.cargo_status === "WAITING_HANDOVER" && task.cargo_location) {
          const signature = `handover:${task.route_revision || 0}:${task.cargo_location.lon}:${task.cargo_location.lat}`;
          if (taskRouteGraphics.get(task.id)?.signature === signature) continue;
          removeTaskRouteGraphic(task.id);
          const entities = [];
          addTaskRouteMarker(entities, task, task.cargo_location, "货物交接点", Cesium.Color.ORANGE, "cargo-waiting");
          taskRouteGraphics.set(task.id, { signature, entities });
        } else {
          removeTaskRouteGraphic(task.id);
        }
        continue;
      }

      const signature = `${plan.revision || task.route_revision || 0}:${task.status}:${task.cargo_status || ""}:${plan.assigned_uav || ""}`;
      if (taskRouteGraphics.get(task.id)?.signature === signature) continue;
      removeTaskRouteGraphic(task.id);

      const entities = [];
      const baseColor = taskColor(task.id);
      const opacity = task.status === "COMPLETED" ? 0.38 : 0.92;
      const pickupPositions = routePositions(pickupRoute);
      const deliveryPositions = routePositions(deliveryRoute);
      const cargoRecoveryPositions = routePositions(cargoRecoveryRoute);

      if (cargoRecoveryPositions.length >= 2) {
        entities.push(viewer.entities.add({
          id: `task-route-${task.id}-cargo-recovery-${plan.revision}`,
          name: `${task.id} 携货安全交接路线`,
          polyline: {
            positions: cargoRecoveryPositions,
            width: 5,
            material: new Cesium.PolylineDashMaterialProperty({
              color: Cesium.Color.ORANGE.withAlpha(.96),
              dashLength: 14,
            }),
          },
        }));
        addTaskRouteMarker(entities, task, cargoRecoveryRoute[0], "携货异常位置", Cesium.Color.YELLOW, "cargo-recovery-start");
        addTaskRouteMarker(entities, task, cargoRecoveryRoute[cargoRecoveryRoute.length - 1], "安全交接点", Cesium.Color.ORANGE, "cargo-handover");
        taskRouteGraphics.set(task.id, { signature, entities });
        continue;
      }

      if (pickupPositions.length >= 2) {
        entities.push(viewer.entities.add({
          id: `task-route-${task.id}-pickup-line-${plan.revision}`,
          name: `${task.id} 起飞至取货点`,
          polyline: {
            positions: pickupPositions,
            width: 4,
            material: new Cesium.PolylineDashMaterialProperty({
              color: baseColor.withAlpha(opacity),
              dashLength: 18,
            }),
          },
        }));
      }
      if (deliveryPositions.length >= 2) {
        entities.push(viewer.entities.add({
          id: `task-route-${task.id}-delivery-line-${plan.revision}`,
          name: `${task.id} 取货点至终点`,
          polyline: {
            positions: deliveryPositions,
            width: 5,
            material: new Cesium.PolylineGlowMaterialProperty({
              color: baseColor.withAlpha(opacity),
              glowPower: 0.16,
            }),
          },
        }));
      }

      addTaskRouteMarker(entities, task, pickupRoute[0], "起飞点", Cesium.Color.LIME, "takeoff");
      addTaskRouteMarker(
        entities,
        task,
        pickupRoute[pickupRoute.length - 1] || deliveryRoute[0],
        (task.handover_history?.length || 0) > 0 ? "交接取货点" : "取货点",
        Cesium.Color.ORANGE,
        "pickup",
      );
      addTaskRouteMarker(
        entities,
        task,
        deliveryRoute[deliveryRoute.length - 1],
        "终点",
        Cesium.Color.RED,
        "destination",
      );
      for (const [legIndex, leg] of [plan.takeoff_to_pickup, plan.pickup_to_destination].entries()) {
        for (const station of leg?.swap_stops || []) {
          addTaskRouteMarker(
            entities,
            task,
            { lon: station.lon, lat: station.lat, alt: 3 },
            `换电 · ${station.name}`,
            Cesium.Color.CYAN,
            `swap-${legIndex}-${station.id}`,
          );
        }
      }
      taskRouteGraphics.set(task.id, { signature, entities });
    }
  }

  function updateBatteryStations(stations) {
    const presentIds = new Set((stations || []).map(station => station.id));
    for (const [stationId, entity] of batteryStationGraphics.entries()) {
      if (!presentIds.has(stationId)) {
        viewer.entities.remove(entity);
        batteryStationGraphics.delete(stationId);
      }
    }
    for (const station of stations || []) {
      if (batteryStationGraphics.has(station.id)) continue;
      const entity = viewer.entities.add({
        id: `battery-station-${station.id}`,
        name: `${station.id} ${station.name}`,
        position: Cesium.Cartesian3.fromDegrees(Number(station.lon), Number(station.lat), 5),
        point: {
          pixelSize: 13,
          color: Cesium.Color.CYAN.withAlpha(.9),
          outlineColor: Cesium.Color.fromCssColorString("#07374a"),
          outlineWidth: 3,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: `⚡ ${station.name}`,
          font: "12px sans-serif",
          pixelOffset: new Cesium.Cartesian2(0, -23),
          fillColor: Cesium.Color.CYAN,
          showBackground: true,
          backgroundColor: new Cesium.Color(.02, .10, .15, .82),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 16000),
        },
      });
      batteryStationGraphics.set(station.id, entity);
    }
  }

  function updateUavBases(bases, uavs) {
    const presentIds = new Set((bases || []).map((base, index) => base.id || `BASE-${index + 1}`));
    for (const [baseId, entity] of uavBaseGraphics.entries()) {
      if (!presentIds.has(baseId)) {
        viewer.entities.remove(entity);
        uavBaseGraphics.delete(baseId);
      }
    }
    for (const [index, base] of (bases || []).entries()) {
      const baseId = base.id || `BASE-${index + 1}`;
      const lon = Number(base.lon), lat = Number(base.lat);
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
      const label = base.label || base.name || baseId;
      const idleCount = (uavs || []).filter(uav => uav.base === base.name && uav.state === "IDLE").length;
      const chargingCount = (uavs || []).filter(uav => uav.base === base.name && uav.phase === "BASE_CHARGING").length;
      const baseStatus = `待命 ${idleCount} 架 · 充电 ${chargingCount} 架`;
      if (uavBaseGraphics.has(baseId)) {
        uavBaseGraphics.get(baseId).label.text = `◆ ${label} · ${baseStatus}`;
        continue;
      }
      const entity = viewer.entities.add({
        id: `uav-base-${baseId}`,
        name: `${baseId} ${label}`,
        position: Cesium.Cartesian3.fromDegrees(lon, lat, 6),
        point: {
          pixelSize: 19,
          color: Cesium.Color.fromCssColorString("#ff63e6").withAlpha(.96),
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 4,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: `◆ ${label} · ${baseStatus}`,
          font: "bold 13px Microsoft YaHei, sans-serif",
          pixelOffset: new Cesium.Cartesian2(0, -29),
          fillColor: Cesium.Color.fromCssColorString("#ffb8f3"),
          showBackground: true,
          backgroundColor: new Cesium.Color(.16, .02, .16, .88),
          backgroundPadding: new Cesium.Cartesian2(7, 5),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 65000),
        },
      });
      uavBaseGraphics.set(baseId, entity);
    }
  }

  function ensureUavGraphic(u, index) {
    let g = uavGraphics.get(u.id);
    const color = Cesium.Color.fromCssColorString(COLORS[index % COLORS.length]);
    if (!g) {
      const live = { lon: u.lon, lat: u.lat, alt: u.alt };
      const target = { lon: u.lon, lat: u.lat, alt: u.alt };
      const headingState = { value: 0 };
      const entity = viewer.entities.add({
        id: `entity-${u.id}`,
        position: new Cesium.CallbackPositionProperty(() => Cesium.Cartesian3.fromDegrees(live.lon, live.lat, Math.max(2, live.alt)), false),
        // 本地 SVG 四旋翼图标不依赖外部模型服务；朝向随飞行航向持续更新。
        billboard: {
          image: "/static/drone.svg",
          width: 54,
          height: 42,
          color,
          rotation: new Cesium.CallbackProperty(() => -headingState.value, false),
          alignedAxis: Cesium.Cartesian3.UNIT_Z,
          disableDepthTestDistance: 5000,
          scaleByDistance: new Cesium.NearFarScalar(40, 1.45, 30000, .55),
        },
        label: {
          text: u.id, font: "13px sans-serif", pixelOffset: new Cesium.Cartesian2(0, -24), fillColor: Cesium.Color.WHITE,
          showBackground: true, backgroundColor: new Cesium.Color(.04,.08,.13,.80), distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 25000),
        },
      });
      const trailPositions = [];
      const trail = viewer.entities.add({
        polyline: { positions: new Cesium.CallbackProperty(() => trailPositions, false), width: 2, material: color.withAlpha(.72) },
      });
      const routeEntity = viewer.entities.add({
        polyline: { positions: [], width: 3, material: color.withAlpha(.72) },
      });
      g = {
        entity, trail, routeEntity, live, target, trailPositions, last: null, routeSig: "",
        interpStart: { ...live }, interpTarget: { ...target }, interpStartMs: performance.now(), interpDurationMs: 520,
        heading: 0, targetHeading: 0, headingState, lastTelemetry: { lon: u.lon, lat: u.lat, alt: u.alt },
      };
      uavGraphics.set(u.id, g);
    }
    return g;
  }

  function updateMap(state) {
    state.uavs.forEach((u, i) => {
      const g = ensureUavGraphic(u, i);
      const previousTelemetry = { ...g.lastTelemetry };
      const nextTelemetry = { lon: Number(u.lon), lat: Number(u.lat), alt: Number(u.alt) };
      const newBearing = bearingRadians(previousTelemetry, nextTelemetry);
      if (newBearing !== null) g.targetHeading = newBearing;
      g.lastTelemetry = nextTelemetry;
      g.interpStart = { ...g.live };
      g.interpTarget = nextTelemetry;
      g.target = nextTelemetry;
      g.interpStartMs = performance.now();
      g.interpDurationMs = 540;
      g.entity.billboard.color = u.state === "IDLE"
        ? Cesium.Color.fromCssColorString("#72d39c")
        : (u.state === "CHARGING"
          ? Cesium.Color.fromCssColorString("#38d5e6")
          : (["HOLDING", "GROUNDED"].includes(u.state)
            ? Cesium.Color.fromCssColorString("#ff6868")
            : Cesium.Color.fromCssColorString(COLORS[i % COLORS.length])));
      const key = `${Number(u.lon).toFixed(6)},${Number(u.lat).toFixed(6)},${Number(u.alt).toFixed(1)}`;
      if (u.state === "BUSY" && g.last !== key) {
        g.trailPositions.push(Cesium.Cartesian3.fromDegrees(u.lon, u.lat, Math.max(2, u.alt)));
        if (g.trailPositions.length > 260) g.trailPositions.shift();
        g.last = key;
      }
      const sig = routeSignature(u.route);
      if (sig !== g.routeSig) {
        g.routeSig = sig;
        g.routeEntity.polyline.positions = (u.route || []).map(p => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.alt ?? 80));
        // 建筑视觉层由摄像机视野驱动，不再依赖任务航线。路径规划仍在后端使用真实 Height。
      }
      // 执行任务时由任务实体绘制完整路线；脱离任务后的返航/恢复路线由 UAV 实体显示。
      const recoveryRoute = !u.task_id && ["RETURNING", "TO_RECOVERY_SWAP"].includes(u.phase);
      g.routeEntity.show = recoveryRoute;
      if (recoveryRoute) {
        g.routeEntity.polyline.material = Cesium.Color.fromCssColorString(
          u.phase === "TO_RECOVERY_SWAP" ? "#38d5e6" : "#72d39c"
        ).withAlpha(.88);
      }
      g.trail.show = g.trailPositions.length > 1;
    });
    updateTaskRoutes(state.tasks);
    updateUavBases(state.uav_bases || [], state.uavs || []);
    updateBatteryStations(state.battery_stations || []);
  }

  function lerpNumber(a, b, t) {
    return a + (b - a) * t;
  }

  function maxLoadedBuildingHeightAt(lon, lat) {
    // 约 6m 水平余量，避免镜头贴着外墙抖动。
    const lonMargin = 6 / 102000;
    const latMargin = 6 / 111000;
    let height = 0;
    for (const volumes of localBuildingVolumes.values()) {
      for (const volume of volumes) {
        if (lon >= volume.minLon - lonMargin && lon <= volume.maxLon + lonMargin
            && lat >= volume.minLat - latMargin && lat <= volume.maxLat + latMargin) {
          height = Math.max(height, volume.height);
        }
      }
    }
    return height;
  }

  function collisionSafeFollowOffset(target, offset) {
    if (!localBuildingVolumes.size) return offset;
    const frame = Cesium.Transforms.eastNorthUpToFixedFrame(target);
    const targetCarto = Cesium.Cartographic.fromCartesian(target);
    const targetHeight = Number(targetCarto.height || 0);
    let requiredUp = offset.z;
    // 检查无人机至镜头视线上的多个位置；若落入楼体，整体抬高镜头到屋顶以上。
    for (const fraction of [0.25, 0.5, 0.75, 1.0]) {
      const localPoint = new Cesium.Cartesian3(
        offset.x * fraction,
        offset.y * fraction,
        requiredUp * fraction,
      );
      const worldPoint = Cesium.Matrix4.multiplyByPoint(frame, localPoint, new Cesium.Cartesian3());
      const carto = Cesium.Cartographic.fromCartesian(worldPoint);
      const lon = Cesium.Math.toDegrees(carto.longitude);
      const lat = Cesium.Math.toDegrees(carto.latitude);
      const roofHeight = maxLoadedBuildingHeightAt(lon, lat);
      if (roofHeight > 0 && Number(carto.height) < roofHeight + 10) {
        requiredUp = Math.max(requiredUp, (roofHeight + 10 - targetHeight) / fraction);
      }
    }
    if (requiredUp <= offset.z + 0.1) return offset;
    return new Cesium.Cartesian3(offset.x, offset.y, requiredUp);
  }

  let lastFollowOcclusionAdjustment = 0;

  function isFollowBuildingOccluder(picked) {
    if (!picked) return false;
    const entity = picked.id;
    if (entity && entity.id === `entity-${followedUavId}`) return false;
    if (entity && entity.polygon) return true;
    return typeof Cesium.Cesium3DTileFeature !== "undefined"
      && picked instanceof Cesium.Cesium3DTileFeature;
  }

  const followCanvas = viewer.scene.canvas;
  followCanvas.addEventListener("pointerdown", (ev) => {
    if (!followedUavId || ev.button !== 0) return;
    followDrag = { pointerId: ev.pointerId, x: ev.clientX, y: ev.clientY };
    try { followCanvas.setPointerCapture(ev.pointerId); } catch {}
    followCanvas.style.cursor = "grabbing";
    ev.preventDefault();
  });
  followCanvas.addEventListener("pointermove", (ev) => {
    if (!followedUavId || !followDrag || followDrag.pointerId !== ev.pointerId) return;
    const dx = ev.clientX - followDrag.x;
    const dy = ev.clientY - followDrag.y;
    followDrag.x = ev.clientX;
    followDrag.y = ev.clientY;
    followCamera.azimuth = normalizeAngle(followCamera.azimuth - dx * 0.006);
    followCamera.elevation = Math.max(
      Cesium.Math.toRadians(5),
      Math.min(Cesium.Math.toRadians(85), followCamera.elevation - dy * 0.004)
    );
    ev.preventDefault();
  });
  const endFollowDrag = (ev) => {
    if (!followDrag || followDrag.pointerId !== ev.pointerId) return;
    followDrag = null;
    if (followedUavId) followCanvas.style.cursor = "grab";
  };
  followCanvas.addEventListener("pointerup", endFollowDrag);
  followCanvas.addEventListener("pointercancel", endFollowDrag);
  followCanvas.addEventListener("wheel", (ev) => {
    if (!followedUavId) return;
    followCamera.distance = Math.max(
      35,
      Math.min(1600, followCamera.distance * Math.exp(ev.deltaY * 0.001))
    );
    ev.preventDefault();
  }, { passive: false });

  // 平滑 UAV 动画 + 可交互的环绕跟随摄像机。
  // telemetry 约 2Hz 到达，但这里每一帧插值，因此无人机和镜头不会一跳一跳。
  viewer.scene.preRender.addEventListener(() => {
    const now = performance.now();
    for (const g of uavGraphics.values()) {
      const t = Math.max(0, Math.min(1, (now - g.interpStartMs) / Math.max(1, g.interpDurationMs)));
      // smoothstep：起止更柔和。
      const k = t * t * (3 - 2 * t);
      g.live.lon = lerpNumber(g.interpStart.lon, g.interpTarget.lon, k);
      g.live.lat = lerpNumber(g.interpStart.lat, g.interpTarget.lat, k);
      g.live.alt = lerpNumber(g.interpStart.alt, g.interpTarget.alt, k);
      g.heading = normalizeAngle(g.heading + shortestAngleDelta(g.heading, g.targetHeading) * 0.12);
      g.headingState.value = g.heading;
    }

    if (!followedUavId) return;
    const g = uavGraphics.get(followedUavId);
    if (!g) return;

    const target = Cesium.Cartesian3.fromDegrees(g.live.lon, g.live.lat, Math.max(2, g.live.alt));
    const horizontal = followCamera.distance * Math.cos(followCamera.elevation);
    const up = followCamera.distance * Math.sin(followCamera.elevation);
    const orbitHeading = g.heading + followCamera.azimuth;
    // camera.lookAt 的 Cartesian3 offset 使用目标点局部 ENU 坐标：x=东、y=北、z=上。
    const requestedOffset = new Cesium.Cartesian3(
      Math.sin(orbitHeading) * horizontal,
      Math.cos(orbitHeading) * horizontal,
      up
    );
    const offset = collisionSafeFollowOffset(target, requestedOffset);
    try { viewer.camera.lookAt(target, offset); } catch {}
  });

  // 对本地 Entity 建筑和 Cesium OSM 3D Tiles 都做屏幕中心遮挡检测。
  // 若建筑挡在镜头与 UAV 之间，逐步提高环绕仰角，避免镜头继续穿入楼体。
  viewer.scene.postRender.addEventListener(() => {
    if (!followedUavId || performance.now() - lastFollowOcclusionAdjustment < 100) return;
    const canvas = viewer.scene.canvas;
    const center = new Cesium.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2);
    let picked;
    try { picked = viewer.scene.pick(center, 3, 3); } catch { return; }
    if (!isFollowBuildingOccluder(picked)) return;
    followCamera.elevation = Math.min(
      Cesium.Math.toRadians(85),
      followCamera.elevation + Cesium.Math.toRadians(2),
    );
    lastFollowOcclusionAdjustment = performance.now();
  });

  function applyState(state) {
    currentState = state;
    updateTables(state);
    updateMap(state);
    renderDispatchProgress(state.dispatch_progress);
    if (followedUavId) {
      const followed = state.uavs.find(u => u.id === followedUavId);
      if (followed) setFollowStatus(`跟随 ${followedUavId} · ${uavStateText(followed)} · 高度 ${Number(followed.alt).toFixed(0)}m · 左键环绕 / 滚轮缩放`, "status ok");
      else stopFollowing();
    }
    const weatherGate = state.weather?.dispatch_allowed === false ? " · 天气已暂停新派遣" : "";
    $("connectionStatus").textContent = `调度服务器已连接 · 仿真 ${state.running ? "运行中" : "暂停"} · ${state.speed_factor}×${weatherGate}`;
  }

  async function pollState() {
    try { applyState(await api("/api/state")); }
    catch (e) { $("connectionStatus").textContent = `状态读取失败：${e.message}`; }
  }

  function renderDispatchProgress(data) {
    const card = $("dispatchPlanningCard");
    const title = $("dispatchPlanningTitle");
    const percentLabel = $("dispatchPlanningPercent");
    const bar = $("dispatchPlanningBar");
    const detail = $("dispatchPlanningDetail");
    if (!card || !title || !percentLabel || !bar || !detail) return;

    const current = data?.current;
    if (!current) {
      card.className = "dispatch-planning-card idle";
      title.textContent = data?.busy ? "正在启动调度计算" : "调度进度";
      percentLabel.textContent = "0%";
      bar.style.width = "0%";
      bar.parentElement.setAttribute("aria-valuenow", "0");
      detail.textContent = data?.busy ? "正在读取等待任务…" : "当前没有正在计算的调度方案。";
      return;
    }

    const percent = Math.max(0, Math.min(100, Number(current.percent) || 0));
    const active = Boolean(current.active || data?.busy);
    const failed = !active && (current.task_status === "WAITING_BLOCKED" || String(current.stage).includes("未通过"));
    card.className = `dispatch-planning-card ${active ? "active" : (failed ? "failed" : (percent >= 100 ? "done" : "idle"))}`;
    title.textContent = `${current.task_id} · ${current.stage || "等待调度"}`;
    percentLabel.textContent = `${percent.toFixed(0)}%`;
    bar.style.width = `${percent}%`;
    bar.parentElement.setAttribute("aria-valuenow", String(percent));
    const route = `${current.origin || "起点"} → ${current.destination || "终点"}`;
    const waiting = Number(data?.waiting_count || 0);
    detail.textContent = `${route} · ${current.detail || ""}${waiting > 1 ? ` · 队列另有 ${waiting - 1} 条` : ""}`;
  }

  let dispatchProgressPolling = false;
  async function pollDispatchProgress() {
    if (dispatchProgressPolling) return;
    dispatchProgressPolling = true;
    try { renderDispatchProgress(await api("/api/dispatch/progress")); }
    catch {}
    finally { dispatchProgressPolling = false; }
  }

  function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
    ws.onopen = () => { $("connectionStatus").textContent = "WebSocket 已连接"; ws.send("ping"); };
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === "state") applyState(data);
      } catch {}
      if (ws && ws.readyState === WebSocket.OPEN) setTimeout(() => { try { ws.send("ping"); } catch {} }, 8000);
    };
    ws.onclose = () => { $("connectionStatus").textContent = "WebSocket 已断开，使用轮询状态"; setTimeout(connectWebSocket, 2500); };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }

  $("focusShenzhenBtn").addEventListener("click", () => {
    stopFollowing();
    focusShenzhen();
  });
  $("followUav").addEventListener("change", (ev) => followUav(ev.target.value));
  $("stopFollowBtn").addEventListener("click", stopFollowing);
  $("refreshWeatherBtn").addEventListener("click", async () => {
    const button = $("refreshWeatherBtn");
    button.disabled = true;
    button.textContent = "↻";
    try {
      const weather = await api("/api/weather/refresh", { method: "POST" });
      updateWeatherCard(weather);
    } catch (error) {
      $("weatherDetails").textContent = `天气刷新失败：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
  $("applyWeatherSimulationBtn").addEventListener("click", async () => {
    const presets = {
      CLEAR: { weather: "晴", temperature_c: 27, humidity_percent: 65, wind_direction: "东南", wind_power: "≤3" },
      LIGHT_RAIN: { weather: "小雨", temperature_c: 25, humidity_percent: 88, wind_direction: "东", wind_power: "3" },
      THUNDERSTORM: { weather: "雷阵雨", temperature_c: 26, humidity_percent: 94, wind_direction: "南", wind_power: "6" },
      GALE: { weather: "晴", temperature_c: 27, humidity_percent: 70, wind_direction: "北", wind_power: "7" },
    };
    const button = $("applyWeatherSimulationBtn");
    const payload = presets[$("weatherSimulationPreset").value] || presets.CLEAR;
    button.disabled = true;
    try {
      const weather = await api("/api/weather/simulated", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      updateWeatherCard(weather);
    } catch (error) {
      alert(`模拟天气设置失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
  $("airspaceApprovalPanel").addEventListener("change", async (event) => {
    const select = event.target.closest("select[data-airspace-zone]");
    if (!select) return;
    const approved = select.value === "true";
    select.disabled = true;
    try {
      const airspace = await api(`/api/airspace/zones/${encodeURIComponent(select.dataset.airspaceZone)}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
      currentState.airspace = airspace;
      airspaceRenderSignature = "";
      updateAirspaceLayer(airspace);
    } catch (error) {
      alert(`空域审批状态修改失败：${error.message}`);
      select.value = approved ? "false" : "true";
      select.disabled = false;
    }
  });
  $("uavRows").addEventListener("click", (ev) => {
    const row = ev.target.closest("tr[data-uav]");
    if (!row) return;
    followUav(row.dataset.uav);
  });
  $("dispatchRoutePanel").addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-focus-dispatch-route]");
    if (button) focusDispatchRoute(button.dataset.focusDispatchRoute);
  });
  $("loadOsmBuildingsBtn").addEventListener("click", async () => {
    try { await loadOsmBuildings(); $("dataStatus").textContent = "OSM 3D Buildings 已加载。"; $("dataStatus").className = "status ok"; }
    catch (e) { $("dataStatus").textContent = e.message; $("dataStatus").className = "status bad"; }
  });
  $("dataMode").addEventListener("change", () => {
    setDataVisibility();
    const mode = $("dataMode").value;
    if (mode === "shenzhen") $("dataStatus").textContent = "本地深圳建筑模式：Height 同时用于规划与 3D 拉伸显示。";
    if (mode === "cesium") $("dataStatus").textContent = "Cesium 仅切换视觉建筑层；后端规划仍使用你提供的深圳 Height 建筑库。";
    if (mode === "custom") $("dataStatus").textContent = "自定义约束模式：深圳建筑库 + 上传 GeoJSON 共同参与规划。";
    if (mode !== "cesium") scheduleVisibleBuildings(50);
  });

  $("uploadBtn").addEventListener("click", async () => {
    const file = $("geojsonFile").files[0];
    if (!file) { $("dataStatus").textContent = "请先选择 GeoJSON 文件。"; return; }
    const fd = new FormData(); fd.append("file", file);
    $("dataStatus").textContent = "上传中…";
    try {
      const result = await api(`/api/data/upload?kind=${encodeURIComponent($("uploadKind").value)}`, { method: "POST", body: fd });
      $("dataMode").value = "custom";
      await refreshCustomData();
      $("dataStatus").textContent = `加载成功：建筑 ${result.added.building}，禁飞区 ${result.added.no_fly}，起降点 ${result.added.landing_site}。`;
      $("dataStatus").className = "status ok";
    } catch (e) { $("dataStatus").textContent = e.message; $("dataStatus").className = "status bad"; }
  });

  $("clearCustomBtn").addEventListener("click", async () => {
    await api("/api/data/custom", { method: "DELETE" });
    clearCustomVisuals();
    $("dataStatus").textContent = "自定义数据已清空。";
  });

  $("initFleetBtn").addEventListener("click", async () => {
    try {
      const state = await api("/api/fleet/init", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ count: Number($("fleetCount").value) }) });
      applyState(state);
    } catch (e) { alert(e.message); }
  });

  async function updateControl(running) {
    const body = { speed_factor: Number($("speedFactor").value), auto_events: $("autoEvents").checked };
    if (running !== undefined) body.running = running;
    applyState(await api("/api/control", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) }));
  }
  $("startSimBtn").addEventListener("click", () => updateControl(true).catch(e => alert(e.message)));
  $("pauseSimBtn").addEventListener("click", () => updateControl(false).catch(e => alert(e.message)));
  $("speedFactor").addEventListener("change", () => updateControl(undefined).catch(() => {}));
  $("autoEvents").addEventListener("change", () => updateControl(undefined).catch(() => {}));
  $("resetBtn").addEventListener("click", async () => {
    if (!confirm("确定重置任务和无人机运行状态吗？自定义城市数据不会删除。")) return;
    stopFollowing();
    for (const g of uavGraphics.values()) { viewer.entities.remove(g.entity); viewer.entities.remove(g.trail); viewer.entities.remove(g.routeEntity); }
    uavGraphics.clear();
    for (const taskId of [...taskRouteGraphics.keys()]) removeTaskRouteGraphic(taskId);
    applyState(await api("/api/reset", { method: "POST" }));
  });

  async function createTaskFromCurrentForm(status) {
    mapPickTarget = null;
    viewer.scene.canvas.style.cursor = "";
    status.className = "status"; status.textContent = "正在解析起点…";
    const origin = await geocodePlace($("originInput").value, "出发地");
    status.textContent = "起点已确认，正在解析终点…";
    const destination = await geocodePlace($("destInput").value, "目的地");
    const task = await api("/api/tasks", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        origin, destination,
        delivery_type: $("deliveryType").value,
        priority: $("priority").value,
        payload_kg: Number($("payloadKg").value),
        deadline_minutes: Number($("deadline").value),
        cruise_alt: Number($("cruiseAlt").value),
        data_mode: $("dataMode").value,
      }),
    });
    status.className = "status ok";
    status.textContent = `${task.id} 已加入队列：${origin.name} → ${destination.name}`;
    // 创建任务后由摄像机飞到任务区域，并按视野自动流式加载全部本地建筑。
    await pollState();
    const centerLon = (origin.lon + destination.lon) / 2, centerLat = (origin.lat + destination.lat) / 2;
    const approxDist = Cesium.Cartesian3.distance(Cesium.Cartesian3.fromDegrees(origin.lon, origin.lat), Cesium.Cartesian3.fromDegrees(destination.lon, destination.lat));
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(centerLon, centerLat, Math.max(5000, approxDist * 1.2)), duration: 1.2 });
    return task;
  }

  $("aiParseTaskBtn").addEventListener("click", () => {
    parseAiTask().catch((e) => setAiStatus(e.message, "status bad"));
  });
  $("aiCreateTaskBtn").addEventListener("click", async () => {
    const status = $("aiServiceStatus");
    $("aiCreateTaskBtn").disabled = true;
    try {
      const task = await createTaskFromCurrentForm(status);
      setAiStatus(`${task.id} 已通过 AI 草案确认并加入任务队列。`, "status ok");
    } catch (e) {
      setAiStatus(e.message, "status bad");
      $("aiCreateTaskBtn").disabled = false;
    }
  });
  $("batchParseFileBtn").addEventListener("click", () => {
    parseBatchTaskFile().catch(error => setBatchStatus(error.message, "status bad"));
  });
  $("batchAiParseBtn").addEventListener("click", () => {
    parseBatchAiTasks().catch(error => setBatchStatus(error.message, "status bad"));
  });
  $("batchCreateBtn").addEventListener("click", () => {
    createBatchTasks().catch(error => setBatchStatus(error.message, "status bad"));
  });
  $("downloadBatchTemplateBtn").addEventListener("click", downloadBatchTemplate);
  $("addTaskBtn").addEventListener("click", async () => {
    const status = $("taskFormStatus");
    try { await createTaskFromCurrentForm(status); }
    catch (e) { status.className = "status bad"; status.textContent = e.message; }
  });
  $("pickOriginBtn").addEventListener("click", () => beginMapPick("origin"));
  $("pickDestBtn").addEventListener("click", () => beginMapPick("destination"));
  $("testGeocoderBtn").addEventListener("click", () => testGeocoderCall());

  $("triggerEventBtn").addEventListener("click", async () => {
    const status = $("eventStatus");
    try {
      const result = await api("/api/events", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ event_type: $("eventType").value, uav_id: $("eventUav").value || null }),
      });
      status.textContent = result.message;
      status.className = result.ok ? "status ok" : "status bad";
      await pollState();
      if ($("eventType").value === "temp_no_fly" && result.ok) await refreshCustomData();
    } catch (e) { status.textContent = e.message; status.className = "status bad"; }
  });

  // 地图自由浏览时，摄像机停下就按当前视野加载深圳本地 3D 建筑。
  viewer.camera.moveEnd.addEventListener(() => scheduleVisibleBuildings(120));
  // 追尾模式下摄像机持续移动，moveEnd 不会触发，因此每 1.1 秒刷新一次视野建筑。
  setInterval(() => { if (followedUavId) loadVisibleBuildings().catch(() => {}); }, 1100);

  loadCesiumIonBaseMap().catch((e) => console.error(e));
  refreshBuildingStats();
  refreshAiStatus();
  refreshGeocoderStatus();
  refreshCustomData().catch(() => {});
  pollState();
  pollDispatchProgress();
  connectWebSocket();
  setTimeout(() => scheduleVisibleBuildings(20), 1000);
  setInterval(() => { if (!ws || ws.readyState !== WebSocket.OPEN) pollState(); }, 1500);
  setInterval(pollDispatchProgress, 650);
})();
