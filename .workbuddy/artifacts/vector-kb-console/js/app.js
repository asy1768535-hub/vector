/* app.js — 应用壳：hash 路由 / 布局 / 主题 / 键盘导航 */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;

  /* ---------------- theme preference ---------------- */
  var THEME_KEY = "vkb_theme";
  var mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  function applyTheme() {
    var pref = localStorage.getItem(THEME_KEY) || "system";
    var dark = pref === "dark" || (pref === "system" && mediaQuery.matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.innerHTML = I(dark ? "sun" : "moon", 18);
  }
  window.ThemePref = {
    get: function () { return localStorage.getItem(THEME_KEY) || "system"; },
    set: function (v) { localStorage.setItem(THEME_KEY, v); applyTheme(); },
    toggle: function () {
      var cur = document.documentElement.getAttribute("data-theme");
      window.ThemePref.set(cur === "dark" ? "light" : "dark");
    }
  };
  mediaQuery.addEventListener("change", applyTheme);

  /* ---------------- routes ---------------- */
  var ROUTES = [
    { hash: "#/dashboard", view: "dashboard", label: "运行总览", icon: "dashboard", section: "概览" },
    { hash: "#/libraries", view: "libraries", label: "知识库", icon: "library", section: "资源" },
    { hash: "#/documents", view: "documents", label: "文档管理", icon: "documents", section: "资源" },
    { hash: "#/import", view: "import", label: "文档摄入", icon: "import", section: "资源" },
    { hash: "#/search", view: "search", label: "语义检索", icon: "search", section: "检索" },
    { hash: "#/jobs", view: "jobs", label: "任务队列", icon: "jobs", section: "检索" },
    { hash: "#/settings", view: "settings", label: "系统设置", icon: "settings", section: "系统" }
  ];

  /* ---------------- shell ---------------- */
  function buildShell() {
    var sections = [];
    var navHtml = "";
    ROUTES.forEach(function (r) {
      if (sections.indexOf(r.section) < 0) {
        sections.push(r.section);
        navHtml += '<div class="nav-section">' + esc(r.section) + "</div>";
      }
      navHtml += '<a class="nav-item" href="' + r.hash + '" data-route="' + r.view + '">' +
        I(r.icon, 19) + "<span>" + esc(r.label) + "</span>" +
        (r.view === "jobs" ? '<span class="nav-item__badge" id="nav-jobs-badge" hidden></span>' : "") +
        "</a>";
    });

    document.getElementById("app").innerHTML =
      '<a class="skip-link" href="#main-content">跳到主要内容</a>' +
      '<div class="app-shell">' +
      '<aside class="sidebar" id="sidebar" aria-label="主导航">' +
      '<div class="sidebar__brand">' +
      '<span class="sidebar__logo">' + I("vector", 20) + "</span>" +
      '<div><div class="sidebar__title">Vector KB</div><div class="sidebar__subtitle">向量知识检索底座</div></div>' +
      '<button class="icon-btn" id="sidebar-close" aria-label="关闭导航" style="margin-left:auto;display:none">' + I("close", 18) + "</button>" +
      "</div>" +
      '<nav class="sidebar__nav">' + navHtml + "</nav>" +
      '<div class="sidebar__footer">' +
      '<button class="conn-pill" id="conn-pill" title="点击查看连接设置">' +
      '<span class="conn-dot" id="conn-dot"></span>' +
      '<span style="min-width:0"><b id="conn-label" style="font-size:12.5px">检测连接中…</b><small id="conn-sub" class="nowrap" style="display:block;overflow:hidden;text-overflow:ellipsis"></small></span>' +
      "</button></div></aside>" +
      '<div class="scrim" id="scrim"></div>' +
      '<div class="main">' +
      '<header class="topbar">' +
      '<button class="icon-btn menu-toggle" id="menu-toggle" aria-label="打开导航">' + I("menu", 20) + "</button>" +
      '<div><div class="topbar__title" id="page-title">运行总览</div>' +
      '<div class="topbar__crumb" id="page-crumb">Vector KB / 运行总览</div></div>' +
      '<div class="topbar__spacer"></div>' +
      '<button class="icon-btn" id="theme-toggle" aria-label="切换主题"></button>' +
      '<a class="icon-btn" href="#/search" aria-label="快速检索">' + I("search", 18) + "</a>" +
      '<span class="icon-btn" aria-hidden="true" title="管理员">' + I("user", 18) + "</span>" +
      "</header>" +
      '<main class="content" id="main-content" tabindex="-1"></main>' +
      "</div></div>";

    /* sidebar (mobile drawer) */
    var sidebar = document.getElementById("sidebar");
    var scrim = document.getElementById("scrim");
    var closeBtn = document.getElementById("sidebar-close");
    function setSidebar(open) {
      sidebar.classList.toggle("is-open", open);
      scrim.classList.toggle("is-open", open);
      closeBtn.style.display = open ? "grid" : "none";
      if (open) sidebar.querySelector(".nav-item").focus();
    }
    document.getElementById("menu-toggle").addEventListener("click", function () { setSidebar(true); });
    closeBtn.addEventListener("click", function () { setSidebar(false); });
    scrim.addEventListener("click", function () { setSidebar(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setSidebar(false);
    });
    sidebar.addEventListener("click", function (e) {
      if (e.target.closest(".nav-item") && window.innerWidth <= 1024) setSidebar(false);
    });

    /* theme toggle */
    document.getElementById("theme-toggle").addEventListener("click", function () { window.ThemePref.toggle(); });

    /* connection pill -> settings */
    document.getElementById("conn-pill").addEventListener("click", function () {
      location.hash = "#/settings";
    });

    /* keyboard shortcuts: Alt+1..6 */
    document.addEventListener("keydown", function (e) {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      var order = ["#/libraries", "#/search", "#/documents", "#/import", "#/jobs", "#/dashboard"];
      var idx = ["1", "2", "3", "4", "5", "6"].indexOf(e.key);
      if (idx >= 0) { e.preventDefault(); location.hash = order[idx]; }
    });
  }

  /* ---------------- connection indicator ---------------- */
  function paintConnection(ok) {
    var dot = document.getElementById("conn-dot");
    var label = document.getElementById("conn-label");
    var sub = document.getElementById("conn-sub");
    if (!dot) return;
    var cfg = VKApi.getConfig();
    if (ok) {
      dot.className = "conn-dot conn-dot--ok";
      label.textContent = "已连接后端";
      sub.textContent = cfg.baseUrl;
    } else {
      dot.className = "conn-dot " + (VKApi.isDemoForced() ? "" : "conn-dot--bad");
      label.textContent = VKApi.isDemoForced() ? "演示模式（已强制）" : "演示模式 · 后端未连通";
      sub.textContent = cfg.baseUrl + " 不可达";
    }
  }

  /* ---------------- router ---------------- */
  var currentView = null; // 当前视图上下文（用于 cleanup）

  function parseHash() {
    var raw = location.hash || "#/dashboard";
    var qIdx = raw.indexOf("?");
    var path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
    var params = new URLSearchParams(qIdx >= 0 ? raw.slice(qIdx + 1) : "");
    return { path: path, params: params };
  }

  async function route() {
    var p = parseHash();
    var r = ROUTES.find(function (x) { return x.hash === p.path; }) || ROUTES[0];

    // cleanup 上一个视图（如轮询定时器）
    if (currentView && typeof currentView.cleanup === "function") {
      try { currentView.cleanup(); } catch (e) {}
    }

    // nav active state
    document.querySelectorAll(".nav-item").forEach(function (a) {
      if (a.getAttribute("data-route") === r.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });

    document.getElementById("page-title").textContent = r.label;
    document.getElementById("page-crumb").textContent = "Vector KB / " + r.label;
    document.title = r.label + " · Vector KB Console";

    var main = document.getElementById("main-content");
    var viewHost = document.createElement("div");
    viewHost.className = "view";
    main.innerHTML = "";
    main.appendChild(viewHost);
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });

    var ctx = {};
    currentView = ctx;
    try {
      await Views[r.view].render.call(ctx, viewHost, p.params);
    } catch (e) {
      console.error(e);
      viewHost.innerHTML = UI.errorState(e.message || "视图渲染失败");
    }
  }

  /* ---------------- jobs badge ---------------- */
  async function refreshJobsBadge() {
    try {
      var jobs = await VKApi.listJobs({});
      var active = jobs.filter(function (j) { return j.status === "processing" || j.status === "pending"; }).length;
      var badge = document.getElementById("nav-jobs-badge");
      if (badge) {
        badge.hidden = active === 0;
        badge.textContent = active > 99 ? "99+" : active;
      }
    } catch (e) { /* ignore */ }
  }

  /* ---------------- boot ---------------- */
  document.addEventListener("DOMContentLoaded", function () {
    buildShell();
    applyTheme();
    window.addEventListener("hashchange", route);
    route();

    VKApi.store.on("connection", paintConnection);
    VKApi.probe(true).then(paintConnection);
    setInterval(function () { VKApi.probe().then(paintConnection); }, 30000);

    refreshJobsBadge();
    setInterval(refreshJobsBadge, 15000);
  });
})();
