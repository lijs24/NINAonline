/* ============================================================================
 * nina-theme.js — 星图册(Celestial Atlas)主题 + 共享顶栏 + 状态行 + API/WS 客户端
 *
 * 全站唯一的视觉与数据契约来源。任何页面:
 *   <header id="app-topbar"></header>
 *   <footer id="app-status"></footer>      (可选, 不写也会自动注入)
 *   <script src="/nina-theme.js"></script>
 *
 * 设计宪法(勿违背 —— 让页面像"一页星图图版", 不像"软件后台"):
 *   靛蓝夜空底 #0a0e1a / 羊皮纸白字 #e9e2d0 / 金 #c9a227 为唯一强调色;
 *   全站衬线; 全站无卡片(只用发丝细线分区); 唯一圆角是 999px 胶囊;
 *   反馈写进页底状态行, 不弹 toast/modal。
 * ========================================================================== */
(() => {
  if (window.__opsTheme) return;
  window.__opsTheme = true;

  // -- 设计令牌 ------------------------------------------------------------ //
  const CSS = `
:root{
  --sky:#0a0e1a; --sky-2:#0c1120; --ink:#e9e2d0; --ink-dim:#8b91a5;
  --gold:#c9a227; --gold-soft:#a8861f; --celestial:#9db4d0;
  --hair:rgba(139,145,165,.28); --hair-soft:rgba(139,145,165,.16);
  --ok:#7d9b6a; --warn:#c9a227; --err:#b5675b;
  --serif:"Noto Serif SC","Source Han Serif SC","Songti SC",STSong,"SimSun",Georgia,serif;
}
@font-face{font-family:"Noto Serif SC";font-weight:400;font-display:swap;
  src:local("Noto Serif SC"),local("Source Han Serif SC"),url("/fonts/NotoSerifSC-Regular.woff2") format("woff2");}
*{box-sizing:border-box;}
html,body{margin:0;background:var(--sky);color:var(--ink);font-family:var(--serif);
  font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased;}
body{padding-top:52px;padding-bottom:34px;min-height:100vh;}
a{color:var(--ink);text-decoration:none;}
h1,h2,h3{font-weight:400;letter-spacing:.5px;}
::selection{background:rgba(201,162,39,.28);}

/* 顶栏 52px, 无卡片, 仅底部发丝线 */
.ops-topbar{position:fixed;top:0;left:0;right:0;height:52px;z-index:80;
  display:flex;align-items:center;gap:0;background:rgba(10,14,26,.94);
  backdrop-filter:blur(6px);border-bottom:1px solid var(--hair);
  font-family:var(--serif);}
.ops-brand{padding:0 20px 0 22px;font-size:17px;letter-spacing:3px;color:var(--ink);
  white-space:nowrap;}
.ops-brand b{color:var(--gold);font-weight:400;}
.ops-nav{display:flex;align-items:center;gap:2px;flex:1;min-width:0;overflow-x:auto;
  scrollbar-width:none;height:100%;}
.ops-nav::-webkit-scrollbar{display:none;}
.ops-nav a{padding:4px 13px;color:var(--ink-dim);font-size:14.5px;white-space:nowrap;
  border-bottom:2px solid transparent;height:52px;display:flex;align-items:center;
  transition:color .25s;}
.ops-nav a:hover{color:var(--ink);}
.ops-nav a.active{color:var(--gold);border-bottom-color:var(--gold);}
.ops-nav a small{font-style:italic;color:var(--ink-dim);margin-left:5px;font-size:11px;}
.ops-actions{display:flex;align-items:center;gap:14px;padding:0 18px;white-space:nowrap;}
#ops-clock{color:var(--ink-dim);font-size:13px;font-variant-numeric:tabular-nums;}
.ops-ctrl{font-size:12.5px;padding:3px 12px;border:1px solid var(--hair);border-radius:999px;
  color:var(--ink-dim);}
.ops-ctrl.self{color:var(--gold);border-color:var(--gold);}
.ops-ctrl.busy{color:var(--err);border-color:var(--err);}
.ops-role{background:transparent;color:var(--ink);border:1px solid var(--hair);
  border-radius:999px;font-family:var(--serif);font-size:13px;padding:3px 10px;cursor:pointer;}
.ops-role:focus{outline:none;border-color:var(--gold);}

/* 状态行: [OK]一句话 ……… 时间戳 */
.ops-status{position:fixed;bottom:0;left:0;right:0;height:34px;z-index:80;
  display:flex;align-items:center;gap:12px;padding:0 20px;
  background:rgba(10,14,26,.94);border-top:1px solid var(--hair);
  font-size:13px;color:var(--ink-dim);}
.ops-status .tag{font-style:normal;letter-spacing:1px;}
.ops-status.ok .tag{color:var(--ok);} .ops-status.warn .tag{color:var(--warn);}
.ops-status.err .tag{color:var(--err);}
.ops-status .msg{color:var(--ink);} .ops-status .ts{margin-left:auto;color:var(--ink-dim);
  font-variant-numeric:tabular-nums;}

/* 通用排版原语(页面可用, 全站无卡片) */
.wrap{max-width:1280px;margin:0 auto;padding:30px 26px;}
.col-rail{display:grid;grid-template-columns:1fr 280px;gap:40px;}
.rail{border-left:1px solid var(--gold);padding-left:22px;}
.sec-h{display:flex;align-items:baseline;gap:12px;margin:34px 0 16px;
  border-bottom:1px solid var(--hair);padding-bottom:8px;}
.sec-h .rn{color:var(--gold);font-size:15px;letter-spacing:2px;}
.sec-h h2{font-size:19px;margin:0;}
.sec-h .en{font-style:italic;color:var(--ink-dim);font-size:12px;}
.kv{display:flex;justify-content:space-between;gap:18px;padding:7px 0;
  border-bottom:1px solid var(--hair-soft);}
.kv .k{color:var(--ink-dim);} .kv .k em{font-style:italic;font-size:11px;margin-left:4px;}
.kv .v{font-variant-numeric:tabular-nums;}
.v.gold{color:var(--gold);} .v.cel{color:var(--celestial);}
.cap{display:inline-flex;align-items:center;gap:6px;background:transparent;color:var(--ink);
  border:1px solid var(--hair);border-radius:999px;padding:6px 16px;font-family:var(--serif);
  font-size:14px;cursor:pointer;transition:border-color .25s,color .25s;}
.cap:hover{border-color:var(--gold);color:var(--gold);}
.cap.solid{border-color:var(--gold);color:var(--gold);}
.cap[disabled]{opacity:.4;cursor:not-allowed;}
.cap.danger:hover{border-color:var(--err);color:var(--err);}
input.fld,select.fld{background:transparent;color:var(--ink);border:1px solid var(--hair);
  border-radius:999px;font-family:var(--serif);font-size:14px;padding:5px 13px;
  font-variant-numeric:tabular-nums;}
input.fld:focus,select.fld:focus{outline:none;border-color:var(--gold);}
.muted{color:var(--ink-dim);} .em{font-style:italic;}
.sup{font-size:.7em;vertical-align:.35em;color:var(--ink-dim);}
.dot{display:inline-block;width:7px;height:7px;border-radius:999px;background:var(--ink-dim);}
.dot.on{background:var(--gold);} .dot.warn{background:var(--err);}
.fade{animation:opsFade .45s ease both;}
@keyframes opsFade{from{opacity:0;transform:translateY(4px);}to{opacity:1;}}
@media (prefers-reduced-motion:reduce){.fade{animation:none;}}
@media(max-width:880px){.col-rail{grid-template-columns:1fr;}.rail{border-left:none;
  border-top:1px solid var(--gold);padding-left:0;padding-top:18px;}}
`;
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  // -- 会话身份 ------------------------------------------------------------ //
  const SID_KEY = "asiair-ops:session-id";
  let sid = null;
  try { sid = localStorage.getItem(SID_KEY); } catch (e) {}
  if (!sid) {
    sid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : `session-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
    try { localStorage.setItem(SID_KEY, sid); } catch (e) {}
  }

  // -- 导航(从 NINA 能力面推导的页面集) ----------------------------------- //
  const NAV = [
    ["总览", "/overview", "Overview"], ["设备", "/equipment", "Equipment"],
    ["相机", "/camera", "Imaging"], ["赤道仪", "/mount", "Mount"],
    ["对焦", "/focuser", "Focuser"], ["导星", "/guider", "Guiding"],
    ["滤镜", "/filterwheel", "Filters"], ["序列", "/sequence", "Sequencer"],
    ["构图", "/framing", "Framing"], ["辅助", "/aux", "Auxiliary"],
    ["图库", "/library", "Library"],
  ];
  const here = location.pathname.replace(/\/$/, "") || "/overview";
  const isActive = (p) => here === p || (p === "/overview" && here === "/");

  // -- API 客户端 ---------------------------------------------------------- //
  const Ops = window.Ops = {
    sessionId: sid,
    isController: false,
    readonly: false,
    controlState: null,
    _evtHandlers: [],
    _ctrlHandlers: [],

    async api(path, opts = {}) {
      const r = await fetch(path, { cache: "no-store", ...opts });
      const ct = r.headers.get("content-type") || "";
      const data = ct.includes("json") ? await r.json() : await r.text();
      return { ok: r.ok, status: r.status, data };
    },

    async get(path) { return (await Ops.api(path)).data; },

    // 写动作: POST /api/{domain}/action, 自动带 session_id
    async action(domain, action, params = {}) {
      const r = await Ops.api(`/api/${domain}/action`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, params, session_id: sid }),
      });
      const d = r.data || {};
      if (d.locked) Ops.status("warn", d.error || "监控模式, 无法操作");
      else if (d.ok === false) Ops.status("err", d.error || "操作失败");
      return d;
    },

    async post(path, body) {
      const r = await Ops.api(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, session_id: sid }),
      });
      return r.data;
    },

    // 页底状态行 —— 所有反馈走这里, 不弹窗
    status(level, msg) {
      const el = document.getElementById("app-status");
      if (!el) return;
      const tag = { ok: "[OK]", warn: "[WARN]", err: "[ERR]" }[level] || "[··]";
      el.className = "ops-status " + (level || "");
      const t = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      el.innerHTML = `<span class="tag">${tag}</span><span class="msg"></span>`
        + `<span class="ts">${t.getFullYear()}/${pad(t.getMonth() + 1)}/${pad(t.getDate())} `
        + `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}</span>`;
      el.querySelector(".msg").textContent = msg || "";
    },

    onEvent(cb) { Ops._evtHandlers.push(cb); },
    onControl(cb) { Ops._ctrlHandlers.push(cb); },
    requireControl() {
      if (Ops.readonly) { Ops.status("warn", "只读监控模式 —— 已禁用对设备的操作"); return false; }
      if (!Ops.isController) { Ops.status("warn", "请先在右上角切到「主控」再操作"); return false; }
      return true;
    },

    // 坐标格式化(雕版上标样式)
    fmtRA(h) {
      h = ((h % 24) + 24) % 24; const hh = Math.floor(h), m = (h - hh) * 60,
        mm = Math.floor(m), ss = Math.round((m - mm) * 60);
      return `${String(hh).padStart(2, "0")}ʰ${String(mm).padStart(2, "0")}ᵐ${String(ss).padStart(2, "0")}ˢ`;
    },
    fmtDec(d) {
      const s = d < 0 ? "−" : "+"; d = Math.abs(d); const dd = Math.floor(d),
        m = (d - dd) * 60, mm = Math.floor(m), ss = Math.round((m - mm) * 60);
      return `${s}${String(dd).padStart(2, "0")}°${String(mm).padStart(2, "0")}′${String(ss).padStart(2, "0")}″`;
    },
  };

  // -- WebSocket 事件流(自动重连; 页面隐藏时不强制断, 仅暂停心跳) ---------- //
  let ws = null, wsTimer = null;
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/socket`);
    ws.onmessage = (e) => {
      let evt; try { evt = JSON.parse(e.data); } catch { return; }
      Ops._evtHandlers.forEach((cb) => { try { cb(evt); } catch (x) {} });
    };
    ws.onclose = () => { clearTimeout(wsTimer); wsTimer = setTimeout(connectWS, 3000); };
    ws.onerror = () => { try { ws.close(); } catch (x) {} };
  }
  connectWS();

  // -- 顶栏渲染 ------------------------------------------------------------ //
  function mountTopbar() {
    const host = document.getElementById("app-topbar");
    if (!host) return;
    host.className = "ops-topbar";
    host.innerHTML =
      `<div class="ops-brand"><b>星枢</b> · 远程台</div>`
      + `<nav class="ops-nav">${NAV.map(([t, p, en]) =>
        `<a href="${p}" class="${isActive(p) ? "active" : ""}">${t}<small>${en}</small></a>`).join("")}</nav>`
      + `<div class="ops-actions"><span id="ops-clock">--:--:--</span>`
      + `<span id="ops-ctrl" class="ops-ctrl">主控空闲</span>`
      + `<select id="ops-role" class="ops-role" aria-label="协作模式">`
      + `<option value="monitor">监控</option><option value="controller">主控</option></select></div>`;

    const clk = host.querySelector("#ops-clock");
    const pad = (n) => String(n).padStart(2, "0");
    setInterval(() => {
      const d = new Date();
      clk.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }, 1000);

    const ind = host.querySelector("#ops-ctrl");
    const sel = host.querySelector("#ops-role");
    const render = (p) => {
      Ops.controlState = p;
      const holder = p && p.controller, self = !!(p && p.held_by_self);
      Ops.isController = self;
      ind.className = "ops-ctrl" + (self ? " self" : holder ? " busy" : "");
      ind.textContent = !holder ? "主控空闲" : self ? "当前主控"
        : `主控中 · ${holder.display_name || holder.client_ip || "其他"}`;
      sel.value = self ? "controller" : "monitor";
      Ops._ctrlHandlers.forEach((cb) => { try { cb(p); } catch (x) {} });
    };
    const poll = async () => {
      try {
        const d = await Ops.get(`/api/control-role?session_id=${encodeURIComponent(sid)}`);
        if (d && d.ok) render(d);
      } catch (e) {}
    };
    sel.addEventListener("change", async () => {
      const d = await Ops.post("/api/control-role", { role: sel.value, session_label: "web" });
      if (d && d.ok) { render(d); Ops.status("ok", sel.value === "controller" ? "已取得主控权" : "已切回监控"); }
    });
    poll();
    setInterval(() => { if (!document.hidden) poll(); }, 10000);
    // 主控者心跳续租
    setInterval(() => { if (!document.hidden && Ops.isController)
      Ops.post("/api/control-role", { role: "controller", session_label: "web" }); }, 20000);
  }

  function mountStatus() {
    if (document.getElementById("app-status")) return;
    const f = document.createElement("footer");
    f.id = "app-status";
    f.className = "ops-status";
    document.body.appendChild(f);
  }

  // -- 启动: /api/status 只读门禁 ----------------------------------------- //
  function boot() {
    mountStatus();
    mountTopbar();
    Ops.api("/api/status").then(({ ok, data }) => {
      if (ok && data && data.ok) {
        Ops.readonly = !!data.readonly;
        if (Ops.readonly) {
          const ind = document.getElementById("ops-ctrl");
          if (ind) { ind.textContent = "只读监控"; ind.className = "ops-ctrl busy"; }
          const role = document.getElementById("ops-role");
          if (role) { role.disabled = true; role.title = "只读模式不可主控"; role.style.display = "none"; }
        }
        const mode = data.provider === "sim" ? "模拟引擎" : "NINA 实机";
        Ops.status(Ops.readonly ? "warn" : "ok",
          `已连接后端 · ${mode}${Ops.readonly ? " · 只读监控" : ""} · `
          + `${data.connected_devices.length}/${data.device_count} 设备在线`);
        window.OPS_BOOT = data;
        document.dispatchEvent(new CustomEvent("ops:ready", { detail: data }));
      } else {
        Ops.status("err", "后端不可达 —— 请确认 星枢后端服务已启动");
      }
    }).catch(() => Ops.status("err", "后端不可达"));
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
