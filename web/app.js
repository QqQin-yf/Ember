/* Ember Test Chat — 前端逻辑（只读观察工具，不是 memory editor） */
const $ = (id) => document.getElementById(id);
const messagesEl = $("messages"), memEl = $("memory-panel"), dbgEl = $("debug");
const inputEl = $("input"), sendBtn = $("send"), dlg = $("dlg");

let sessionId = localStorage.getItem("ember_session") || null;

$("provider-note").textContent = "";
$("sess-note").textContent = sessionId ? `session: ${sessionId}` : "";

function log(section, obj) {
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  dbgEl.textContent += `[${time}] ${section}\n` +
    (typeof obj === "string" ? obj : JSON.stringify(obj, null, 1)) + "\n\n";
  dbgEl.scrollTop = dbgEl.scrollHeight;
}

function addMsg(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function badge(mem) {
  const span = document.createElement("span");
  span.className = `badge ${mem.status}`;
  if (mem.status === "created") {
    span.textContent = `Memory: created ${mem.event_id} · ${mem.route_count} routes`;
  } else if (mem.status === "failed") {
    span.textContent = `Memory: failed${mem.error ? " — " + mem.error.slice(0, 80) : ""}`;
  } else {
    const recalled = mem.recalled_count > 0 ? ` · 召回注入 ${mem.recalled_count} 条` : "";
    span.textContent = `Memory: skipped（模型本轮未写记忆）${recalled}`;
  }
  return span;
}

function toolBadge(mem) {
  // 工具调用观察（momo 裁决：AI 要知道自己调用了工具且成功了）
  const span = document.createElement("span");
  if (mem.tool_called === "create_memory_event" && mem.tool_result_in_context) {
    span.className = "badge tool";
    span.textContent = mem.status === "created"
      ? `Tool call: ${mem.tool_called} → success（已写入模型上下文）`
      : `Tool call: ${mem.tool_called} → failed（结果已写入模型上下文）`;
  } else if (mem.writer_called && mem.status !== "skipped") {
    span.className = "badge tool";
    span.textContent = "Tool call: 尝试失败，未产生调用记录";
  } else {
    return null;
  }
  return span;
}

async function refreshEvents() {
  try {
    const res = await fetch("/v1/ember/events");
    const data = await res.json();
    memEl.innerHTML = "";
    if (!data.events.length) {
      memEl.innerHTML = '<div style="color:var(--muted);font-size:12px">暂无 Event。聊一段有明确经历/决定的内容试试。</div>';
      return;
    }
    for (const ev of data.events.slice().reverse()) {
      const card = document.createElement("div");
      card.className = "ev-card";
      card.innerHTML = `<div class="eid">${ev.event_id}</div>
        <div class="esum"></div>
        <div class="kv" style="font-size:11px;color:var(--muted);margin-top:4px">${ev.route_count} routes · ${ev.ingest_time}</div>`;
      card.querySelector(".esum").textContent = ev.summary;
      card.onclick = () => openEvent(ev.event_id);
      memEl.appendChild(card);
    }
  } catch (e) { log("events", String(e)); }
}

async function openEvent(eventId) {
  try {
    const res = await fetch(`/v1/ember/events/${encodeURIComponent(eventId)}`);
    if (!res.ok) return;
    const data = await res.json();
    $("dlg-title").textContent = eventId;
    $("dlg-body").innerHTML = "";
    const ev = data.event;
    const routes = data.recall_routes || [];
    const html = `
      <p><span class="kv">summary:</span> ${esc(ev.summary)}</p>
      <p><span class="kv">raw_ref:</span> ${esc(ev.raw_ref)}</p>
      <p><span class="kv">event_time:</span> ${esc(ev.event_time)}</p>
      <p><span class="kv">participants:</span> ${esc(ev.participants.join(", "))}</p>
      <p><span class="kv">core:</span> ${esc(ev.core)}</p>
      <p><span class="kv">outcome:</span> ${esc(ev.outcome.join("; "))}</p>
      <p><span class="kv">change:</span> ${esc(ev.change || "（待 Sleep 补写）")}</p>
      <p><span class="kv">significance:</span> ${esc(ev.significance)}</p>
      <p style="margin-top:8px"><b>Recall Routes</b></p>
      <pre>${esc(routes.map(r => `${r.cue_type.padEnd(11)} ${r.cue}`).join("\n") || "（无）")}</pre>
      <p style="margin-top:8px"><b>完整 Event JSON</b></p>
      <pre>${esc(JSON.stringify(ev, null, 1))}</pre>`;
    $("dlg-body").innerHTML = html;
    dlg.showModal();
  } catch (e) { log("event-view", String(e)); }
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || sendBtn.disabled) return;
  inputEl.value = "";
  sendBtn.disabled = true;
  addMsg("user", text);
  const thinking = addMsg("assistant", "……");

  log("[Chat] request → provider", { model: "chat", message: text });

  try {
    const res = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [{ role: "user", content: text }],
        ember_session: sessionId || undefined,
        model: $("model-select").value || undefined,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      thinking.textContent = `请求失败：${data?.error?.message || res.status}`;
      log("[Chat] error", data);
      return;
    }
    if (data.ember?.session_id) {
      sessionId = data.ember.session_id;
      localStorage.setItem("ember_session", sessionId);
      $("sess-note").textContent = `session: ${sessionId}`;
    }
    thinking.textContent = data.choices[0].message.content;
    const mem = data.ember?.memory;
    if (mem && mem.status !== "skipped") {
      // 只有模型真的调用了记忆工具才显示（momo：没调用就不显示，调用了再拿出来，附在回复后）
      thinking.appendChild(document.createElement("br"));
      thinking.appendChild(badge(mem));
      if (mem.tool_result_in_context) {
        thinking.appendChild(document.createElement("br"));
        thinking.appendChild(toolBadge(mem));
      }
      log("[Ember 记忆写入]", mem);
    } else if (mem && mem.recalled_count > 0) {
      // 没写记忆但本轮有召回注入：只在 debug 面板留痕，不进对话气泡
      log("[Ember 召回注入]", { recalled_count: mem.recalled_count });
    }
    log("[Chat] response ← provider", `${data.model ?? "?"}: ${data.choices[0].message.content}`);
    refreshEvents();
  } catch (e) {
    thinking.textContent = "网络错误：" + e;
    log("[Chat] exception", String(e));
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

async function manualRecall() {
  const q = $("recall-input").value.trim();
  if (!q) return;
  try {
    const res = await fetch("/v1/ember/recall", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q }),
    });
    const data = await res.json();
    log("[Recall] " + q, data.recall_trace ?? data);
    $("recall-input").value = "";
  } catch (e) { log("[Recall]", String(e)); }
}

sendBtn.onclick = send;
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("recall-btn").onclick = manualRecall;
$("recall-input").addEventListener("keydown", (e) => { if (e.key === "Enter") manualRecall(); });

refreshEvents();
log("[ember]", "Ember Test Chat 就绪。Memory 面板只读：观察 Event 与 Routes，不能编辑。");
