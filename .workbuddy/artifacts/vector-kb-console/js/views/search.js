/* search.js — 语义检索（双分数 / 阈值过滤 / 历史记录） */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;
  var HISTORY_KEY = "vkb_search_history";

  function loadHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch (e) { return []; }
  }
  function pushHistory(q) {
    var h = loadHistory().filter(function (x) { return x !== q; });
    h.unshift(q);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(0, 6)));
  }

  async function render(root, params) {
    var state = { lib: params.get("lib") || "", topK: 5, threshold: 0, searching: false };
    var libs = [];
    try { libs = await VKApi.listLibraries(); } catch (e) {}
    if (!state.lib && libs.length) state.lib = libs[0].slug;

    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>语义检索</h1>" +
      '<p class="lede">Dense 召回 + 可见性过滤 + 可选 Rerank，支持 score_threshold 与 metadata 过滤。</p>' +
      "</div></div>" +
      '<div class="card card--pad" style="margin-bottom:16px">' +
      '<div class="toolbar" style="margin-bottom:12px">' +
      '<select class="select" id="s-lib" aria-label="选择知识库"></select>' +
      '<select class="select" id="s-topk" aria-label="返回条数">' +
      '<option value="3">Top 3</option><option value="5" selected>Top 5</option><option value="10">Top 10</option>' +
      "</select>" +
      '<select class="select" id="s-th" aria-label="分数阈值">' +
      '<option value="0">不过滤</option><option value="0.5">阈值 ≥ 0.50</option>' +
      '<option value="0.7">阈值 ≥ 0.70</option><option value="0.85">阈值 ≥ 0.85</option>' +
      "</select>" +
      "</div>" +
      '<div class="search-box">' +
      '<span class="icon">' + I("search", 18) + "</span>" +
      '<input class="input" id="s-q" style="height:48px;font-size:15px" placeholder="输入自然语言问题，例如：员工差旅报销标准是什么？" aria-label="检索内容">' +
      "<kbd>Enter ↵</kbd></div>" +
      '<div class="row between wrap" style="margin-top:12px">' +
      '<div class="row wrap" id="s-history" style="gap:6px"></div>' +
      '<button class="btn btn--primary" id="s-run" style="height:42px">' + I("send", 16) + "开始检索</button>" +
      "</div></div>" +
      '<div id="s-meta"></div>' +
      '<div class="result-list" id="s-results">' +
      UI.emptyState({ icon: "search", title: "输入问题开始检索", text: "检索结果将展示命中文档、分片内容与向量相似度得分。" }) +
      "</div>";

    var libSel = document.getElementById("s-lib");
    libSel.innerHTML = libs.map(function (l) {
      return '<option value="' + esc(l.slug) + '"' + (l.slug === state.lib ? " selected" : "") + ">" + esc(l.name) + "</option>";
    }).join("");

    var qInput = document.getElementById("s-q");
    var resultsBox = document.getElementById("s-results");
    var metaBox = document.getElementById("s-meta");
    var historyBox = document.getElementById("s-history");

    function paintHistory() {
      var h = loadHistory();
      historyBox.innerHTML = h.length
        ? '<span class="small muted nowrap">最近：</span>' + h.map(function (q) {
            return '<button class="btn btn--sm btn--ghost" data-hq="' + esc(q) + '">' + esc(q.length > 18 ? q.slice(0, 18) + "…" : q) + "</button>";
          }).join("")
        : "";
    }
    paintHistory();

    historyBox.addEventListener("click", function (e) {
      var b = e.target.closest("[data-hq]");
      if (b) { qInput.value = b.getAttribute("data-hq"); run(); }
    });

    async function run() {
      var q = qInput.value.trim();
      if (!q) { qInput.focus(); UI.toast({ type: "warning", title: "请输入检索内容" }); return; }
      if (state.searching) return;
      state.searching = true;
      var btn = document.getElementById("s-run");
      btn.disabled = true;
      btn.innerHTML = I("refresh", 16) + "检索中…";
      resultsBox.innerHTML = UI.skeletonRows(3);
      metaBox.innerHTML = "";
      try {
        var res = await VKApi.query(state.lib, q, state.topK);
        var th = state.threshold;
        var list = (res.results || []).filter(function (r) { return (r.score || 0) >= th; });
        pushHistory(q);
        paintHistory();

        metaBox.innerHTML =
          '<div class="row between wrap small muted" style="margin-bottom:12px">' +
          "<span>命中 <b style='color:var(--text)'>" + list.length + "</b> 条" +
          (res.library ? " · 库：" + esc(res.library) : "") +
          (res.elapsed_ms ? " · 耗时 " + res.elapsed_ms + "ms" : "") +
          (res.mode === "demo" ? ' · <span class="badge badge--neutral">演示数据</span>' : "") + "</span>" +
          "<span>score_threshold = " + th.toFixed(2) + "</span></div>";

        var words = q.split(/\s+/).filter(Boolean);
        resultsBox.innerHTML = list.length ? list.map(function (r, i) {
          return '<article class="card result-item">' +
            '<div class="result-item__meta">' +
            '<span class="badge badge--info">#' + (i + 1) + "</span>" +
            '<span class="mono">' + esc(r.document_id || "") + '</span>' +
            '<span>分片 ' + (r.chunk_index != null ? r.chunk_index : "-") + "</span>" +
            (r.rerank_score != null ? '<span class="badge badge--success">rerank ' + r.rerank_score.toFixed(3) + "</span>" : "") +
            '<span style="margin-left:auto">' + UI.scoreBar(r.score) + "</span>" +
            "</div>" +
            '<div class="result-item__title">' + esc(r.title || "未命名文档") + "</div>" +
            '<p class="result-item__text">' + UI.highlight(r.content || "", words) + "</p>" +
            "</article>";
        }).join("") : UI.emptyState({
          icon: "search", title: "没有命中结果",
          text: "尝试降低 score_threshold、换用更贴近业务的关键词，或先摄入相关文档。"
        });
      } catch (e) {
        resultsBox.innerHTML = UI.errorState(e.message);
        var r = resultsBox.querySelector("[data-retry]");
        if (r) r.addEventListener("click", run);
      } finally {
        state.searching = false;
        btn.disabled = false;
        btn.innerHTML = I("send", 16) + "开始检索";
      }
    }

    document.getElementById("s-run").addEventListener("click", run);
    qInput.addEventListener("keydown", function (e) { if (e.key === "Enter") run(); });
    document.getElementById("s-topk").addEventListener("change", function () { state.topK = parseInt(this.value, 10); });
    document.getElementById("s-th").addEventListener("change", function () { state.threshold = parseFloat(this.value); });
    libSel.addEventListener("change", function () { state.lib = this.value; });

    qInput.focus();
  }

  window.Views = window.Views || {};
  Views.search = { render: render, title: "语义检索" };
})();
