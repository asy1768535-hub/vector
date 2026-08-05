/* jobs.js — 嵌入任务队列（状态筛选 / 自动轮询 / 失败重试） */
(function () {
  "use strict";
  var I = UI.icon, esc = UI.esc;

  async function render(root) {
    var state = { status: "", jobs: [], timer: null };

    root.innerHTML =
      '<div class="page-head"><div>' +
      "<h1>任务队列</h1>" +
      '<p class="lede">PostgreSQL 队列（FOR UPDATE SKIP LOCKED）驱动的嵌入任务，每 5 秒自动刷新。</p>' +
      "</div>" +
      '<div class="page-head__actions">' +
      '<button class="btn btn--ghost" id="jobs-refresh">' + I("refresh", 16) + '<span class="btn-text">手动刷新</span></button>' +
      "</div></div>" +
      '<div class="toolbar">' +
      '<div class="seg" id="jobs-seg" role="tablist" aria-label="按状态筛选">' +
      '<button data-st="" aria-pressed="true">全部</button>' +
      '<button data-st="processing" aria-pressed="false">处理中</button>' +
      '<button data-st="pending" aria-pressed="false">排队中</button>' +
      '<button data-st="done" aria-pressed="false">已完成</button>' +
      '<button data-st="failed" aria-pressed="false">失败</button>' +
      "</div>" +
      '<span class="small muted" id="jobs-updated" style="margin-left:auto"></span>' +
      "</div>" +
      '<div class="card" id="jobs-list">' + UI.skeletonRows(5) + "</div>";

    var listBox = document.getElementById("jobs-list");
    var updatedBox = document.getElementById("jobs-updated");

    async function load(silent) {
      if (!silent) listBox.innerHTML = UI.skeletonRows(5);
      try {
        state.jobs = await VKApi.listJobs({ status: state.status });
        paint();
        updatedBox.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
      } catch (e) {
        if (!silent) {
          listBox.innerHTML = UI.errorState(e.message);
          var r = listBox.querySelector("[data-retry]");
          if (r) r.addEventListener("click", function () { load(); });
        }
      }
    }

    function paint() {
      if (!state.jobs.length) {
        listBox.innerHTML = UI.emptyState({
          icon: "jobs", title: "队列已清空",
          text: "当前没有" + (state.status ? "该状态的" : "") + "嵌入任务。",
          action: '<a class="btn btn--primary" href="#/import">' + I("upload", 15) + "去摄入文档</a>"
        });
        return;
      }
      listBox.innerHTML = '<div class="card__body stack-sm" style="padding:14px 20px">' + state.jobs.map(function (j) {
        var iconCls = "job-row__icon--" + (j.status === "done" ? "done" : j.status === "failed" ? "failed" : j.status === "processing" ? "processing" : "pending");
        var iconName = j.status === "done" ? "check" : j.status === "failed" ? "error" : j.status === "processing" ? "refresh" : "clock";
        return '<div class="job-row">' +
          '<span class="job-row__icon ' + iconCls + '">' + I(iconName, 17) + "</span>" +
          '<div class="job-row__body">' +
          '<div class="job-row__title">' + esc(j.document) + " " + UI.badge(j.status) + "</div>" +
          '<div class="job-row__meta">' + esc(j.libraryName || j.library) + ' · <span class="mono">' + esc(j.id) + "</span> · " + UI.fmtTime(j.created_at) + "</div>" +
          (j.status === "processing" ? '<div class="progress progress--striped" style="margin-top:8px;max-width:320px"><i style="width:' + (j.progress || 0) + '%"></i></div>' : "") +
          (j.error ? '<div class="small" style="color:var(--danger);margin-top:6px">' + esc(j.error) + "</div>" : "") +
          "</div>" +
          (j.status === "failed" ? '<button class="btn btn--sm btn--ghost" data-retry-job="' + esc(j.id) + '">' + I("retry", 14) + "重试</button>" : "") +
          "</div>";
      }).join("") + "</div>";
    }

    listBox.addEventListener("click", async function (e) {
      var b = e.target.closest("[data-retry-job]");
      if (!b) return;
      b.disabled = true;
      try {
        await VKApi.retryJob(b.getAttribute("data-retry-job"));
        UI.toast({ title: "已重置为 pending", text: "Worker 将重新消费该任务" });
        load(true);
      } catch (err) {
        UI.toast({ type: "error", title: "重试失败", text: err.message });
        b.disabled = false;
      }
    });

    document.getElementById("jobs-seg").addEventListener("click", function (e) {
      var b = e.target.closest("[data-st]");
      if (!b) return;
      state.status = b.getAttribute("data-st");
      this.querySelectorAll("[data-st]").forEach(function (x) {
        x.setAttribute("aria-pressed", x === b ? "true" : "false");
      });
      load();
    });
    document.getElementById("jobs-refresh").addEventListener("click", function () { load(); });

    // 自动轮询（离开视图时由 app.js 调用 cleanup）
    state.timer = setInterval(function () { load(true); }, 5000);
    this.cleanup = function () { clearInterval(state.timer); };

    load();
  }

  window.Views = window.Views || {};
  Views.jobs = { render: render, title: "任务队列" };
})();
