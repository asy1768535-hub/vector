/* documents.js — 文档管理（筛选 / 分页 / 删除 / 详情抽屉） */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;
  var PAGE_SIZE = 8;

  async function render(root, params) {
    var state = { lib: params.get("lib") || "", status: "", q: "", page: 1, docs: [] };

    var libs = [];
    try { libs = await VKApi.listLibraries(); } catch (e) { /* handled in load */ }
    if (!state.lib && libs.length) state.lib = libs[0].slug;

    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>文档管理</h1>" +
      '<p class="lede">文档身份由 external_id / content_hash 决定；删除为 tombstone + outbox 异步物理清理。</p>' +
      "</div>" +
      '<div class="page-head__actions">' +
      '<a class="btn btn--primary" id="doc-goto-import" href="#/import">' + I("upload", 16) + '<span class="btn-text">摄入新文档</span></a>' +
      "</div></div>" +
      '<div class="toolbar">' +
      '<select class="select" id="doc-lib" aria-label="选择知识库"></select>' +
      '<select class="select" id="doc-status" aria-label="按状态筛选">' +
      '<option value="">全部状态</option><option value="done">已完成</option>' +
      '<option value="processing">处理中</option><option value="pending">排队中</option><option value="failed">失败</option>' +
      "</select>" +
      '<div class="search-box grow"><span class="icon">' + I("search", 16) + '</span>' +
      '<input class="input" id="doc-q" placeholder="搜索标题 / external_id…" aria-label="搜索文档"></div>' +
      "</div>" +
      '<div class="table-wrap"><table class="table" aria-label="文档列表">' +
      "<thead><tr><th>文档</th><th>状态</th><th>分片</th><th>大小</th><th>修订</th><th>更新时间</th><th></th></tr></thead>" +
      '<tbody id="doc-tbody"></tbody></table>' +
      '<div class="pagination" id="doc-pager"></div></div>';

    var libSel = document.getElementById("doc-lib");
    libSel.innerHTML = libs.map(function (l) {
      return '<option value="' + esc(l.slug) + '"' + (l.slug === state.lib ? " selected" : "") + ">" + esc(l.name) + "（" + esc(l.slug) + "）</option>";
    }).join("");
    if (!libs.length) {
      document.getElementById("doc-tbody").innerHTML = '<tr><td colspan="7">' + UI.emptyState({ icon: "library", title: "暂无知识库", text: "请先创建一个知识库。", action: '<a class="btn btn--primary" href="#/libraries">去创建</a>' }) + "</td></tr>";
      return;
    }
    document.getElementById("doc-goto-import").href = "#/import?lib=" + encodeURIComponent(state.lib);

    var tbody = document.getElementById("doc-tbody");
    var pager = document.getElementById("doc-pager");

    async function load() {
      tbody.innerHTML = '<tr><td colspan="7">' + UI.skeletonRows(5) + "</td></tr>";
      try {
        state.docs = await VKApi.listDocuments(state.lib, { status: state.status, q: state.q });
        state.page = 1;
        paint();
      } catch (e) {
        tbody.innerHTML = '<tr><td colspan="7">' + UI.errorState(e.message) + "</td></tr>";
        var r = tbody.querySelector("[data-retry]");
        if (r) r.addEventListener("click", load);
      }
    }

    function paint() {
      var total = state.docs.length;
      var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      if (state.page > pages) state.page = pages;
      var slice = state.docs.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);

      tbody.innerHTML = slice.length ? slice.map(function (d) {
        return "<tr>" +
          '<td><div class="row"><span class="upload-item__icon">' + I("file", 17) + "</span>" +
          '<div style="min-width:0"><div class="cell-main">' + esc(d.title) + '</div>' +
          '<div class="cell-sub mono">' + esc(d.external_id || d.id) + "</div></div></div></td>" +
          "<td>" + UI.badge(d.status) + "</td>" +
          '<td class="num">' + UI.fmtNum(d.chunks) + "</td>" +
          '<td class="num">' + UI.fmtBytes(d.size) + "</td>" +
          '<td class="num">r' + (d.revision || 1) + "</td>" +
          '<td class="nowrap small muted">' + UI.fmtTime(d.updated_at) + "</td>" +
          '<td class="nowrap">' +
          '<button class="icon-btn btn--sm" data-detail="' + esc(d.id) + '" aria-label="查看详情">' + I("info", 15) + "</button> " +
          '<button class="icon-btn btn--sm" data-del="' + esc(d.id) + '" aria-label="删除文档" style="color:var(--danger)">' + I("trash", 15) + "</button>" +
          "</td></tr>";
      }).join("") : '<tr><td colspan="7">' + UI.emptyState({ icon: "documents", title: "没有匹配的文档", text: "调整筛选条件，或摄入新文档。" }) + "</td></tr>";

      // pager
      var btns = '<span class="info">共 ' + total + " 篇 · 第 " + state.page + "/" + pages + " 页</span>";
      btns += '<button class="page-btn" data-pg="prev" ' + (state.page <= 1 ? "disabled" : "") + ' aria-label="上一页">' + I("chevronL", 14) + "</button>";
      for (var p = 1; p <= pages; p++) {
        btns += '<button class="page-btn" data-pg="' + p + '" aria-current="' + (p === state.page) + '">' + p + "</button>";
      }
      btns += '<button class="page-btn" data-pg="next" ' + (state.page >= pages ? "disabled" : "") + ' aria-label="下一页">' + I("chevronR", 14) + "</button>";
      pager.innerHTML = btns;
    }

    // events
    libSel.addEventListener("change", function () {
      state.lib = this.value;
      document.getElementById("doc-goto-import").href = "#/import?lib=" + encodeURIComponent(state.lib);
      load();
    });
    document.getElementById("doc-status").addEventListener("change", function () { state.status = this.value; load(); });
    var qTimer = null;
    document.getElementById("doc-q").addEventListener("input", function () {
      var v = this.value;
      clearTimeout(qTimer);
      qTimer = setTimeout(function () { state.q = v; load(); }, 300);
    });
    pager.addEventListener("click", function (e) {
      var b = e.target.closest("[data-pg]");
      if (!b || b.disabled) return;
      var v = b.getAttribute("data-pg");
      var pages = Math.max(1, Math.ceil(state.docs.length / PAGE_SIZE));
      if (v === "prev") state.page = Math.max(1, state.page - 1);
      else if (v === "next") state.page = Math.min(pages, state.page + 1);
      else state.page = parseInt(v, 10);
      paint();
    });
    tbody.addEventListener("click", async function (e) {
      var del = e.target.closest("[data-del]");
      var det = e.target.closest("[data-detail]");
      if (det) {
        var d = state.docs.find(function (x) { return x.id === det.getAttribute("data-detail"); });
        if (!d) return;
        UI.modal({
          title: "文档详情",
          body:
            '<div class="stack-sm">' +
            '<div class="row between"><span class="muted small">标题</span><b>' + esc(d.title) + "</b></div><hr class='divider'>" +
            '<div class="row between"><span class="muted small">External ID</span><span class="mono small">' + esc(d.external_id || "-") + "</span></div>" +
            '<div class="row between"><span class="muted small">状态</span>' + UI.badge(d.status) + "</div>" +
            '<div class="row between"><span class="muted small">分片数</span><b>' + UI.fmtNum(d.chunks) + "</b></div>" +
            '<div class="row between"><span class="muted small">文件大小</span><b>' + UI.fmtBytes(d.size) + "</b></div>" +
            '<div class="row between"><span class="muted small">当前修订</span><b>r' + (d.revision || 1) + "</b></div>" +
            '<div class="row between"><span class="muted small">更新时间</span><b>' + UI.fmtTime(d.updated_at) + "</b></div>" +
            '<hr class="divider"><p class="small muted">更新该文档会触发 reingest（revision +1），旧向量按代际过滤立即不可见；删除走 tombstone + outbox，Cleanup Worker 异步幂等物理清理。</p>' +
            "</div>"
        });
      }
      if (del) {
        var id = del.getAttribute("data-del");
        var doc = state.docs.find(function (x) { return x.id === id; });
        var ok = await UI.confirm({
          title: "删除文档",
          text: "确定删除「" + (doc ? doc.title : id) + "」吗？删除后检索立即可见失效，Qdrant 物理清理由 Cleanup Worker 异步完成。",
          okText: "删除",
          danger: true
        });
        if (!ok) return;
        try {
          await VKApi.deleteDocument(state.lib, id);
          UI.toast({ title: "已删除", text: "tombstone 已写入，物理清理入队" });
          load();
        } catch (err) {
          UI.toast({ type: "error", title: "删除失败", text: err.message });
        }
      }
    });

    load();
  }

  window.Views = window.Views || {};
  Views.documents = { render: render, title: "文档管理" };
})();
