/* ============================================================
   components.js — 通用 UI 工具：图标 / Toast / 空态 / 骨架屏 / 格式化
   ============================================================ */
(function () {
  "use strict";

  /* ---------------- Inline SVG Icons (24x24, stroke) ---------------- */
  var P = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  var ICONS = {
    dashboard: '<rect x="3" y="3" width="7.5" height="9" rx="1.5" ' + P + '/><rect x="13.5" y="3" width="7.5" height="5.5" rx="1.5" ' + P + '/><rect x="13.5" y="12" width="7.5" height="9" rx="1.5" ' + P + '/><rect x="3" y="15.5" width="7.5" height="5.5" rx="1.5" ' + P + '/>',
    library: '<path d="M4 19.5V6a2 2 0 0 1 2-2h13v13.5" ' + P + '/><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H19v3.5a2 2 0 0 1-2 2H6.5A2.5 2.5 0 0 1 4 19.5Z" ' + P + '/><path d="M9 8.5h6" ' + P + '/>',
    documents: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" ' + P + '/><path d="M14 3v5h5" ' + P + '/><path d="M9 13h6M9 17h4" ' + P + '/>',
    search: '<circle cx="11" cy="11" r="7" ' + P + '/><path d="m20 20-3.8-3.8" ' + P + '/>',
    import: '<path d="M12 3v12" ' + P + '/><path d="m7 10 5 5 5-5" ' + P + '/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" ' + P + '/>',
    jobs: '<circle cx="12" cy="12" r="3" ' + P + '/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.2 2.2M16.9 16.9l2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2" ' + P + '/>',
    settings: '<circle cx="12" cy="12" r="3" ' + P + '/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h.01a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1Z" ' + P + '/>',
    sun: '<circle cx="12" cy="12" r="4" ' + P + '/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" ' + P + '/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" ' + P + '/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16" ' + P + '/>',
    close: '<path d="M18 6 6 18M6 6l12 12" ' + P + '/>',
    plus: '<path d="M12 5v14M5 12h14" ' + P + '/>',
    upload: '<path d="M12 16V4" ' + P + '/><path d="m7 9 5-5 5 5" ' + P + '/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" ' + P + '/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36" ' + P + '/><path d="M21 3v6h-6" ' + P + '/>',
    trash: '<path d="M4 7h16" ' + P + '/><path d="M10 11v6M14 11v6" ' + P + '/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" ' + P + '/><path d="M9 7V4h6v3" ' + P + '/>',
    alert: '<path d="M12 9v4M12 17h.01" ' + P + '/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" ' + P + '/>',
    key: '<circle cx="7.5" cy="15.5" r="4.5" ' + P + '/><path d="m11 12 9-9" ' + P + '/><path d="m15 8 2.5 2.5M18 5l2 2" ' + P + '/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5" ' + P + '/>',
    error: '<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 8v4.5M12 16h.01" ' + P + '/>',
    info: '<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 16v-4.5M12 8h.01" ' + P + '/>',
    clock: '<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 7v5l3.5 2" ' + P + '/>',
    retry: '<path d="M3 12a9 9 0 1 0 2.64-6.36" ' + P + '/><path d="M3 3v6h6" ' + P + '/>',
    external: '<path d="M15 3h6v6" ' + P + '/><path d="M10 14 21 3" ' + P + '/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" ' + P + '/>',
    chevronL: '<path d="m14.5 6-6 6 6 6" ' + P + '/>',
    chevronR: '<path d="m9.5 6 6 6-6 6" ' + P + '/>',
    arrowUp: '<path d="M12 19V5" ' + P + '/><path d="m5 12 7-7 7 7" ' + P + '/>',
    arrowDown: '<path d="M12 5v14" ' + P + '/><path d="m19 12-7 7-7-7" ' + P + '/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" ' + P + '/><path d="M14 3v5h5" ' + P + '/>',
    database: '<ellipse cx="12" cy="5.5" rx="8" ry="3" ' + P + '/><path d="M4 5.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" ' + P + '/><path d="M4 11.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" ' + P + '/>',
    vector: '<circle cx="5" cy="6" r="2.2" ' + P + '/><circle cx="19" cy="6" r="2.2" ' + P + '/><circle cx="5" cy="18" r="2.2" ' + P + '/><circle cx="19" cy="18" r="2.2" ' + P + '/><path d="M7 7.2 17 16.8M17 7.2 7 16.8M7.2 6h9.6M7.2 18h9.6M5 8.2v7.6M19 8.2v7.6" ' + P + '/>',
    spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8" ' + P + '/>',
    send: '<path d="m21 3-9.5 9.5" ' + P + '/><path d="M21 3 14 21l-2.5-8.5L3 10l18-7Z" ' + P + '/>',
    filter: '<path d="M4 5h16l-6.5 7.5v5L10 20v-7.5L4 5Z" ' + P + '/>',
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2" ' + P + '/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1Z" ' + P + '/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" ' + P + '/><path d="m16 17 5-5-5-5M21 12H9" ' + P + '/>',
    user: '<circle cx="12" cy="8" r="4" ' + P + '/><path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5" ' + P + '/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2" ' + P + '/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" ' + P + '/>',
    server: '<rect x="3" y="4" width="18" height="7" rx="2" ' + P + '/><rect x="3" y="13" width="18" height="7" rx="2" ' + P + '/><path d="M7 7.5h.01M7 16.5h.01" ' + P + '/>'
  };

  function icon(name, size) {
    var s = size || 20;
    var body = ICONS[name] || ICONS.info;
    return '<svg class="icon" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" aria-hidden="true">' + body + '</svg>';
  }

  /* ---------------- formatting ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmtNum(n) {
    if (n == null || isNaN(n)) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtBytes(b) {
    if (b == null) return "-";
    var u = ["B", "KB", "MB", "GB"];
    var i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i === 0 ? b : b.toFixed(1)) + " " + u[i];
  }
  function fmtTime(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d)) return String(iso);
    var diff = Date.now() - d.getTime();
    if (diff < 60e3) return "刚刚";
    if (diff < 3600e3) return Math.floor(diff / 60e3) + " 分钟前";
    if (diff < 86400e3) return Math.floor(diff / 3600e3) + " 小时前";
    if (diff < 7 * 86400e3) return Math.floor(diff / 86400e3) + " 天前";
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function highlight(text, words) {
    var html = esc(text);
    (words || []).forEach(function (w) {
      if (!w || w.length < 1) return;
      var safe = w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      html = html.replace(new RegExp("(" + safe + ")", "gi"), "<mark>$1</mark>");
    });
    return html;
  }

  /* ---------------- badges ---------------- */
  var STATUS_META = {
    done: { label: "已完成", cls: "success" },
    processing: { label: "处理中", cls: "info" },
    pending: { label: "排队中", cls: "warning" },
    failed: { label: "失败", cls: "danger" },
    active: { label: "运行中", cls: "success" },
    rebuilding: { label: "重建中", cls: "warning" },
    ok: { label: "正常", cls: "success" },
    warn: { label: "未启用", cls: "neutral" }
  };
  function badge(status, withDot) {
    var m = STATUS_META[status] || { label: status, cls: "neutral" };
    return '<span class="badge badge--' + m.cls + '">' + (withDot !== false ? '<i class="dot"></i>' : "") + esc(m.label) + "</span>";
  }

  /* ---------------- states ---------------- */
  function emptyState(opts) {
    return '<div class="state">' +
      '<div class="state__icon">' + icon(opts.icon || "inbox", 26) + "</div>" +
      "<h3>" + esc(opts.title || "暂无数据") + "</h3>" +
      "<p>" + esc(opts.text || "") + "</p>" +
      (opts.action || "") +
      "</div>";
  }
  function errorState(msg, retryFn) {
    return '<div class="state" role="alert">' +
      '<div class="state__icon" style="color:var(--danger);background:var(--danger-soft)">' + icon("error", 26) + "</div>" +
      "<h3>加载失败</h3><p>" + esc(msg || "请求出错，请稍后重试") + "</p>" +
      '<button class="btn btn--ghost" data-retry>' + icon("retry", 16) + "重试</button>" +
      "</div>";
  }
  function skeletonRows(n) {
    var s = "";
    for (var i = 0; i < (n || 5); i++) s += '<div class="skeleton sk-row"></div>';
    return s;
  }
  function skeletonCards(n) {
    var s = "";
    for (var i = 0; i < (n || 3); i++) s += '<div class="skeleton sk-card"></div>';
    return s;
  }

  /* ---------------- toast ---------------- */
  var toastRegion = null;
  function ensureToastRegion() {
    if (!toastRegion) {
      toastRegion = document.createElement("div");
      toastRegion.className = "toast-region";
      toastRegion.setAttribute("aria-live", "polite");
      document.body.appendChild(toastRegion);
    }
    return toastRegion;
  }
  function toast(opts) {
    var type = opts.type || "success";
    var icons = { success: "check", error: "error", warning: "alert", info: "info" };
    var colors = { success: "var(--success)", error: "var(--danger)", warning: "var(--warning)", info: "var(--info)" };
    var el = document.createElement("div");
    el.className = "toast toast--" + type;
    el.innerHTML =
      '<span style="color:' + colors[type] + ';flex:none;margin-top:1px">' + icon(icons[type] || "info", 18) + "</span>" +
      "<div><b>" + esc(opts.title || "") + "</b>" + (opts.text ? "<span>" + esc(opts.text) + "</span>" : "") + "</div>";
    ensureToastRegion().appendChild(el);
    setTimeout(function () {
      el.classList.add("is-leaving");
      setTimeout(function () { el.remove(); }, 260);
    }, opts.duration || 3600);
  }

  /* ---------------- modal ---------------- */
  function modal(opts) {
    var backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-label="' + esc(opts.title || "") + '">' +
      '<div class="modal__head"><div class="modal__title">' + esc(opts.title || "") + "</div>" +
      '<div style="flex:1"></div>' +
      '<button class="icon-btn" data-close aria-label="关闭">' + icon("close", 18) + "</button></div>" +
      '<div class="modal__body">' + (opts.body || "") + "</div>" +
      (opts.footer ? '<div class="modal__foot">' + opts.footer + "</div>" : "") +
      "</div>";
    function close() {
      backdrop.classList.remove("is-open");
      setTimeout(function () { backdrop.remove(); document.removeEventListener("keydown", onKey); }, 200);
    }
    function onKey(e) { if (e.key === "Escape") close(); }
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop || e.target.closest("[data-close]")) close();
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
    requestAnimationFrame(function () { backdrop.classList.add("is-open"); });
    if (opts.onMount) opts.onMount(backdrop, close);
    var firstInput = backdrop.querySelector("input, select, textarea, button.btn--primary");
    if (firstInput) setTimeout(function () { firstInput.focus(); }, 120);
    return { close: close, el: backdrop };
  }

  function confirmDialog(opts) {
    return new Promise(function (resolve) {
      modal({
        title: opts.title || "确认操作",
        body: '<p style="color:var(--text-2);font-size:14px">' + esc(opts.text || "") + "</p>",
        footer:
          '<button class="btn btn--ghost" data-close>取消</button>' +
          '<button class="btn ' + (opts.danger ? "btn--danger" : "btn--primary") + '" data-ok>' + esc(opts.okText || "确认") + "</button>",
        onMount: function (el, close) {
          el.querySelector("[data-ok]").addEventListener("click", function () {
            close(); resolve(true);
          });
          el.addEventListener("click", function (e) {
            if (e.target.closest("[data-close]")) resolve(false);
          });
        }
      });
    });
  }

  /* ---------------- charts ---------------- */
  function barChart(data, opts) {
    opts = opts || {};
    var max = Math.max.apply(null, data.map(function (d) { return d.value; }).concat([1]));
    var html = '<div class="chart" role="img" aria-label="' + esc(opts.label || "柱状图") + '">';
    data.forEach(function (d, i) {
      var h = Math.max(3, Math.round((d.value / max) * 100));
      html += '<div class="chart__bar' + (d.muted ? " muted" : "") + '" title="' + esc(d.label + "：" + d.value) + '">' +
        '<div class="chart__col" style="height:' + h + "%;animation-delay:" + i * 45 + 'ms"></div>' +
        '<div class="chart__label">' + esc(d.label) + "</div></div>";
    });
    return html + "</div>";
  }

  function sparkline(values, w, h) {
    w = w || 84; h = h || 30;
    var max = Math.max.apply(null, values.concat([1]));
    var min = Math.min.apply(null, values.concat([0]));
    var range = max - min || 1;
    var pts = values.map(function (v, i) {
      var x = (i / (values.length - 1)) * w;
      var y = h - 3 - ((v - min) / range) * (h - 6);
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true">' +
      '<polygon points="0,' + h + " " + pts.join(" ") + " " + w + "," + h + '"/>' +
      '<polyline points="' + pts.join(" ") + '"/></svg>';
  }

  function donut(parts, size) {
    size = size || 120;
    var total = parts.reduce(function (s, p) { return s + p.value; }, 0) || 1;
    var r = 45, cx = 60, cy = 60, C = 2 * Math.PI * r;
    var offset = 0;
    var circles = parts.map(function (p) {
      var frac = p.value / total;
      var dash = (frac * C).toFixed(2) + " " + (C - frac * C).toFixed(2);
      var c = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke="' + p.color + '" stroke-dasharray="' + dash + '" stroke-dashoffset="' + (-offset * C).toFixed(2) + '"/>';
      offset += frac;
      return c;
    }).join("");
    var legend = parts.map(function (p) {
      return '<li><span class="swatch" style="background:' + p.color + '"></span>' + esc(p.label) + "<b>" + fmtNum(p.value) + "</b></li>";
    }).join("");
    return '<div class="donut-wrap">' +
      '<svg class="donut" width="' + size + '" height="' + size + '" viewBox="0 0 120 120" role="img" aria-label="占比图">' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke="var(--bg-sunken)"/>' + circles + "</svg>" +
      '<ul class="donut-legend">' + legend + "</ul></div>";
  }

  function scoreBar(score) {
    var pct = Math.round((score || 0) * 100);
    return '<div class="score"><div class="progress"><i style="width:' + pct + '%"></i></div><span class="val">' + (score || 0).toFixed(3) + "</span></div>";
  }

  /* ---------------- export ---------------- */
  window.UI = {
    icon: icon, esc: esc, fmtNum: fmtNum, fmtBytes: fmtBytes, fmtTime: fmtTime,
    highlight: highlight, badge: badge,
    emptyState: emptyState, errorState: errorState,
    skeletonRows: skeletonRows, skeletonCards: skeletonCards,
    toast: toast, modal: modal, confirm: confirmDialog,
    barChart: barChart, sparkline: sparkline, donut: donut, scoreBar: scoreBar
  };
})();
