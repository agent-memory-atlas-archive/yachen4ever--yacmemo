/* yacmemo WebUI — vanilla JS single page app */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  users: [],
  user: null,
  notes: [],
  current: null,     // {path, title, content}
  editing: false,
  creating: null,    // {title} while creating a new note
};

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch { /* ignore */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 3500);
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ` +
         `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- tabs

for (const btn of $("tabs").querySelectorAll("button")) {
  btn.onclick = () => {
    for (const b of $("tabs").querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    for (const sec of document.querySelectorAll("main > section")) sec.hidden = true;
    $(`tab-${btn.dataset.tab}`).hidden = false;
    if (btn.dataset.tab === "health") loadHealth();
    if (btn.dataset.tab === "usage") loadUsage();
  };
}

// ---------------------------------------------------------------- users

async function loadUsers() {
  const data = await api("/api/overview");
  state.users = data.users.map((u) => u.id);
  const sel = $("user-select");
  sel.innerHTML = state.users
    .map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("");
  if (!state.user || !state.users.includes(state.user)) {
    state.user = state.users[0] || null;
  }
  if (state.user) sel.value = state.user;
  return data;
}

$("user-select").onchange = () => {
  state.user = $("user-select").value;
  state.current = null;
  loadNotes();
};

// ---------------------------------------------------------------- notes

async function loadNotes() {
  if (!state.user) return;
  const filter = $("note-filter").value.trim().toLowerCase();
  const data = await api(`/api/${state.user}/notes`);
  state.notes = data.notes.filter((n) =>
    !filter || n.path.toLowerCase().includes(filter));
  $("note-list").innerHTML = renderTree(buildTree(state.notes));
  for (const li of $("note-list").querySelectorAll("li[data-path]")) {
    li.onclick = () => openNote(li.dataset.path);
  }
}

function buildTree(notes) {
  // {dirs: {name: node}, files: [note]}
  const root = { dirs: {}, files: [] };
  for (const n of notes) {
    let node = root;
    const parts = n.path.split("/");
    for (let i = 0; i < parts.length - 1; i++) {
      node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [] };
      node = node.dirs[parts[i]];
    }
    node.files.push(n);
  }
  return root;
}

function countFiles(node) {
  let n = node.files.length;
  for (const sub of Object.values(node.dirs)) n += countFiles(sub);
  return n;
}

function renderTree(node) {
  // Always returns a <ul>; folders nest as li > details > ul so every level
  // is a valid list and indentation accumulates naturally.
  const dirs = Object.entries(node.dirs).sort(([a], [b]) => a.localeCompare(b));
  const files = [...node.files].sort((a, b) => a.title.localeCompare(b.title, "zh"));
  let inner = "";
  for (const [name, sub] of dirs) {
    inner += `<li class="folder"><details open>
      <summary>📁 ${esc(name)} <span class="m">(${countFiles(sub)})</span></summary>
      ${renderTree(sub)}
    </details></li>`;
  }
  inner += files.map((n) => `
    <li data-path="${esc(n.path)}" class="${state.current && state.current.path === n.path ? "active" : ""}">
      <span class="t" title="${esc(n.path)}">${esc(n.title)}</span>
      <span class="m">${fmtTime(n.mtime)}</span>
    </li>`).join("");
  return `<ul class="tree-children">${inner}</ul>`;
}

$("note-filter").oninput = () => loadNotes();

async function openNote(path) {
  try {
    const data = await api(`/api/${state.user}/note?path=${encodeURIComponent(path)}`);
    state.current = data;
    state.editing = false;
    state.creating = null;
    renderNote();
  } catch (e) { toast(e.message, true); }
}

function renderNote() {
  const cur = state.current;
  $("note-empty").hidden = !!cur;
  $("note-toolbar").hidden = !cur && !state.creating;
  $("note-path").textContent = cur ? `${cur.path}` : `新建：${state.creating?.title ?? ""}`;
  $("btn-edit").hidden = state.editing || !cur;
  $("btn-save").hidden = !state.editing;
  $("btn-cancel").hidden = !state.editing;
  $("btn-delete").hidden = state.editing || !cur;
  $("force-wrap").hidden = !state.creating;
  const renderOn = $("chk-render").checked && !state.editing;

  if (state.editing) {
    $("note-rendered").hidden = true;
    $("note-editor").hidden = false;
    if ($("note-editor").value === "" || $("note-editor").dataset.path !== (cur?.path ?? "")) {
      $("note-editor").value = cur ? cur.content : "";
      $("note-editor").dataset.path = cur?.path ?? "__new__";
    }
  } else {
    $("note-editor").hidden = true;
    $("note-rendered").hidden = !cur || !renderOn;
    if (cur) {
      $("note-rendered").innerHTML = renderOn
        ? marked.parse(cur.content)
        : `<pre>${esc(cur.content)}</pre>`;
    }
  }
  for (const li of $("note-list").querySelectorAll("li[data-path]")) {
    li.classList.toggle("active", !!cur && li.dataset.path === cur.path);
  }
}

$("chk-render").onchange = renderNote;
$("btn-edit").onclick = () => { state.editing = true; renderNote(); };
$("btn-cancel").onclick = () => {
  state.editing = false;
  if (!state.current && state.creating) { state.creating = null; $("note-editor").value = ""; }
  renderNote();
};

$("btn-save").onclick = async () => {
  const content = $("note-editor").value;
  try {
    if (state.creating) {
      const force = $("force-chk").checked;
      const data = await api(`/api/${state.user}/notes`, {
        method: "POST",
        body: JSON.stringify({ title: state.creating.title, content, force }),
      });
      toast(`已创建：${data.path}`);
      state.creating = null;
      $("force-chk").checked = false;
      await loadNotes();
      await openNote(data.path);
    } else {
      const data = await api(`/api/${state.user}/note`, {
        method: "PUT",
        body: JSON.stringify({ path: state.current.path, content }),
      });
      toast("已保存并重建索引");
      state.current.title = data.title;
      state.editing = false;
      state.current.content = content;
      await loadNotes();
      renderNote();
    }
  } catch (e) { toast(e.message, true); }
};

$("btn-delete").onclick = async () => {
  if (!state.current) return;
  if (!confirm(`确定删除《${state.current.title}》？（索引同步清理，git 里仍可找回）`)) return;
  try {
    await api(`/api/${state.user}/note?path=${encodeURIComponent(state.current.path)}`,
              { method: "DELETE" });
    toast("已删除");
    state.current = null;
    await loadNotes();
    renderNote();
  } catch (e) { toast(e.message, true); }
};

$("btn-new").onclick = () => {
  const title = prompt("新笔记标题（主题名，可含目录前缀如 projects/xx）：");
  if (!title || !title.trim()) return;
  state.creating = { title: title.trim() };
  state.current = null;
  state.editing = true;
  $("note-editor").value = `# ${title.trim()}\n\n`;
  $("note-editor").dataset.path = "__new__";
  renderNote();
};

// ---------------------------------------------------------------- search

$("btn-search").onclick = async () => {
  const q = $("search-q").value.trim();
  if (!q) return;
  $("search-results").innerHTML = `<div class="muted">搜索中…</div>`;
  try {
    const data = await api(`/api/${state.user}/search?q=${encodeURIComponent(q)}` +
                           `&kind=${$("search-kind").value}`);
    if (!data.results.length) {
      $("search-results").innerHTML = `<div class="muted center">未找到相关笔记</div>`;
      return;
    }
    $("search-results").innerHTML = data.results.map((r, i) => `
      <div class="hit" data-path="${esc(r.path)}">
        <div>${i + 1}. <b>${esc(r.title)}</b>
             <span class="muted">(score ${r.score?.toFixed?.(4) ?? r.score}, ${(r.channels || []).join("+")})</span></div>
        <div class="path">${esc(r.path)}</div>
        ${(r.warnings || []).map((w) => `<div class="warnline">⚠ ${esc(w)}</div>`).join("")}
      </div>`).join("");
    for (const el of $("search-results").querySelectorAll(".hit")) {
      el.onclick = () => {
        document.querySelector('#tabs [data-tab="notes"]').click();
        openNote(el.dataset.path);
      };
    }
  } catch (e) {
    $("search-results").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
};
$("search-q").onkeydown = (e) => { if (e.key === "Enter") $("btn-search").click(); };

// ---------------------------------------------------------------- audit

$("btn-audit-run").onclick = async () => {
  $("audit-status").textContent = "运行中…";
  try {
    const data = await api(`/api/${state.user}/audit`, { method: "POST" });
    renderAudit(data.audit);
    $("audit-status").textContent = "";
  } catch (e) {
    $("audit-status").textContent = e.message;
  }
};

$("btn-reindex").onclick = async () => {
  if (!confirm("全量重建索引？将清除并重建该用户的 FTS/向量/撞车记录（笔记文件不受影响）。")) return;
  $("audit-status").textContent = "重建中…";
  try {
    const data = await api(`/api/${state.user}/reindex`, { method: "POST" });
    toast(`重建完成：${data.indexed} 篇${data.failed?.length ? "，失败 " + data.failed.length : ""}`);
    $("btn-audit-run").click();
  } catch (e) {
    $("audit-status").textContent = e.message;
  }
};

function noteTitleOf(path) {
  const n = state.notes.find((x) => x.path === path);
  return n ? n.title : path.split("/").pop().replace(/\.md$/, "");
}

function jumpToNote(path) {
  document.querySelector('#tabs [data-tab="notes"]').click();
  openNote(path);
}

const COLLISION_GUIDE = `
<div class="section muted" style="font-size:13px">
  撞车 = 两篇笔记里出现了同一（或极相似）的事实行。点「打开」看两篇的关系，再裁决：
  ① <b>两篇是同一主题的两份拷贝</b> → 把有价值的内容并进保留篇、删除另一篇，然后点「✓ 已处理」；
  ② <b>两篇主题不同，只是恰好都有这条事实</b>（如同一功能在两个项目里各有一条）→ 点「忽略」；
  ③ <b>一篇是另一篇的旧版本</b> → 把新内容并入保留篇，点「✓ 已处理」。
</div>`;

function renderAudit(a) {
  const sec = (title, items, render) =>
    `<h3>${title}（${items.length}）</h3>` +
    (items.length ? items.map(render).join("") : `<div class="muted">无</div>`);

  const openBtn = (p, label = "打开") =>
    `<button onclick="jumpToNote(${JSON.stringify(p).replace(/"/g, "&quot;")})">${label}</button>`;

  const groups = {};
  for (const c of a.collisions) {
    const key = [c.a_path, c.b_path].sort().join("↔");
    (groups[key] = groups[key] || { a: c.a_path, b: c.b_path, rows: [] }).rows.push(c);
  }

  $("audit-results").innerHTML =
    sec("新发现文件（已建立索引）", a.added, (p) => `
      <div class="section pair"><div class="texts">${esc(p)}</div>${openBtn(p)}</div>`) +
    sec("外部修改（已自动重建索引）", a.resynced, (p) => `
      <div class="section pair"><div class="texts">${esc(p)}</div>${openBtn(p)}</div>`) +
    sec("外部删除（已清理索引）", a.missing, (p) => `<div class="section">${esc(p)}</div>`) +
    sec("标题重复", a.title_duplicates, (c) => `
      <div class="section pair"><div class="texts">
        <div>[[${esc(c.a_title)}]] ↔ [[${esc(c.b_title)}]] (score ${c.score})</div>
        <div class="small">${esc(c.a_path)} · ${esc(c.b_path)} — 建议合并为一篇</div>
      </div>${openBtn(c.a_path, "打开 A")}${openBtn(c.b_path, "打开 B")}</div>`) +
    `<h3>语义撞车（${a.collisions.length} 处，按笔记对分组）</h3>` + COLLISION_GUIDE +
    (Object.values(groups).map((g) => {
      const ids = JSON.stringify(g.rows.map((r) => r.id)).replace(/"/g, "&quot;");
      return `
      <div class="section pair">
        <div class="texts">
          <div><b>A</b>：《${esc(noteTitleOf(g.a))}》<span class="small"> ${esc(g.a)}</span></div>
          <div><b>B</b>：《${esc(noteTitleOf(g.b))}》<span class="small"> ${esc(g.b)}</span></div>
          <div class="small" style="margin-top:6px">${g.rows.map((r) =>
            `<div>score ${r.score} — A:「${esc(r.a_text)}」 / B:「${esc(r.b_text)}」</div>`).join("")}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px">
          ${openBtn(g.a, "打开 A")}${openBtn(g.b, "打开 B")}
          <button data-resolve="${esc(ids)}" data-st="resolved">✓ 已处理</button>
          <button data-resolve="${esc(ids)}" data-st="dismissed">忽略</button>
        </div>
      </div>`;
    }).join("") || `<div class="muted" style="margin-bottom:10px">无</div>`) +
    sec("悬空链接", a.dangling_links, (d) => `
      <div class="section pair"><div class="texts">${esc(d.path)} → [[${esc(d.link)}]]</div>${openBtn(d.path)}</div>`) +
    `<h3>守卫统计</h3><div class="section">
      拒绝 ${a.guard_stats.refused} 次 · force 越过 ${a.guard_stats.forced} 次</div>
      <button id="btn-reindex2" style="margin-top:8px">全量重建索引</button>`;

  const r2 = $("btn-reindex2");
  if (r2) r2.onclick = () => $("btn-reindex").click();

  for (const btn of $("audit-results").querySelectorAll("button[data-resolve]")) {
    btn.onclick = async () => {
      const ids = JSON.parse(btn.dataset.resolve);
      try {
        for (const id of ids) {
          await api(`/api/${state.user}/collision`, {
            method: "POST",
            body: JSON.stringify({ id, status: btn.dataset.st }),
          });
        }
        toast(btn.dataset.st === "resolved" ? "已标记处理完成" : "已忽略");
        $("btn-audit-run").click();
      } catch (e) { toast(e.message, true); }
    };
  }
}

// ---------------------------------------------------------------- usage

const TOOL_NAMES = ["memory_search", "memory_read", "memory_write", "memory_edit",
  "memory_edit_section", "memory_move", "memory_audit", "memory_list"];

async function loadUsage() {
  try {
    const tool = $("usage-tool").value;
    const [recent, clients, days] = await Promise.all([
      api(`/api/usage?limit=100${tool ? `&tool=${tool}` : ""}`),
      api("/api/usage/clients"),
      api("/api/usage/days"),
    ]);

    $("usage-cards").innerHTML = `
      <div class="card"><div class="k">近 14 天调用</div>
        <div class="v">${days.rows.reduce((s, d) => s + d.calls, 0)}</div></div>
      <div class="card"><div class="k">错误</div>
        <div class="v ${days.rows.reduce((s, d) => s + d.errors, 0) ? "warn" : ""}">
          ${days.rows.reduce((s, d) => s + d.errors, 0)}</div></div>
      <div class="card"><div class="k">客户端</div>
        <div class="v">${clients.rows.length}</div></div>`;

    $("usage-tool").innerHTML = `<option value="">全部工具</option>` +
      TOOL_NAMES.map((t) => `<option ${t === tool ? "selected" : ""}>${t}</option>`).join("");

    $("usage-table").innerHTML = `
      <tr><th>时间</th><th>用户</th><th>工具</th><th>内容</th><th>客户端</th><th>IP</th><th class="num">耗时</th></tr>` +
      (recent.rows.map((r) => `
        <tr>
          <td>${esc(r.ts.replace("T", " "))}</td>
          <td>${esc(r.user_id)}</td>
          <td>${esc(r.tool)}</td>
          <td>${esc(r.summary)}${r.ok ? "" : ` <span class="err">✗ ${esc(r.error)}</span>`}</td>
          <td class="muted">${esc(r.client)}</td>
          <td class="muted">${esc(r.ip)}</td>
          <td class="num">${r.duration_ms}ms</td>
        </tr>`).join("") || `<tr><td colspan="7" class="muted center">暂无调用记录</td></tr>`);

    // 客户端明细放健康页复用
    window._clients = clients.rows;
    window._days = days.rows;
  } catch (e) { toast(e.message, true); }
}
$("btn-usage-refresh").onclick = loadUsage;

// ---------------------------------------------------------------- health

async function loadHealth() {
  try {
    const [overview, clients, days] = await Promise.all([
      api("/api/overview"), api("/api/usage/clients"), api("/api/usage/days")]);
    const emb = overview.embedding;
    $("health-view").innerHTML = `
      <div class="cards">
        <div class="card"><div class="k">embedding</div>
          <div class="v ${emb.configured ? "" : "bad"}" style="font-size:14px">
            ${emb.configured ? esc(emb.model) : "未配置（FTS-only）"}</div>
          <div class="k">${emb.configured ? emb.dimensions + " 维" : "语义检索/撞车检测关闭"}</div></div>
        ${overview.users.map((u) => `
          <div class="card"><div class="k">${esc(u.id)}</div>
            <div class="v">${u.note_count}</div>
            <div class="k">笔记 · 撞车 ${u.open_collisions} · 守卫 ${u.guard.refused}/${u.guard.forced}</div></div>`).join("")}
      </div>
      <h3 style="font-size:14px">客户端</h3>
      <table class="grid">
        <tr><th>客户端 (UA)</th><th>IP</th><th class="num">调用</th><th>最近</th></tr>
        ${clients.rows.map((c) => `
          <tr><td>${esc(c.client || "unknown")}</td><td>${esc(c.ip)}</td>
              <td class="num">${c.calls}</td><td>${esc(c.last_seen.replace("T", " "))}</td></tr>`).join("")
          || `<tr><td colspan="4" class="muted center">暂无记录</td></tr>`}
      </table>
      <h3 style="font-size:14px;margin-top:14px">近 14 天</h3>
      <table class="grid">
        <tr><th>日期</th><th class="num">调用</th><th class="num">错误</th></tr>
        ${days.rows.map((d) => `<tr><td>${esc(d.day)}</td>
          <td class="num">${d.calls}</td>
          <td class="num ${d.errors ? "err" : ""}">${d.errors}</td></tr>`).join("")
          || `<tr><td colspan="3" class="muted center">暂无记录</td></tr>`}
      </table>`;
  } catch (e) {
    $("health-view").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------- boot

(async () => {
  try {
    await loadUsers();
    await loadNotes();
  } catch (e) { toast(e.message, true); }
})();
