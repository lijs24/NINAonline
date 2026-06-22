/* ============================================================================
 * nina-theme.js — 星图册主题外壳 + 客户端路由 + 状态行 + API/WS 客户端
 *
 * 外壳(顶栏/状态行/主题/WebSocket/连接)只初始化一次并常驻;
 * 点顶栏导航时,不整页重载,而是 fetch 目标页内容就地替换(带缓存+预取),
 * 切页瞬时、无重建、无闪。出错则自动回退到整页跳转(最坏=旧行为,不会弄坏)。
 *
 * 页面契约不变:每页 <header id="app-topbar"> + <main class="wrap view-pending">
 *   + 各自内联 <style>/<script> + <link rel="stylesheet" href="/nina-theme.css">。
 * ========================================================================== */
(() => {
  if (window.__opsTheme) return;
  window.__opsTheme = true;

  // ---- 生命周期跟踪:页面脚本注册的定时器 / ops:ready 监听,切页时清理 ----
  // theme.js 在各页内联脚本之前执行,因此装好这两个拦截器后,连首屏页脚本也被纳入跟踪。
  const realSetInterval = window.setInterval.bind(window);
  const realAddEvent = document.addEventListener.bind(document);
  const Page = { intervals: [], readyHandlers: [] };
  window.setInterval = function (fn, ms, ...rest) {
    const id = realSetInterval(fn, ms, ...rest);
    Page.intervals.push(id);
    return id;
  };
  document.addEventListener = function (type, fn, opt) {
    if (type === "ops:ready") Page.readyHandlers.push(fn);
    return realAddEvent(type, fn, opt);
  };
  function teardownPage() {
    Page.intervals.forEach((id) => clearInterval(id));
    Page.intervals = [];
    Page.readyHandlers.forEach((fn) => document.removeEventListener("ops:ready", fn));
    Page.readyHandlers = [];
    Ops._evtHandlers.length = 0;     // 页面通过 Ops.onEvent 订阅的 WS 处理器
    Ops._ctrlHandlers.length = 0;    // 页面通过 Ops.onControl 订阅(顶栏自身不依赖)
  }

  // -- 会话身份 ------------------------------------------------------------ //
  const SID_KEY = "asiair-ops:session-id";
  let sid = null;
  try { sid = localStorage.getItem(SID_KEY); } catch (e) {}
  if (!sid) {
    sid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : `session-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
    try { localStorage.setItem(SID_KEY, sid); } catch (e) {}
  }

  // -- 导航(NINA 能力面推导的页面集) ------------------------------------- //
  const NAV = [
    ["总览", "/overview", "Overview"], ["设备", "/equipment", "Equipment"],
    ["相机", "/camera", "Imaging"], ["赤道仪", "/mount", "Mount"],
    ["对焦", "/focuser", "Focuser"], ["导星", "/guider", "Guiding"],
    ["滤镜", "/filterwheel", "Filters"], ["序列", "/sequence", "Sequencer"],
    ["设计器", "/designer", "Designer"],
    ["构图", "/framing", "Framing"], ["辅助", "/aux", "Auxiliary"],
    ["图库", "/library", "Library"],
  ];
  const ROUTES = NAV.map(([, p]) => p);
  const norm = (p) => (p === "/" ? "/overview" : p.replace(/\/$/, "")) || "/overview";
  const pageFile = (p) => "/nina-" + (norm(p) === "/overview" ? "overview" : norm(p).slice(1)) + ".html";

  // -- API 客户端 ---------------------------------------------------------- //
  const Ops = window.Ops = {
    sessionId: sid,
    isController: false,
    readonly: false,
    writableDomains: [],            // 只读下仍可控的域(按域解禁,如 ["camera"])
    fullyReadonly: false,           // readonly 且无任何解禁域 → 完全只读
    controlState: null,
    _evtHandlers: [],
    _ctrlHandlers: [],

    async api(path, opts = {}) {
      const r = await fetch(path, { cache: "no-store", ...opts });
      const ct = r.headers.get("content-type") || "";
      const data = ct.includes("json") ? await r.json() : await r.text();
      return { ok: r.ok, status: r.status, data };
    },

    async get(path) {
      const gen = __pageGen;
      let res;
      try {
        res = await Ops.api(path, { signal: __pageAbort ? __pageAbort.signal : undefined });
      } catch (e) {
        if (e && e.name === "AbortError") return await new Promise(() => {});  // 本页已切走:让旧回调静默丢弃,不继续
        throw e;
      }
      // 仅当仍是发起它的那一页时,首个"本页域数据"返回后才淡入(防跨页竞态误 reveal 下一页)
      if (gen === __pageGen && !__viewRevealed && path !== "/api/status" && path.indexOf("/api/control-role") < 0)
        requestAnimationFrame(revealView);
      return res.data;
    },

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
    requireControl(domain) {
      // 只读模式下,仅"已解禁的域"放行(传 domain 才判定);其余一律拦下
      if (Ops.readonly && !(domain && Ops.writableDomains.includes(domain))) {
        Ops.status("warn", "只读监控模式 —— 已禁用对设备的操作"); return false;
      }
      if (!Ops.isController) { Ops.status("warn", "请先在右上角切到「主控」再操作"); return false; }
      return true;
    },
    esc(s) {  // HTML 转义:设备名/目标名/滤镜名等经 innerHTML 渲染前先过它,防注入
      return String(s == null ? "" : s).replace(/[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },

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

  // -- WebSocket 事件流(常驻,自动重连) ---------------------------------- //
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

  // -- 顶栏(常驻,只建一次;切页只更新 active 链接) --------------------- //
  function mountTopbar() {
    const host = document.getElementById("app-topbar");
    if (!host || host.dataset.built) return;
    host.dataset.built = "1";
    host.className = "ops-topbar";
    host.innerHTML =
      `<div class="ops-brand"><b>星枢</b> · 远程台</div>`
      + `<nav class="ops-nav">${NAV.map(([t, p, en]) =>
        `<a href="${p}">${t}<small>${en}</small></a>`).join("")}</nav>`
      + `<div class="ops-actions"><span id="ops-clock">--:--:--</span>`
      + `<span id="ops-ctrl" class="ops-ctrl">主控空闲</span>`
      + `<select id="ops-role" class="ops-role" aria-label="协作模式">`
      + `<option value="monitor">监控</option><option value="controller">主控</option></select></div>`;

    const clk = host.querySelector("#ops-clock");
    const pad = (n) => String(n).padStart(2, "0");
    const tick = () => { const d = new Date(); clk.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
    tick();
    realSetInterval(tick, 1000);   // 常驻:不被 teardown 清

    const ind = host.querySelector("#ops-ctrl");
    const sel = host.querySelector("#ops-role");
    const render = (p) => {
      Ops.controlState = p;
      const holder = p && p.controller, self = !!(p && p.held_by_self);
      Ops.isController = self;
      // 完全只读:定格"只读监控",不显示主控状态。部分解禁(如相机可控)则正常显示主控,
      // 以便用户取得主控来操作已解禁的域。
      if (Ops.fullyReadonly) { ind.className = "ops-ctrl busy"; ind.textContent = "只读监控"; return; }
      ind.className = "ops-ctrl" + (self ? " self" : holder ? " busy" : "");
      const suffix = Ops.readonly ? " · 限" + Ops.writableDomains.join("/") : "";
      ind.textContent = (!holder ? "主控空闲" : self ? "当前主控"
        : `主控中 · ${holder.display_name || holder.client_ip || "其他"}`) + (holder || self ? "" : suffix);
      sel.value = self ? "controller" : "monitor";
      Ops._ctrlHandlers.forEach((cb) => { try { cb(p); } catch (x) {} });
    };
    const poll = async () => {
      try {
        const d = await Ops.api(`/api/control-role?session_id=${encodeURIComponent(sid)}`);
        if (d.ok && d.data && d.data.ok) render(d.data);
      } catch (e) {}
    };
    sel.addEventListener("change", async () => {
      if (Ops.fullyReadonly) return;
      const d = await Ops.post("/api/control-role", { role: sel.value, session_label: "web" });
      if (d && d.ok) { render(d); Ops.status("ok", sel.value === "controller" ? "已取得主控权" : "已切回监控"); }
    });
    poll();
    realSetInterval(() => { if (!document.hidden) poll(); }, 10000);
    realSetInterval(() => { if (!document.hidden && Ops.isController)
      Ops.post("/api/control-role", { role: "controller", session_label: "web" }); }, 20000);
  }

  function updateActiveNav(path) {
    const p = norm(path);
    document.querySelectorAll(".ops-nav a").forEach((a) => {
      a.classList.toggle("active", norm(a.getAttribute("href")) === p);
    });
  }

  function mountStatus() {
    if (document.getElementById("app-status")) return;
    const f = document.createElement("footer");
    f.id = "app-status";
    f.className = "ops-status";
    document.body.appendChild(f);
  }

  // -- 内容区错峰浮现(数据就绪后) ---------------------------------------- //
  // 内容在 view-pending(整体隐藏)下搭建+填值,布局先稳定;就绪后把各列/行拆成
  // 浮现单元,赋递增时延并于下一帧统一放行 → 自上而下、逐列错峰浮现。
  // 全程只动 opacity+transform(不引起回流),故不会带回任何布局位移。
  let __viewRevealed = false, __revealTimer = null;
  // 页面代次 + 中止器:用于切页时作废上一页在途的读取与 reveal(跨页竞态防护)
  let __pageGen = 0, __pageAbort = null;
  function prepareStagger(main) {
    const units = [];
    const visible = (el) => el.tagName !== "SCRIPT" && el.tagName !== "STYLE" && el.offsetParent !== null;
    const isRowGroup = (el) => [...el.children].some((c) => c.matches(".kv,.row,.readout,.eq"));
    (function collect(node) {
      for (const ch of node.children) {
        if (ch.matches(".rail")) {
          if (visible(ch)) units.push(ch);                // 右栏整列作为一个浮现单元(金线随该列一起浮现,不先亮)
        } else if (ch.matches(".col-rail, .cam-grid")) {
          for (const col of ch.children) {                // 多列网格 → 逐列展开(.rail 列由上面分支整列收为单元)
            if (col.matches(".rail")) { if (visible(col)) units.push(col); }
            else collect(col);
          }
        } else if (isRowGroup(ch)) {
          collect(ch);                                    // 读数组 → 递归到行
        } else if (visible(ch)) {
          units.push(ch);                                 // 其余可见块(跳过 display:none 的占位/通知,不占错峰名额)
        }
      }
    })(main);
    units.forEach((el, i) => {
      el.style.animationDelay = Math.min(i * 42, 760) + "ms";
      el.classList.add("ops-rise");       // opsRise 含 from:opacity:0 + fill:both → 起始即隐藏,无需 rAF/前置占位
    });
  }
  function revealView() {
    if (__viewRevealed) return;
    __viewRevealed = true;
    clearTimeout(__revealTimer);
    const m = document.querySelector("main");
    if (!m) return;
    // 此刻 main 仍 view-pending(整体不可见):给各单元挂上错峰浮现动画(动画 fill 起始为 opacity:0),
    // 再移除 view-pending → main 转可见,各单元仍由动画 fill 处于 opacity:0,随后逐列浮现。全程不依赖 rAF。
    prepareStagger(m);
    m.classList.remove("view-pending");
  }

  // -- 客户端路由 ---------------------------------------------------------- //
  const cache = {};
  let pageStyleEl = null;

  async function fetchPage(path) {
    if (cache[path]) return cache[path];
    const html = await fetch(pageFile(path), { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); });
    const doc = new DOMParser().parseFromString(html, "text/html");
    const main = doc.querySelector("main");
    const style = [...doc.querySelectorAll("head style")]
      .filter((s) => !/html\s*,\s*body\s*\{\s*background:#0a0e1a/.test(s.textContent))
      .map((s) => s.textContent).join("\n");
    const script = [...doc.querySelectorAll("body script:not([src])")]
      .map((s) => s.textContent).join("\n");
    cache[path] = { main: main ? main.outerHTML : "<main class='wrap'></main>", style, script, title: doc.title || "星枢" };
    return cache[path];
  }

  async function loadInto(path, push) {
    const entry = await fetchPage(path);
    if (__pageAbort) __pageAbort.abort();      // 作废上一页在途读取
    __pageAbort = new AbortController();
    __pageGen++;                                // 作废上一页在途的 reveal 触发
    teardownPage();
    if (pageStyleEl) pageStyleEl.textContent = entry.style;
    const cur = document.querySelector("main");
    const tmp = document.createElement("div");
    tmp.innerHTML = entry.main;
    const newMain = tmp.querySelector("main") || tmp.firstElementChild;
    newMain.classList.add("view-pending");
    if (cur) cur.replaceWith(newMain); else document.body.appendChild(newMain);
    document.title = entry.title;
    __viewRevealed = false;
    if (push) history.pushState({ path }, "", path);
    updateActiveNav(path);
    // 运行页面脚本:IIFE 包裹避免顶层 const/let 重复声明,作用域随切页释放
    const s = document.createElement("script");
    s.textContent = "(function(){\n" + entry.script + "\n})();";
    document.body.appendChild(s);
    s.remove();
    document.dispatchEvent(new CustomEvent("ops:ready"));
    clearTimeout(__revealTimer);
    __revealTimer = setTimeout(revealView, 1200);   // 兜底:数据不来也要显示
    // 预取其余页面(空闲时,只取一次)
    prefetchAll();
  }

  function navigate(path, push = true) {
    path = norm(path);
    if (!ROUTES.includes(path)) { location.href = path; return; }
    if (path === norm(location.pathname)) return;
    loadInto(path, push).catch((e) => { console.warn("[router] 回退整页跳转:", e); location.href = path; });
  }

  let __prefetched = false;
  function prefetchAll() {
    if (__prefetched) return;
    __prefetched = true;
    const run = () => ROUTES.forEach((p) => { fetchPage(p).catch(() => {}); });
    if (window.requestIdleCallback) requestIdleCallback(run, { timeout: 3000 });
    else setTimeout(run, 1500);
  }

  // 拦截顶栏内部导航点击 → 客户端切页
  realAddEvent.call(document, "click", (e) => {
    const a = e.target.closest && e.target.closest("a");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href || !ROUTES.includes(norm(href))) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.target === "_blank") return;
    e.preventDefault();
    navigate(href, true);
  }, true);

  window.addEventListener("popstate", (e) => {
    const path = norm((e.state && e.state.path) || location.pathname);
    if (!ROUTES.includes(path)) return;
    loadInto(path, false).catch(() => location.reload());
  });

  // -- 启动 ---------------------------------------------------------------- //
  function boot() {
    // 建立常驻外壳
    __pageAbort = new AbortController();   // 首屏页也可在首次切页时作废其在途读取
    mountStatus();
    mountTopbar();
    updateActiveNav(location.pathname);
    connectWS();
    // 创建承载各页内联样式的元素(把首屏页自带的内联 <style> 迁入,便于切页替换)
    pageStyleEl = document.createElement("style");
    pageStyleEl.id = "ops-page-style";
    const moved = [];
    document.querySelectorAll("head style").forEach((s) => {
      if (!/html\s*,\s*body\s*\{\s*background:#0a0e1a/.test(s.textContent)) moved.push(s);
    });
    pageStyleEl.textContent = moved.map((s) => s.textContent).join("\n");
    document.head.appendChild(pageStyleEl);
    moved.forEach((s) => s.remove());

    // 首屏页内容已就位(其脚本已随解析执行并被跟踪),数据就绪后淡入;兜底定时显示
    clearTimeout(__revealTimer);
    __revealTimer = setTimeout(revealView, 1500);
    document.dispatchEvent(new CustomEvent("ops:ready"));   // 首屏页 init
    prefetchAll();

    // /api/status:状态行 + 只读/按域解禁标记
    Ops.api("/api/status").then(({ ok, data }) => {
      if (ok && data && data.ok) {
        Ops.readonly = !!data.readonly;
        Ops.writableDomains = data.writable_domains || [];
        Ops.fullyReadonly = Ops.readonly && Ops.writableDomains.length === 0;
        const role = document.getElementById("ops-role");
        if (Ops.fullyReadonly) {
          const ind = document.getElementById("ops-ctrl");
          if (ind) { ind.textContent = "只读监控"; ind.className = "ops-ctrl busy"; }
          if (role) { role.disabled = true; role.title = "只读模式不可主控"; }
        } else if (role) {
          role.disabled = false;       // 部分解禁:可取得主控来操作已解禁的域
          role.title = Ops.readonly ? "只读监控 · " + Ops.writableDomains.join("/") + " 可控" : "";
        }
        const mode = data.provider === "sim" ? "模拟引擎" : "NINA 实机";
        const ctl = Ops.fullyReadonly ? " · 只读监控"
          : Ops.readonly ? ` · ${Ops.writableDomains.join("/")} 可控(余只读)` : "";
        Ops.status(Ops.readonly ? "warn" : "ok",
          `已连接后端 · ${mode}${ctl} · `
          + `${data.connected_devices.length}/${data.device_count} 设备在线`);
        window.OPS_BOOT = data;
      } else {
        Ops.status("err", "后端不可达 —— 请确认 星枢后端服务已启动");
      }
    }).catch(() => Ops.status("err", "后端不可达"));
  }

  if (document.readyState === "loading")
    realAddEvent.call(document, "DOMContentLoaded", boot);
  else boot();
})();
