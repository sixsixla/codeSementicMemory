import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const state = { graph: null, quality: null, tasks: [], selectedTask: "", selectedNode: null, nodeObjects: new Map(), scene: null, camera: null, renderer: null, controls: null, frame: 0 };
const $ = (id) => document.getElementById(id);
const colors = { card: 0xd8c9ff, memory: 0xa98cff, card_version: 0x8d7bbd, event: 0x55d6e7, file: 0xffb86b, symbol: 0x6ee7ad, task: 0xf3f4f6, session: 0x91a7ff };

async function requestJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadTasks() {
  const data = await requestJson("/v1/tasks?limit=100");
  state.tasks = data.tasks || [];
  const select = $("task-select");
  select.replaceChildren();
  if (!state.tasks.length) {
    select.add(new Option("暂无任务", ""));
    return;
  }
  for (const task of state.tasks) select.add(new Option(task.title || task.task_id, task.task_id));
  if (!state.selectedTask || !state.tasks.some((task) => task.task_id === state.selectedTask)) state.selectedTask = state.tasks[0].task_id;
  select.value = state.selectedTask;
}

async function loadGraph() {
  const suffix = state.selectedTask ? `?task_id=${encodeURIComponent(state.selectedTask)}&limit=500` : "?limit=500";
  const graph = await requestJson(`/v1/graph${suffix}`);
  state.graph = graph;
  renderStats(graph);
  await loadQuality();
  renderCards(state.selectedTask);
  renderCandidates(state.selectedTask);
  renderGraph(graph);
  $("connection-status").textContent = "SQLite API 已连接";
  $("connection-status").previousElementSibling.classList.add("ok");
}

async function loadQuality() {
  const suffix = state.selectedTask ? `?task_id=${encodeURIComponent(state.selectedTask)}` : "";
  try {
    const report = await requestJson(`/v1/quality/report${suffix}`);
    state.quality = report;
    const events = report.events || {}, candidates = report.candidates || {};
    const accepted = (events.decisions?.accepted || 0) + (candidates.decisions?.accepted || 0);
    const review = (events.decisions?.review || 0) + (candidates.decisions?.review || 0);
    const quarantine = (events.decisions?.quarantine || 0) + (candidates.decisions?.quarantine || 0);
    $("quality-count").textContent = `${accepted} / ${review}`;
    $("quality-version").textContent = report.classifier_version || "gate";
    const mismatches = (events.scope_mismatches || 0) + (candidates.scope_mismatches || 0);
    $("quality-summary").innerHTML = `<div class="quality-row"><span>已评估事件</span><b>${events.evaluated || 0}/${events.total || 0}</b></div><div class="quality-row"><span>已审候选</span><b>${candidates.reviewed || 0}/${candidates.total || 0}</b></div><div class="quality-row"><span>隔离</span><b class="quality-quarantine">${quarantine}</b></div><div class="quality-row"><span>跨项目引用</span><b class="quality-mismatch">${mismatches}</b></div>`;
  } catch (error) {
    state.quality = null;
    $("quality-count").textContent = "—";
    $("quality-summary").innerHTML = `<div class="empty">质量报告读取失败：${escapeHtml(error.message)}</div>`;
  }
}

async function renderCards(taskId) {
  const list = $("card-list");
  try {
    const suffix = taskId ? `?task_id=${encodeURIComponent(taskId)}&limit=100` : "?limit=100";
    const data = await requestJson(`/v1/cards${suffix}`);
    const cards = data.cards || [];
    $("card-count").textContent = cards.length;
    list.replaceChildren();
    if (!cards.length) {
      list.innerHTML = '<div class="empty">还没有版本化卡片。运行 <code>codememory consolidate</code> 后刷新。</div>';
      return;
    }
    for (const card of cards) {
      const item = document.createElement("div");
      item.className = "card-item";
      item.dataset.node = `card:${card.card_id}`;
      const quality = card.quality?.decision || "legacy_unreviewed";
      item.innerHTML = `<strong>${escapeHtml(card.statement || card.card_id)}</strong><small><span>${escapeHtml(card.status)} · ${escapeHtml(card.kind)}</span><span class="quality-${escapeHtml(quality)}">${escapeHtml(quality)} · ${Math.round(card.confidence * 100)}%</span></small>`;
      item.addEventListener("click", () => selectNode(`card:${card.card_id}`));
      list.append(item);
    }
  } catch (error) {
    list.innerHTML = `<div class="empty">卡片读取失败：${escapeHtml(error.message)}</div>`;
  }
}

async function renderCandidates(taskId) {
  const list = $("candidate-list");
  try {
    const data = await requestJson(`/v1/tasks/${encodeURIComponent(taskId)}/memories?limit=100`);
    const memories = data.memories || [];
    $("candidate-count").textContent = memories.length;
    list.replaceChildren();
    if (!memories.length) { list.innerHTML = '<div class="empty">还没有候选记忆。运行 <code>codememory extract</code> 后刷新。</div>'; return; }
    for (const memory of memories) {
      const item = document.createElement("div"); item.className = "candidate-item"; item.dataset.node = `candidate:${memory.candidate_id}`;
      const quality = memory.quality?.decision || "legacy_unreviewed";
      item.innerHTML = `<strong>${escapeHtml(memory.statement)}</strong><small><span>${escapeHtml(memory.kind)}</span><span class="quality-${escapeHtml(quality)}">${escapeHtml(quality)} · ${Math.round(memory.confidence * 100)}%</span></small>`;
      item.addEventListener("click", () => selectNode(`candidate:${memory.candidate_id}`));
      list.append(item);
    }
  } catch (error) { list.innerHTML = `<div class="empty">候选记忆读取失败：${escapeHtml(error.message)}</div>`; }
}

function renderStats(graph) {
  $("node-count").textContent = graph.nodes?.length ?? 0;
  $("edge-count").textContent = graph.edges?.length ?? 0;
  $("memory-count").textContent = graph.nodes?.filter((node) => node.kind === "card" || node.kind === "memory").length ?? 0;
  $("event-count").textContent = graph.nodes?.filter((node) => node.kind === "event").length ?? 0;
  $("graph-caption").textContent = state.selectedTask ? state.selectedTask : "全部任务";
  $("page-title").textContent = state.selectedTask ? (state.tasks.find((task) => task.task_id === state.selectedTask)?.title || "记忆关系图") : "记忆关系图";
}

function renderGraph(graph) {
  const container = $("graph-container");
  container.querySelector(".loading")?.remove();
  if (!state.renderer) initScene(container);
  for (const child of [...state.scene.children]) if (child.userData.graphLayer) state.scene.remove(child);
  state.nodeObjects.clear();
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const layer = new THREE.Group(); layer.userData.graphLayer = true; state.scene.add(layer);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const positions = new Map();
  nodes.forEach((node, index) => {
    const ring = Math.max(1, Math.ceil(Math.sqrt(nodes.length)));
    const angle = index * Math.PI * (3 - Math.sqrt(5));
    const radius = 2.2 + (index % ring) * 0.5;
    positions.set(node.id, new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius, ((index % 7) - 3) * .62));
  });
  for (const edge of edges) {
    const a = positions.get(edge.source), b = positions.get(edge.target);
    if (!a || !b) continue;
    const geometry = new THREE.BufferGeometry().setFromPoints([a, b]);
    const material = new THREE.LineBasicMaterial({ color: 0x4b6281, transparent: true, opacity: .46 });
    const line = new THREE.Line(geometry, material); line.userData.graphLayer = true; layer.add(line);
  }
  for (const node of nodes) {
    const radius = node.kind === "card" ? .23 : node.kind === "memory" ? .19 : node.kind === "task" ? .22 : .13;
    const geometry = new THREE.SphereGeometry(radius, 18, 12);
    const material = new THREE.MeshStandardMaterial({ color: colors[node.kind] || 0x94a3b8, emissive: colors[node.kind] || 0x94a3b8, emissiveIntensity: .18, roughness: .52, metalness: .12 });
    const mesh = new THREE.Mesh(geometry, material); mesh.position.copy(positions.get(node.id)); mesh.userData.node = node; mesh.userData.graphLayer = true; layer.add(mesh); state.nodeObjects.set(node.id, mesh);
  }
  $("graph-container").dataset.ready = "true";
  if (state.selectedNode && byId.has(state.selectedNode)) selectNode(state.selectedNode); else clearDetail();
}

function initScene(container) {
  state.scene = new THREE.Scene(); state.scene.background = new THREE.Color(0x080d17);
  state.camera = new THREE.PerspectiveCamera(45, 1, .1, 100); state.camera.position.set(0, 0, 10);
  state.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); state.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)); container.prepend(state.renderer.domElement);
  state.controls = new OrbitControls(state.camera, state.renderer.domElement); state.controls.enableDamping = true; state.controls.dampingFactor = .08; state.controls.minDistance = 3; state.controls.maxDistance = 22;
  state.scene.add(new THREE.AmbientLight(0x9bb8db, 1.7)); const key = new THREE.PointLight(0x55d6e7, 18, 30); key.position.set(3, 4, 7); state.scene.add(key); const fill = new THREE.PointLight(0xa98cff, 12, 25); fill.position.set(-5, -2, 2); state.scene.add(fill);
  const raycaster = new THREE.Raycaster(); const pointer = new THREE.Vector2();
  container.addEventListener("click", (event) => { const rect = container.getBoundingClientRect(); pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1; pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1; raycaster.setFromCamera(pointer, state.camera); const hit = raycaster.intersectObjects([...state.nodeObjects.values()])[0]; if (hit?.object.userData.node) selectNode(hit.object.userData.node.id); });
  const resize = () => { const width = container.clientWidth, height = container.clientHeight; state.camera.aspect = width / height; state.camera.updateProjectionMatrix(); state.renderer.setSize(width, height, false); }; window.addEventListener("resize", resize); resize();
  const animate = () => { state.frame += .01; state.controls.update(); for (const mesh of state.nodeObjects.values()) { const kind = mesh.userData.node.kind; const pulse = (kind === "memory" || kind === "card") ? 1 + Math.sin(state.frame * 2 + mesh.position.x) * .06 : 1; mesh.scale.setScalar(pulse); } state.renderer.render(state.scene, state.camera); requestAnimationFrame(animate); }; animate();
}

async function selectNode(nodeId) {
  const node = state.graph?.nodes?.find((item) => item.id === nodeId); if (!node) return; state.selectedNode = nodeId;
  for (const [id, mesh] of state.nodeObjects) mesh.material.emissiveIntensity = id === nodeId ? .8 : .18;
  document.querySelectorAll(".candidate-item").forEach((item) => item.classList.toggle("selected", item.dataset.node === nodeId));
  document.querySelectorAll(".card-item").forEach((item) => item.classList.toggle("selected", item.dataset.node === nodeId));
  $("detail-kind").textContent = node.kind; const detail = $("detail-content");
  const pairs = Object.entries(node).filter(([key]) => !["id", "kind", "label", "aliases"].includes(key));
  detail.innerHTML = `<h3>${escapeHtml(node.label || node.id)}</h3><div class="detail-grid">${pairs.slice(0, 8).map(([key, value]) => `<div>${escapeHtml(key)}<br><b>${escapeHtml(formatValue(value))}</b></div>`).join("")}</div><pre>${escapeHtml(JSON.stringify(node, null, 2))}</pre>`;
  if (node.kind === "card" && node.card_id) {
    try {
      const full = await requestJson(`/v1/cards/${encodeURIComponent(node.card_id)}`);
      if (state.selectedNode !== nodeId) return;
      const current = full.versions?.find((version) => version.card_version_id === full.current_version_id) || full.versions?.[0];
      const bindings = (current?.bindings || []).map((binding) => `<p><b>${escapeHtml(binding.role)}</b> · ${escapeHtml(binding.path || binding.qualified_symbol || binding.symbol || "未绑定")} <span class="muted">(${escapeHtml(binding.status)})</span></p>`).join("");
      const versions = (full.versions || []).map((version) => `<li>v${version.version_no} · ${escapeHtml(version.statement)} · ${escapeHtml(version.change_reason)}</li>`).join("");
      detail.innerHTML = `<h3>${escapeHtml(current?.statement || full.card_id)}</h3><div class="detail-grid"><div>状态<br><b>${escapeHtml(full.status)}</b></div><div>置信度<br><b>${Math.round(full.confidence * 100)}%</b></div><div>版本<br><b>${full.versions?.length || 0}</b></div><div>关系<br><b>${full.links?.length || 0}</b></div></div><h4>当前代码绑定</h4>${bindings || '<p class="muted">暂无代码绑定</p>'}<h4>版本历史</h4><ul>${versions || '<li class="muted">暂无版本</li>'}</ul><pre>${escapeHtml(JSON.stringify(full, null, 2))}</pre>`;
    } catch (error) { detail.insertAdjacentHTML("beforeend", `<p class="muted">卡片详情读取失败：${escapeHtml(error.message)}</p>`); }
  } else if (node.kind === "memory" && node.candidate_id) {
    try {
      const full = await requestJson(`/v1/memories/${encodeURIComponent(node.candidate_id)}`);
      if (state.selectedNode !== nodeId) return;
      const evidence = (full.evidence || []).map((item) => `<p><b>${escapeHtml(item.event_type)}</b> · ${escapeHtml(item.occurred_at)}<br>${escapeHtml(formatValue(item.payload))}</p>`).join("");
      detail.innerHTML = `<h3>${escapeHtml(full.statement)}</h3><div class="detail-grid"><div>置信度<br><b>${Math.round(full.confidence * 100)}%</b></div><div>生命周期提示<br><b>${escapeHtml(full.lifecycle_hint)}</b></div><div>候选类型<br><b>${escapeHtml(full.kind)}</b></div><div>证据数<br><b>${full.evidence?.length || 0}</b></div></div><p class="muted">${escapeHtml(full.uncertainty)}</p><h4>来源证据</h4>${evidence || '<p class="muted">无可用来源片段</p>'}<pre>${escapeHtml(JSON.stringify(full.bindings, null, 2))}</pre>`;
    } catch (error) { detail.insertAdjacentHTML("beforeend", `<p class="muted">证据详情读取失败：${escapeHtml(error.message)}</p>`); }
  }
}

function clearDetail() { state.selectedNode = null; $("detail-kind").textContent = "未选择"; $("detail-content").innerHTML = '<div class="empty">点击图中的节点查看原始证据、候选记忆和代码绑定。</div>'; }
function formatValue(value) { return typeof value === "string" ? value : JSON.stringify(value); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }

$("task-select").addEventListener("change", async (event) => { state.selectedTask = event.target.value; try { await loadGraph(); } catch (error) { $("connection-status").textContent = error.message; } });
$("refresh-button").addEventListener("click", async () => { try { await loadTasks(); await loadGraph(); } catch (error) { $("connection-status").textContent = error.message; } });
$("search-button").addEventListener("click", async () => {
  const query = $("search-input").value.trim();
  if (!query) return;
  try {
    const cardSuffix = state.selectedTask ? `&task_id=${encodeURIComponent(state.selectedTask)}` : "";
    const cards = await requestJson(`/v1/cards/search?q=${encodeURIComponent(query)}${cardSuffix}`);
    const firstCard = cards.results?.[0];
    if (firstCard) { selectNode(`card:${firstCard.card_id}`); return; }
    const data = await requestJson(`/v1/memories/search?q=${encodeURIComponent(query)}${cardSuffix}`);
    const first = data.results?.[0];
    if (first) selectNode(`candidate:${first.candidate_id}`);
  } catch (error) { $("detail-content").innerHTML = `<div class="empty">搜索失败：${escapeHtml(error.message)}</div>`; }
});
$("search-input").addEventListener("keydown", (event) => { if (event.key === "Enter") $("search-button").click(); });

(async function boot() { try { await loadTasks(); if (state.tasks.length) await loadGraph(); else { $("connection-status").textContent = "数据库暂无任务"; $("graph-container").querySelector(".loading").textContent = "导入历史或事件后刷新图谱"; } } catch (error) { $("connection-status").textContent = `API 错误：${error.message}`; $("graph-container").querySelector(".loading").textContent = "无法连接本地 API，请先运行 codememory serve"; } })();
