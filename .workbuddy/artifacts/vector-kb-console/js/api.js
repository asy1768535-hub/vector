/* ============================================================
   api.js — API 层：真实后端优先，失败回退到本地 Mock 数据
   在「系统设置」页可配置 Base URL 与 API Key（localStorage 持久化）
   ============================================================ */
(function () {
  "use strict";

  var DEFAULT_BASE = "http://localhost:8100";
  var LS_KEY = "vkb_console_config";

  /* ---------------- config ---------------- */
  function loadConfig() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (raw) {
        var c = JSON.parse(raw);
        return { baseUrl: c.baseUrl || DEFAULT_BASE, apiKey: c.apiKey || "" };
      }
    } catch (e) { /* ignore */ }
    return { baseUrl: DEFAULT_BASE, apiKey: "" };
  }
  function saveConfig(cfg) {
    localStorage.setItem(LS_KEY, JSON.stringify(cfg));
  }

  var config = loadConfig();
  var forceDemo = false; // 设置页可强制演示模式

  /* ---------------- store (pub/sub) ---------------- */
  var listeners = {};
  var store = {
    on: function (evt, fn) {
      (listeners[evt] = listeners[evt] || []).push(fn);
      return function () {
        listeners[evt] = (listeners[evt] || []).filter(function (f) { return f !== fn; });
      };
    },
    emit: function (evt, data) {
      (listeners[evt] || []).forEach(function (fn) { try { fn(data); } catch (e) { console.error(e); } });
    }
  };

  /* ---------------- mock data ---------------- */
  var LIB_COLORS = ["#0e9384", "#1570ef", "#dc6803", "#7a5af8", "#d444f1", "#12805c"];
  var mock = {
    libraries: [
      { slug: "hr-policy", name: "人力资源政策库", description: "公司人事制度、考勤、薪酬福利与员工手册相关文档，供 HR 助手与全员问答使用。", docs: 1284, chunks: 38912, model: "bge-m3", updated: "2026-07-20T16:42:00+08:00", status: "active" },
      { slug: "tech-docs", name: "研发技术文档", description: "后端架构、接口规范、部署运维手册，覆盖 FastAPI / Qdrant / PostgreSQL 技术栈。", docs: 856, chunks: 27140, model: "bge-m3", updated: "2026-07-21T08:15:00+08:00", status: "active" },
      { slug: "finance-rules", name: "财务制度汇编", description: "报销流程、预算管理、采购审批等财务制度文件，含扫描件 OCR 文本。", docs: 342, chunks: 12075, model: "bge-m3", updated: "2026-07-18T11:03:00+08:00", status: "active" },
      { slug: "legal-contracts", name: "合同法务知识库", description: "合同模板、法务审核要点与历史判例摘要，权限受控，仅法务部门可见。", docs: 517, chunks: 15833, model: "bge-m3", updated: "2026-07-15T09:27:00+08:00", status: "active" },
      { slug: "product-faq", name: "产品 FAQ", description: "面向客服与销售的产品常见问题解答，按版本归档，支持 Dify 外部知识库调用。", docs: 210, chunks: 5430, model: "bge-m3", updated: "2026-07-21T07:52:00+08:00", status: "rebuilding" },
      { slug: "ops-runbook", name: "运维值班手册", description: "告警处置流程、值班记录模板与应急预案，Cleanup / Embedding Worker 运维相关。", docs: 96, chunks: 2861, model: "bge-m3", updated: "2026-07-10T14:20:00+08:00", status: "active" }
    ],
    docsByLib: {},
    jobs: [],
    queries: []
  };

  var DOC_STATUSES = ["done", "processing", "pending", "failed"];
  var SAMPLE_TITLES = {
    "hr-policy": ["员工考勤与休假管理办法 v3.2", "薪酬结构调整说明（2026）", "新员工入职指引", "异地办公申请流程", "年度绩效考核方案", "员工手册（2026 修订版）", "商业保险福利说明", "离职交接清单模板"],
    "tech-docs": ["向量检索服务架构设计 v0.1", "Qdrant Collection 规划规范", "API 参考手册（Dify 兼容）", "Embedding Worker 部署手册", "数据库迁移指引 0010", "内部试运行上线清单", "Casbin 权限模型说明", "检索评测脚本使用说明"],
    "finance-rules": ["差旅费报销细则", "预算编制与执行管理办法", "采购审批权限表", "发票合规审核要点", "固定资产管理制度", "费用报销系统操作手册"],
    "legal-contracts": ["软件采购合同模板 v2", "NDA 保密协议审核要点", "供应商合同风险清单", "劳动合同解除判例摘要", "数据合规协议（DPA）模板"],
    "product-faq": ["如何配置外部知识库 API Key？", "扫描 PDF 无法检索怎么办？", "支持哪些文件格式？", "检索结果分数如何理解？", "文档更新后多久生效？"],
    "ops-runbook": ["Embedding 队列积压处置", "Cleanup Worker 告警响应", "Qdrant 磁盘容量预案", "夜间值班记录模板"]
  };
  var SAMPLE_EXTS = [".pdf", ".docx", ".xlsx", ".md", ".txt", ".csv"];

  function seededRand(seed) {
    var s = seed;
    return function () {
      s = (s * 9301 + 49297) % 233280;
      return s / 233280;
    };
  }

  function buildMockDocs() {
    mock.libraries.forEach(function (lib, li) {
      var rand = seededRand(li * 77 + 13);
      var titles = SAMPLE_TITLES[lib.slug] || ["示例文档"];
      var docs = [];
      var n = Math.min(titles.length * 2, 14);
      for (var i = 0; i < n; i++) {
        var title = titles[i % titles.length];
        var ext = SAMPLE_EXTS[Math.floor(rand() * SAMPLE_EXTS.length)];
        var st = DOC_STATUSES[rand() < 0.82 ? 0 : Math.floor(rand() * 4)];
        var d = new Date(Date.now() - rand() * 12 * 864e5);
        docs.push({
          id: lib.slug + "-" + (1000 + i),
          title: title + ext,
          status: st,
          chunks: st === "done" ? 8 + Math.floor(rand() * 220) : 0,
          size: 40 * 1024 + Math.floor(rand() * 8 * 1024 * 1024),
          revision: 1 + Math.floor(rand() * 4),
          updated_at: d.toISOString(),
          external_id: "EXT-" + (10000 + li * 100 + i)
        });
      }
      mock.docsByLib[lib.slug] = docs;
    });
  }

  function buildMockJobs() {
    var rows = [];
    var now = Date.now();
    for (var i = 0; i < 9; i++) {
      var lib = mock.libraries[i % mock.libraries.length];
      var st = i === 0 ? "processing" : i === 1 ? "pending" : i === 4 ? "failed" : "done";
      rows.push({
        id: "job-" + (9000 + i),
        library: lib.slug,
        libraryName: lib.name,
        document: (SAMPLE_TITLES[lib.slug] || ["文档"])[i % 3] + SAMPLE_EXTS[i % SAMPLE_EXTS.length],
        status: st,
        progress: st === "done" ? 100 : st === "processing" ? 42 + i : st === "failed" ? 67 : 0,
        created_at: new Date(now - i * 27 * 60e3).toISOString(),
        error: st === "failed" ? "Embedding 服务连接超时（ReadTimeout），已自动重试 2 次" : null
      });
    }
    mock.jobs = rows;
  }
  buildMockDocs();
  buildMockJobs();

  /* ---------------- demo latency ---------------- */
  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* ---------------- HTTP core ---------------- */
  async function request(path, opts) {
    opts = opts || {};
    var url = config.baseUrl.replace(/\/$/, "") + path;
    var headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    if (config.apiKey) headers["Authorization"] = "Bearer " + config.apiKey;
    if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";

    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, opts.timeout || 8000);
    try {
      var res = await fetch(url, {
        method: opts.method || "GET",
        headers: headers,
        body: opts.body instanceof FormData ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined),
        signal: controller.signal
      });
      clearTimeout(timer);
      if (!res.ok) {
        var text = "";
        try { text = await res.text(); } catch (e) {}
        var err = new Error("HTTP " + res.status + (text ? " · " + text.slice(0, 160) : ""));
        err.status = res.status;
        throw err;
      }
      var ct = res.headers.get("content-type") || "";
      return ct.indexOf("json") >= 0 ? res.json() : res.text();
    } catch (e) {
      clearTimeout(timer);
      throw e;
    }
  }

  /* 真实后端是否可用（health 探测并缓存 15s） */
  var backendState = { checked: 0, ok: false };
  async function probeBackend(force) {
    if (forceDemo) { backendState = { checked: Date.now(), ok: false }; return false; }
    if (!force && Date.now() - backendState.checked < 15000) return backendState.ok;
    try {
      await request("/health", { timeout: 3500 });
      backendState = { checked: Date.now(), ok: true };
    } catch (e) {
      backendState = { checked: Date.now(), ok: false };
    }
    store.emit("connection", backendState.ok);
    return backendState.ok;
  }

  /* ---------------- Public API ---------------- */
  var api = {
    store: store,
    getConfig: function () { return Object.assign({}, config); },
    isDemoForced: function () { return forceDemo; },
    setDemoForced: function (v) { forceDemo = !!v; probeBackend(true); },

    saveConfig: async function (cfg) {
      if (!cfg.baseUrl || !/^https?:\/\//.test(cfg.baseUrl)) {
        throw new Error("Base URL 必须以 http:// 或 https:// 开头");
      }
      config = { baseUrl: cfg.baseUrl.replace(/\/$/, ""), apiKey: cfg.apiKey || "" };
      saveConfig(config);
      return probeBackend(true);
    },

    probe: probeBackend,
    backendOk: function () { return backendState.ok && !forceDemo; },

    health: async function () {
      if (await probeBackend()) return request("/health");
      await delay(350);
      return {
        status: "ok",
        mode: "demo",
        components: [
          { name: "PostgreSQL", detail: "队列表 FOR UPDATE SKIP LOCKED", status: "ok", latency: 4 },
          { name: "Qdrant", detail: "6 个 collection · 101k 向量", status: "ok", latency: 11 },
          { name: "Embedding (bge-m3)", detail: "本地推理 · 1024 维", status: "ok", latency: 38 },
          { name: "Rerank", detail: "未启用（RERANK_PROVIDER=off）", status: "warn", latency: 0 },
          { name: "OCR", detail: "库级开关 · 2 个库已启用", status: "ok", latency: 0 }
        ]
      };
    },

    overview: async function () {
      if (await probeBackend()) {
        try {
          var libs = await request("/admin/libraries");
          return { libraries: libs, mode: "live" };
        } catch (e) { /* fallthrough to demo */ }
      }
      await delay(500);
      var week = [];
      var dayNames = ["一", "二", "三", "四", "五", "六", "日"];
      for (var i = 6; i >= 0; i--) {
        var d = new Date(Date.now() - i * 864e5);
        week.push({
          label: "周" + dayNames[(d.getDay() + 6) % 7],
          queries: 180 + Math.round(120 * Math.sin(i * 1.3) + Math.random() * 60),
          ingested: 12 + Math.round(Math.random() * 40)
        });
      }
      return {
        mode: "demo",
        libraries: mock.libraries,
        totals: {
          libraries: mock.libraries.length,
          documents: mock.libraries.reduce(function (s, l) { return s + l.docs; }, 0),
          chunks: mock.libraries.reduce(function (s, l) { return s + l.chunks; }, 0),
          queries24h: 1247
        },
        week: week,
        activity: [
          { icon: "upload", text: "tech-docs 摄入「API 参考手册（Dify 兼容）.pdf」", time: "8 分钟前" },
          { icon: "search", text: "product-faq 完成 42 次检索调用（Dify）", time: "26 分钟前" },
          { icon: "refresh", text: "hr-policy「员工手册（2026 修订版）」reingest 完成，revision 4", time: "1 小时前" },
          { icon: "alert", text: "job-9004 嵌入失败：Embedding 服务连接超时", time: "2 小时前" },
          { icon: "trash", text: "finance-rules 删除 3 篇过期制度，Cleanup 已完成物理清理", time: "昨天 18:40" },
          { icon: "key", text: "签发了新 API Key「dify-prod-07」（仅显示一次）", time: "昨天 15:12" }
        ]
      };
    },

    listLibraries: async function () {
      if (await probeBackend()) {
        try { return await request("/admin/libraries"); } catch (e) {}
      }
      await delay(420);
      return mock.libraries;
    },

    libraryStats: async function (slug) {
      if (await probeBackend()) {
        try { return await request("/libraries/" + encodeURIComponent(slug) + "/stats"); } catch (e) {}
      }
      await delay(250);
      var lib = mock.libraries.find(function (l) { return l.slug === slug; });
      return lib ? { documents: lib.docs, chunks: lib.chunks, pending_jobs: 2 } : null;
    },

    listDocuments: async function (slug, filters) {
      filters = filters || {};
      if (await probeBackend()) {
        try {
          var q = [];
          if (filters.status) q.push("status=" + encodeURIComponent(filters.status));
          return await request("/libraries/" + encodeURIComponent(slug) + "/documents" + (q.length ? "?" + q.join("&") : ""));
        } catch (e) {}
      }
      await delay(380);
      var docs = (mock.docsByLib[slug] || []).slice();
      if (filters.status) docs = docs.filter(function (d) { return d.status === filters.status; });
      if (filters.q) {
        var kw = filters.q.toLowerCase();
        docs = docs.filter(function (d) { return d.title.toLowerCase().indexOf(kw) >= 0 || d.external_id.toLowerCase().indexOf(kw) >= 0; });
      }
      return docs;
    },

    deleteDocument: async function (slug, docId) {
      if (await probeBackend()) {
        try { return await request("/libraries/" + encodeURIComponent(slug) + "/documents/" + encodeURIComponent(docId), { method: "DELETE" }); } catch (e) { throw e; }
      }
      await delay(450);
      var list = mock.docsByLib[slug] || [];
      var idx = list.findIndex(function (d) { return d.id === docId; });
      if (idx >= 0) list.splice(idx, 1);
      return { ok: true, mode: "demo", note: "tombstone + outbox 已入队（模拟）" };
    },

    query: async function (slug, text, topK) {
      if (!text || !text.trim()) throw new Error("请输入检索内容");
      if (await probeBackend()) {
        try {
          return await request("/libraries/" + encodeURIComponent(slug) + "/query", {
            method: "POST",
            body: { query: text, retrieval_setting: { top_k: topK || 5 } }
          });
        } catch (e) { throw e; }
      }
      await delay(650);
      var lib = mock.libraries.find(function (l) { return l.slug === slug; });
      var titles = SAMPLE_TITLES[slug] || ["示例文档"];
      var words = text.trim().split(/\s+/).filter(Boolean);
      var results = [];
      var n = topK || 5;
      for (var i = 0; i < n; i++) {
        var base = 0.92 - i * (0.05 + Math.random() * 0.04);
        var t = titles[i % titles.length];
        results.push({
          document_id: slug + "-" + (1000 + i),
          title: t + SAMPLE_EXTS[i % SAMPLE_EXTS.length],
          score: Math.max(0.2, base),
          vector_score: Math.max(0.2, base - 0.02),
          rerank_score: null,
          chunk_index: 3 + i * 2,
          content: "……与「" + (words[0] || text) + "」相关的段落：本制度适用于全体正式员工与实习生。具体执行细则以最新修订版本为准，如有冲突以人力资源部解释为准。原文件第 " + (3 + i) + " 节明确约定了申请流程、审批层级与时限要求……"
        });
      }
      return { mode: "demo", library: lib ? lib.name : slug, results: results, elapsed_ms: 240 + Math.round(Math.random() * 120) };
    },

    uploadFiles: async function (slug, files, onProgress) {
      if (await probeBackend()) {
        var out = [];
        for (var i = 0; i < files.length; i++) {
          var fd = new FormData();
          fd.append("file", files[i]);
          try {
            out.push(await request("/libraries/" + encodeURIComponent(slug) + "/import-file", { method: "POST", body: fd, timeout: 60000 }));
          } catch (e) { out.push({ error: e.message, file: files[i].name }); }
          if (onProgress) onProgress(i + 1, files.length);
        }
        return out;
      }
      // demo：模拟逐个上传
      var results = [];
      for (var j = 0; j < files.length; j++) {
        var steps = 8;
        for (var s = 1; s <= steps; s++) {
          await delay(90 + Math.random() * 90);
          if (onProgress) onProgress(j, files.length, s / steps);
        }
        results.push({ ok: true, file: files[j].name, job_id: "job-demo-" + Date.now() + j });
      }
      return results;
    },

    ingestText: async function (slug, payload) {
      if (await probeBackend()) {
        return request("/libraries/" + encodeURIComponent(slug) + "/documents", { method: "POST", body: payload });
      }
      await delay(700);
      return { mode: "demo", document_id: slug + "-demo-" + Date.now(), status: "pending", message: "已创建摄入任务（演示）" };
    },

    listJobs: async function (filters) {
      filters = filters || {};
      if (await probeBackend()) {
        try {
          var q = [];
          if (filters.status) q.push("status=" + encodeURIComponent(filters.status));
          return await request("/admin/jobs" + (q.length ? "?" + q.join("&") : ""));
        } catch (e) {}
      }
      await delay(360);
      var rows = mock.jobs.slice();
      if (filters.status) rows = rows.filter(function (j) { return j.status === filters.status; });
      return rows;
    },

    retryJob: async function (jobId) {
      if (await probeBackend()) {
        return request("/admin/jobs/" + encodeURIComponent(jobId) + "/retry", { method: "POST" });
      }
      await delay(400);
      var job = mock.jobs.find(function (j) { return j.id === jobId; });
      if (job) { job.status = "pending"; job.progress = 0; job.error = null; }
      return { ok: true, mode: "demo" };
    },

    createLibrary: async function (payload) {
      if (await probeBackend()) {
        return request("/admin/libraries", { method: "POST", body: payload });
      }
      await delay(600);
      var lib = {
        slug: payload.slug, name: payload.name,
        description: payload.description || "",
        docs: 0, chunks: 0, model: "bge-m3",
        updated: new Date().toISOString(), status: "active"
      };
      mock.libraries.unshift(lib);
      mock.docsByLib[lib.slug] = [];
      return { mode: "demo", library: lib };
    }
  };

  window.VKApi = api;
})();
