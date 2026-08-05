/* libraries.js — 知识库管理 */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;

  var SLUG_RE = /^[a-z0-9][a-z0-9-]{1,40}$/;

  function libCard(lib) {
    return '<article class="card lib-card" data-slug="' + esc(lib.slug) + '">' +
      '<div class="lib-card__top">' +
      '<span class="lib-card__icon">' + I("library", 21) + "</span>" +
      '<div style="min-width:0"><div class="lib-card__name clamp-2">' + esc(lib.name) + '</div>' +
      '<div class="lib-card__slug">' + esc(lib.slug) + "</div></div>" +
      '<div style="margin-left:auto">' + UI.badge(lib.status || "active") + "</div>" +
      "</div>" +
      '<p class="lib-card__desc">' + esc(lib.description || "暂无描述") + "</p>" +
      '<div class="lib-card__stats">' +
      '<div class="lib-card__stat"><b>' + UI.fmtNum(lib.docs || 0) + "</b><span>文档</span></div>" +
      '<div class="lib-card__stat"><b>' + UI.fmtNum(lib.chunks || 0) + "</b><span>分片</span></div>" +
      '<div class="lib-card__stat"><b>' + esc(lib.model || "bge-m3") + "</b><span>模型</span></div>" +
      '<div class="lib-card__stat" style="margin-left:auto;text-align:right"><b>' + UI.fmtTime(lib.updated) + "</b><span>更新</span></div>" +
      "</div>" +
      '<div class="lib-card__foot">' +
      '<a class="btn btn--sm btn--soft" href="#/documents?lib=' + encodeURIComponent(lib.slug) + '">' + I("documents", 14) + "文档</a>" +
      '<a class="btn btn--sm btn--ghost" href="#/search?lib=' + encodeURIComponent(lib.slug) + '">' + I("search", 14) + "检索</a>" +
      '<a class="btn btn--sm btn--ghost" href="#/import?lib=' + encodeURIComponent(lib.slug) + '">' + I("upload", 14) + "摄入</a>" +
      "</div></article>";
  }

  function createLibraryModal(onCreated) {
    UI.modal({
      title: "新建知识库",
      body:
        '<div class="field"><label for="lib-name">名称</label>' +
        '<input class="input" id="lib-name" placeholder="例如：人力资源政策库" maxlength="40"></div>' +
        '<div class="field"><label for="lib-slug">Slug（唯一标识，小写字母/数字/连字符）</label>' +
        '<input class="input mono" id="lib-slug" placeholder="例如：hr-policy" maxlength="40">' +
        '<span class="hint">创建后不可修改，将映射到独立 Qdrant collection。</span>' +
        '<span class="error-text">Slug 格式不正确（小写字母开头，可含数字与连字符，2-40 位）</span></div>' +
        '<div class="field"><label for="lib-desc">描述</label>' +
        '<textarea class="textarea" id="lib-desc" placeholder="这个库存放什么内容、给谁用？" maxlength="200"></textarea></div>',
      footer:
        '<button class="btn btn--ghost" data-close>取消</button>' +
        '<button class="btn btn--primary" data-submit>' + I("plus", 15) + "创建知识库</button>",
      onMount: function (el, close) {
        var nameI = el.querySelector("#lib-name");
        var slugI = el.querySelector("#lib-slug");
        var descI = el.querySelector("#lib-desc");
        var btn = el.querySelector("[data-submit]");
        var slugTouched = false;

        slugI.addEventListener("input", function () { slugTouched = true; });
        nameI.addEventListener("input", function () {
          if (!slugTouched) {
            slugI.value = nameI.value.trim().toLowerCase()
              .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
              .replace(/[\u4e00-\u9fa5]/g, "")
              .replace(/^-+|-+$/g, "").slice(0, 40);
          }
        });

        btn.addEventListener("click", async function () {
          var field = slugI.closest(".field");
          field.classList.remove("has-error");
          if (!nameI.value.trim()) { nameI.focus(); return; }
          if (!SLUG_RE.test(slugI.value.trim())) {
            field.classList.add("has-error"); slugI.focus(); return;
          }
          btn.disabled = true;
          btn.innerHTML = I("refresh", 15) + "创建中…";
          try {
            await VKApi.createLibrary({
              name: nameI.value.trim(),
              slug: slugI.value.trim(),
              description: descI.value.trim()
            });
            UI.toast({ title: "知识库已创建", text: nameI.value.trim() + " · collection 同步建立" });
            close();
            onCreated();
          } catch (e) {
            UI.toast({ type: "error", title: "创建失败", text: e.message });
            btn.disabled = false;
            btn.innerHTML = I("plus", 15) + "创建知识库";
          }
        });
      }
    });
  }

  async function render(root) {
    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>知识库</h1>" +
      '<p class="lede">每个库物理隔离（独立 Qdrant collection），按 (用户, 库, 动作) 三元组授权。</p>' +
      "</div>" +
      '<div class="page-head__actions">' +
      '<div class="search-box"><span class="icon">' + I("search", 16) + '</span>' +
      '<input class="input" id="lib-filter" placeholder="筛选知识库…" aria-label="筛选知识库"></div>' +
      '<button class="btn btn--primary" id="lib-create">' + I("plus", 16) + '<span class="btn-text">新建知识库</span></button>' +
      "</div></div>" +
      '<div class="grid grid--libs" id="lib-grid">' + UI.skeletonCards(6) + "</div>";

    var grid = document.getElementById("lib-grid");
    var all = [];

    async function load() {
      grid.innerHTML = UI.skeletonCards(6);
      try {
        all = await VKApi.listLibraries();
        paint(document.getElementById("lib-filter").value);
      } catch (e) {
        grid.innerHTML = UI.errorState(e.message);
        grid.querySelector("[data-retry]").addEventListener("click", load);
      }
    }

    function paint(kw) {
      var list = all;
      if (kw && kw.trim()) {
        var k = kw.trim().toLowerCase();
        list = all.filter(function (l) {
          return l.name.toLowerCase().indexOf(k) >= 0 || l.slug.toLowerCase().indexOf(k) >= 0;
        });
      }
      grid.innerHTML = list.length
        ? list.map(libCard).join("")
        : UI.emptyState({
            icon: "library",
            title: kw ? "没有匹配的知识库" : "还没有知识库",
            text: kw ? "换个关键词试试。" : "创建第一个知识库，开始摄入文档并对外提供检索。",
            action: kw ? "" : '<button class="btn btn--primary" id="empty-create">' + I("plus", 15) + "新建知识库</button>"
          });
      var emptyBtn = document.getElementById("empty-create");
      if (emptyBtn) emptyBtn.addEventListener("click", function () { createLibraryModal(load); });
    }

    document.getElementById("lib-filter").addEventListener("input", function () { paint(this.value); });
    document.getElementById("lib-create").addEventListener("click", function () { createLibraryModal(load); });

    load();
  }

  window.Views = window.Views || {};
  Views.libraries = { render: render, title: "知识库" };
})();
