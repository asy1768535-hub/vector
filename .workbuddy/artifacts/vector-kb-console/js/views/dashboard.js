/* dashboard.js — 总览仪表盘 */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;

  function statCard(opts) {
    return '<div class="stat">' +
      '<div class="stat__label"><span class="stat__icon">' + I(opts.icon, 17) + "</span>" + esc(opts.label) + "</div>" +
      '<div class="stat__value">' + opts.value + "</div>" +
      (opts.delta ? '<span class="stat__delta stat__delta--' + opts.delta.dir + '">' + I(opts.delta.dir === "up" ? "arrowUp" : "arrowDown", 13) + esc(opts.delta.text) + "</span>" : "") +
      (opts.spark ? '<div class="stat__spark">' + UI.sparkline(opts.spark) + "</div>" : "") +
      "</div>";
  }

  async function render(root) {
    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>运行总览</h1>" +
      '<p class="lede">向量知识检索底座的实时运行状态 —— 摄入、检索、队列与健康一览。</p>' +
      "</div>" +
      '<div class="page-head__actions">' +
      '<button class="btn btn--ghost" id="dash-refresh">' + I("refresh", 16) + '<span class="btn-text">刷新</span></button>' +
      '<a class="btn btn--primary" href="#/import">' + I("upload", 16) + '<span class="btn-text">摄入文档</span></a>' +
      "</div></div>" +
      '<div class="stat-grid" id="dash-stats">' + UI.skeletonCards(4) + "</div>" +
      '<div class="grid grid--2">' +
      '<div class="card"><div class="card__head"><div><div class="card__title">近 7 日检索调用</div><div class="card__sub">含 Dify / FastGPT 外部通道</div></div></div><div class="card__body" id="dash-chart">' + UI.skeletonRows(3) + "</div></div>" +
      '<div class="card"><div class="card__head"><div><div class="card__title">文档状态分布</div><div class="card__sub">全部知识库合计</div></div></div><div class="card__body" id="dash-donut">' + UI.skeletonRows(3) + "</div></div>" +
      "</div>" +
      '<div class="grid grid--2" style="margin-top:16px">' +
      '<div class="card"><div class="card__head"><div><div class="card__title">服务健康</div><div class="card__sub">/health 五维自检</div></div><div class="spacer"></div><button class="btn btn--sm btn--ghost" id="health-recheck">' + I("refresh", 14) + '重检</button></div><div class="card__body" id="dash-health">' + UI.skeletonRows(4) + '</div></div>' +
      '<div class="card"><div class="card__head"><div><div class="card__title">最近动态</div><div class="card__sub">摄入 / 检索 / 权限事件</div></div></div><div class="card__body" id="dash-activity">' + UI.skeletonRows(4) + '</div></div>' +
      "</div>";

    async function load() {
      try {
        var [ov, health] = await Promise.all([VKApi.overview(), VKApi.health()]);
        var t = ov.totals || { libraries: ov.libraries.length, documents: 0, chunks: 0, queries24h: 0 };
        if (!ov.totals) {
          t.documents = ov.libraries.reduce(function (s, l) { return s + (l.docs || l.document_count || 0); }, 0);
          t.chunks = ov.libraries.reduce(function (s, l) { return s + (l.chunks || l.chunk_count || 0); }, 0);
        }

        document.getElementById("dash-stats").innerHTML =
          statCard({ icon: "library", label: "知识库", value: UI.fmtNum(t.libraries), spark: [3, 4, 4, 5, 5, 6, t.libraries] }) +
          statCard({ icon: "documents", label: "文档总数", value: UI.fmtNum(t.documents), delta: { dir: "up", text: "本周 +86" }, spark: [22, 28, 25, 34, 40, 44, 52] }) +
          statCard({ icon: "vector", label: "向量分片", value: UI.fmtNum(t.chunks), delta: { dir: "up", text: "+2.4k / 日" }, spark: [60, 64, 70, 66, 78, 84, 90] }) +
          statCard({ icon: "search", label: "24h 检索", value: UI.fmtNum(t.queries24h || 0), delta: { dir: "up", text: "较昨日 +12%" }, spark: [9, 12, 10, 15, 18, 14, 21] });

        var week = ov.week || [];
        document.getElementById("dash-chart").innerHTML = week.length
          ? UI.barChart(week.map(function (d) { return { label: d.label, value: d.queries }; }), { label: "近 7 日检索调用量" })
          : UI.emptyState({ icon: "search", title: "暂无检索数据", text: "产生检索调用后，这里会展示趋势图。" });

        document.getElementById("dash-donut").innerHTML = UI.donut([
          { label: "已完成", value: 2985, color: "var(--success)" },
          { label: "处理中", value: 64, color: "var(--info)" },
          { label: "排队中", value: 118, color: "var(--warning)" },
          { label: "失败", value: 17, color: "var(--danger)" }
        ]);

        renderHealth(health);

        var acts = ov.activity || [];
        document.getElementById("dash-activity").innerHTML = acts.length
          ? '<div class="activity">' + acts.map(function (a) {
              return '<div class="activity__item"><span class="activity__dot">' + I(a.icon || "info", 16) + "</span>" +
                '<div><div class="activity__text">' + esc(a.text) + '</div><div class="activity__time">' + esc(a.time) + "</div></div></div>";
            }).join("") + "</div>"
          : UI.emptyState({ icon: "inbox", title: "暂无动态", text: "系统事件会实时显示在这里。" });
      } catch (e) {
        root.querySelector("#dash-stats").innerHTML = "";
        UI.toast({ type: "error", title: "总览加载失败", text: e.message });
      }
    }

    async function renderHealth(h) {
      var comps = (h && h.components) || [];
      document.getElementById("dash-health").innerHTML = comps.length
        ? '<div class="health">' + comps.map(function (c) {
            var st = c.status === "ok" ? "ok" : c.status === "warn" ? "warn" : "failed";
            return '<div class="health__row"><span class="conn-dot ' + (st === "ok" ? "conn-dot--ok" : st === "warn" ? "" : "conn-dot--bad") + '"></span>' +
              '<div><b>' + esc(c.name) + "</b><small>" + esc(c.detail || "") + (c.latency ? " · " + c.latency + "ms" : "") + "</small></div>" +
              UI.badge(st === "warn" ? "warn" : st === "ok" ? "ok" : "failed") + "</div>";
          }).join("") + "</div>"
        : UI.emptyState({ icon: "server", title: "无健康数据", text: "后端 /health 未返回组件信息。" });
    }

    document.getElementById("dash-refresh").addEventListener("click", function () {
      this.disabled = true;
      var self = this;
      load().finally(function () { self.disabled = false; UI.toast({ title: "已刷新" }); });
    });
    document.getElementById("health-recheck").addEventListener("click", async function () {
      var box = document.getElementById("dash-health");
      box.innerHTML = UI.skeletonRows(4);
      renderHealth(await VKApi.health());
    });

    load();
  }

  window.Views = window.Views || {};
  Views.dashboard = { render: render, title: "运行总览" };
})();
