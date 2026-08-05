/* import.js — 文档摄入（拖拽上传 + 纯文本摄入） */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;
  var ALLOWED = [".txt", ".md", ".markdown", ".json", ".csv", ".docx", ".xlsx", ".pdf"];
  var MAX_SIZE = 50 * 1024 * 1024;

  function extOf(name) {
    var i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i).toLowerCase() : "";
  }

  async function render(root, params) {
    var state = { lib: params.get("lib") || "", tab: "file", queue: [], uploading: false };
    var libs = [];
    try { libs = await VKApi.listLibraries(); } catch (e) {}
    if (!state.lib && libs.length) state.lib = libs[0].slug;

    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>文档摄入</h1>" +
      '<p class="lede">支持 TXT / MD / JSON / CSV / DOCX / XLSX / PDF，异步转向量，任务可监控可重试。</p>' +
      "</div></div>" +
      '<div class="toolbar">' +
      '<select class="select" id="i-lib" aria-label="目标知识库"></select>' +
      '<div class="seg" role="tablist" aria-label="摄入方式">' +
      '<button role="tab" data-tab="file" aria-pressed="true">' + I("upload", 15) + " 文件上传</button>" +
      '<button role="tab" data-tab="text" aria-pressed="false">' + I("documents", 15) + " 纯文本摄入</button>" +
      "</div></div>" +

      '<div id="tab-file">' +
      '<div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="上传文件，点击选择或拖拽文件到此处">' +
      '<div class="dropzone__icon">' + I("upload", 26) + "</div>" +
      "<h3>拖拽文件到此处，或点击选择</h3>" +
      "<p>单文件最大 50MB · 扫描版 PDF 需目标库开启 OCR</p>" +
      '<div class="formats">' + ALLOWED.map(function (e) { return '<span class="badge badge--neutral">' + e + "</span>"; }).join("") + "</div>" +
      '<input type="file" id="file-input" hidden multiple accept="' + ALLOWED.join(",") + '">' +
      "</div>" +
      '<div id="upload-queue" style="margin-top:16px"></div>' +
      '<div class="row" style="margin-top:16px;justify-content:flex-end">' +
      '<button class="btn btn--primary" id="start-upload" disabled>' + I("send", 16) + "开始摄入</button>" +
      "</div></div>" +

      '<div id="tab-text" hidden>' +
      '<div class="card card--pad stack">' +
      '<div class="field"><label for="t-title">文档标题</label><input class="input" id="t-title" placeholder="例如：差旅报销常见问题"></div>' +
      '<div class="field"><label for="t-extid">External ID（可选）</label><input class="input mono" id="t-extid" placeholder="例如：faq-travel-001"><span class="hint">填写后按 (库, external_id) upsert；不填则按 content_hash 去重。</span></div>' +
      '<div class="field"><label for="t-content">正文内容</label><textarea class="textarea" id="t-content" rows="9" placeholder="粘贴需要摄入的文本内容…"></textarea></div>' +
      '<div class="row" style="justify-content:flex-end"><button class="btn btn--primary" id="t-submit">' + I("send", 16) + "提交摄入</button></div>" +
      "</div></div>";

    var libSel = document.getElementById("i-lib");
    libSel.innerHTML = libs.map(function (l) {
      return '<option value="' + esc(l.slug) + '"' + (l.slug === state.lib ? " selected" : "") + ">" + esc(l.name) + "</option>";
    }).join("");
    libSel.addEventListener("change", function () { state.lib = this.value; });

    /* ---- tabs ---- */
    var seg = root.querySelector(".seg");
    seg.addEventListener("click", function (e) {
      var b = e.target.closest("[data-tab]");
      if (!b) return;
      state.tab = b.getAttribute("data-tab");
      seg.querySelectorAll("[data-tab]").forEach(function (x) {
        x.setAttribute("aria-pressed", x === b ? "true" : "false");
      });
      document.getElementById("tab-file").hidden = state.tab !== "file";
      document.getElementById("tab-text").hidden = state.tab !== "text";
    });

    /* ---- dropzone ---- */
    var dz = document.getElementById("dropzone");
    var fi = document.getElementById("file-input");
    var queueBox = document.getElementById("upload-queue");
    var startBtn = document.getElementById("start-upload");

    function addFiles(fileList) {
      var added = 0;
      Array.prototype.forEach.call(fileList, function (f) {
        var ext = extOf(f.name);
        if (ALLOWED.indexOf(ext) < 0) {
          UI.toast({ type: "warning", title: "不支持的格式", text: f.name + "（" + (ext || "无扩展名") + "）" });
          return;
        }
        if (f.size > MAX_SIZE) {
          UI.toast({ type: "warning", title: "文件过大", text: f.name + " 超过 50MB 限制" });
          return;
        }
        if (state.queue.some(function (q) { return q.file.name === f.name && q.file.size === f.size; })) return;
        state.queue.push({ file: f, progress: 0, status: "ready" });
        added++;
      });
      if (added) paintQueue();
    }

    function paintQueue() {
      startBtn.disabled = !state.queue.length || state.uploading;
      queueBox.innerHTML = state.queue.length ? state.queue.map(function (q, i) {
        var statusHtml = q.status === "done"
          ? '<span class="badge badge--success"><i class="dot"></i>已入队</span>'
          : q.status === "error"
            ? '<span class="badge badge--danger"><i class="dot"></i>失败</span>'
            : q.status === "uploading"
              ? '<span class="small muted">' + Math.round(q.progress * 100) + "%</span>"
              : '<button class="icon-btn btn--sm" data-rm="' + i + '" aria-label="移除">' + I("close", 14) + "</button>";
        return '<div class="upload-item">' +
          '<span class="upload-item__icon">' + I("file", 17) + "</span>" +
          '<div class="upload-item__body">' +
          '<div class="upload-item__name">' + esc(q.file.name) + "</div>" +
          '<div class="upload-item__meta">' + UI.fmtBytes(q.file.size) + " · " + extOf(q.file.name) + "</div>" +
          (q.status === "uploading" ? '<div class="progress progress--striped"><i style="width:' + Math.round(q.progress * 100) + '%"></i></div>' : "") +
          "</div>" + statusHtml + "</div>";
      }).join("") : "";
    }

    queueBox.addEventListener("click", function (e) {
      var rm = e.target.closest("[data-rm]");
      if (rm && !state.uploading) {
        state.queue.splice(parseInt(rm.getAttribute("data-rm"), 10), 1);
        paintQueue();
      }
    });

    dz.addEventListener("click", function () { fi.click(); });
    dz.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } });
    fi.addEventListener("change", function () { addFiles(fi.files); fi.value = ""; });
    ["dragenter", "dragover"].forEach(function (evt) {
      dz.addEventListener(evt, function (e) { e.preventDefault(); dz.classList.add("is-dragover"); });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      dz.addEventListener(evt, function (e) { e.preventDefault(); dz.classList.remove("is-dragover"); });
    });
    dz.addEventListener("drop", function (e) { addFiles(e.dataTransfer.files); });

    startBtn.addEventListener("click", async function () {
      if (!state.queue.length || state.uploading) return;
      state.uploading = true;
      startBtn.disabled = true;
      startBtn.innerHTML = I("refresh", 16) + "摄入中…";
      var files = state.queue.map(function (q) { return q.file; });
      state.queue.forEach(function (q) { q.status = "uploading"; q.progress = 0; });
      paintQueue();
      try {
        await VKApi.uploadFiles(state.lib, files, function (idx, total, frac) {
          if (typeof frac === "number" && state.queue[idx]) {
            state.queue[idx].progress = frac;
            if (frac >= 1) state.queue[idx].status = "done";
          } else if (state.queue[idx - 1]) {
            state.queue[idx - 1].status = "done";
          }
          paintQueue();
        });
        state.queue.forEach(function (q) { if (q.status === "uploading") q.status = "done"; });
        paintQueue();
        UI.toast({ title: "摄入任务已创建", text: files.length + " 个文件进入嵌入队列，可在「任务队列」查看进度" });
        setTimeout(function () { location.hash = "#/jobs"; }, 900);
      } catch (e) {
        UI.toast({ type: "error", title: "摄入失败", text: e.message });
      } finally {
        state.uploading = false;
        startBtn.innerHTML = I("send", 16) + "开始摄入";
        paintQueue();
      }
    });

    /* ---- text ingest ---- */
    document.getElementById("t-submit").addEventListener("click", async function () {
      var title = document.getElementById("t-title").value.trim();
      var extid = document.getElementById("t-extid").value.trim();
      var content = document.getElementById("t-content").value.trim();
      if (!title) { UI.toast({ type: "warning", title: "请填写文档标题" }); return; }
      if (content.length < 10) { UI.toast({ type: "warning", title: "正文太短", text: "至少 10 个字符才有检索价值" }); return; }
      this.disabled = true;
      var self = this;
      self.innerHTML = I("refresh", 16) + "提交中…";
      try {
        var payload = { name: title, text: content };
        if (extid) payload.external_id = extid;
        await VKApi.ingestText(state.lib, payload);
        UI.toast({ title: "摄入任务已创建", text: "「" + title + "」进入嵌入队列" });
        document.getElementById("t-title").value = "";
        document.getElementById("t-extid").value = "";
        document.getElementById("t-content").value = "";
      } catch (e) {
        UI.toast({ type: "error", title: "提交失败", text: e.message });
      } finally {
        self.disabled = false;
        self.innerHTML = I("send", 16) + "提交摄入";
      }
    });
  }

  window.Views = window.Views || {};
  Views.import = { render: render, title: "文档摄入" };
})();
