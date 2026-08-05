/* settings.js — 系统设置（API 连接 / 外观 / 演示模式） */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;

  async function render(root) {
    var cfg = VKApi.getConfig();

    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>系统设置</h1>" +
      '<p class="lede">配置后端 API 连接；未连通时控制台自动使用本地演示数据，便于离线浏览与联调。</p>' +
      "</div></div>" +
      '<div class="grid grid--2">' +

      '<div class="card"><div class="card__head"><div><div class="card__title">API 连接</div><div class="card__sub">FastAPI 服务地址与 API Key</div></div></div>' +
      '<div class="card__body stack">' +
      '<div class="field"><label for="cfg-url">Base URL</label>' +
      '<input class="input mono" id="cfg-url" value="' + esc(cfg.baseUrl) + '" placeholder="http://localhost:8100">' +
      '<span class="hint">向量知识库服务地址（默认 http://localhost:8100）。</span></div>' +
      '<div class="field"><label for="cfg-key">API Key（可选）</label>' +
      '<input class="input mono" id="cfg-key" type="password" value="' + esc(cfg.apiKey) + '" placeholder="vk_...">' +
      '<span class="hint">仅保存在本机 localStorage，不会上传到任何服务器。</span></div>' +
      '<label class="switch"><input type="checkbox" id="cfg-demo" ' + (VKApi.isDemoForced() ? "checked" : "") + '><span class="track"></span>' +
      '<span><b style="font-size:14px">强制演示模式</b><br><span class="small muted">忽略后端，始终使用内置演示数据。</span></span></label>' +
      '<div class="row"><button class="btn btn--primary" id="cfg-save">' + I("check", 16) + "保存并测试连接</button>" +
      '<span id="cfg-status" class="small muted"></span></div>' +
      "</div></div>" +

      '<div class="stack">' +
      '<div class="card"><div class="card__head"><div><div class="card__title">外观</div><div class="card__sub">主题偏好即时生效并持久化</div></div></div>' +
      '<div class="card__body">' +
      '<div class="row between wrap">' +
      '<div><b style="font-size:14px">界面主题</b><p class="small muted">跟随系统，或手动指定。</p></div>' +
      '<div class="seg" id="theme-seg">' +
      '<button data-theme-val="system">跟随系统</button>' +
      '<button data-theme-val="light">浅色</button>' +
      '<button data-theme-val="dark">深色</button>' +
      "</div></div></div></div>" +

      '<div class="card"><div class="card__head"><div><div class="card__title">快捷键</div><div class="card__sub">提升键盘操作效率</div></div></div>' +
      '<div class="card__body stack-sm small">' +
      '<div class="row between"><span>全局检索</span><span class="mono muted">Alt + 2</span></div>' +
      '<div class="row between"><span>知识库</span><span class="mono muted">Alt + 1</span></div>' +
      '<div class="row between"><span>文档管理</span><span class="mono muted">Alt + 3</span></div>' +
      '<div class="row between"><span>文档摄入</span><span class="mono muted">Alt + 4</span></div>' +
      '<div class="row between"><span>任务队列</span><span class="mono muted">Alt + 5</span></div>' +
      '<div class="row between"><span>收起 / 展开侧栏（移动端）</span><span class="mono muted">Esc 关闭</span></div>' +
      "</div></div>" +

      '<div class="card"><div class="card__head"><div><div class="card__title">关于</div></div></div>' +
      '<div class="card__body small muted stack-sm">' +
      '<p>Vector KB Console · 向量知识检索底座管理界面</p>' +
      '<p>架构：FastAPI + SQLAlchemy 2.0 · Qdrant · PostgreSQL 队列 · bge-m3 Embedding · Casbin 权限</p>' +
      '<p class="mono" style="font-size:11.5px">Dify external-knowledge-base compatible</p>' +
      "</div></div>" +
      "</div></div>";

    /* theme seg */
    var themeSeg = document.getElementById("theme-seg");
    function paintThemeSeg() {
      var cur = window.ThemePref.get();
      themeSeg.querySelectorAll("[data-theme-val]").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-theme-val") === cur ? "true" : "false");
      });
    }
    paintThemeSeg();
    themeSeg.addEventListener("click", function (e) {
      var b = e.target.closest("[data-theme-val]");
      if (!b) return;
      window.ThemePref.set(b.getAttribute("data-theme-val"));
      paintThemeSeg();
    });

    /* save & test */
    document.getElementById("cfg-save").addEventListener("click", async function () {
      var btn = this;
      var status = document.getElementById("cfg-status");
      btn.disabled = true;
      btn.innerHTML = I("refresh", 16) + "测试连接中…";
      status.textContent = "";
      try {
        var ok = await VKApi.saveConfig({
          baseUrl: document.getElementById("cfg-url").value.trim(),
          apiKey: document.getElementById("cfg-key").value.trim()
        });
        if (ok) {
          status.innerHTML = '<span style="color:var(--success)">已连接后端服务</span>';
          UI.toast({ title: "连接成功", text: "已切换到真实后端数据" });
        } else {
          status.innerHTML = '<span style="color:var(--warning)">未连通，已回退演示数据</span>';
          UI.toast({ type: "warning", title: "无法连接后端", text: "已保存配置，当前使用演示数据" });
        }
      } catch (e) {
        status.innerHTML = '<span style="color:var(--danger)">' + esc(e.message) + "</span>";
        UI.toast({ type: "error", title: "配置无效", text: e.message });
      } finally {
        btn.disabled = false;
        btn.innerHTML = I("check", 16) + "保存并测试连接";
      }
    });

    document.getElementById("cfg-demo").addEventListener("change", function () {
      VKApi.setDemoForced(this.checked);
      UI.toast({ title: this.checked ? "已开启强制演示模式" : "已关闭强制演示模式" });
    });
  }

  window.Views = window.Views || {};
  Views.settings = { render: render, title: "系统设置" };
})();
