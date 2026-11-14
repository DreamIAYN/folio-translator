/* Folio 译坊 — application shell.
   Vanilla JS, no build step. All book text is escaped before rendering. */

(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const LANG = {
    auto: "自动识别", "zh-CN": "简体中文", "zh-TW": "繁體中文", en: "English",
    ja: "日本語", ko: "한국어", fr: "Français", de: "Deutsch", es: "Español",
    ru: "Русский", ar: "العربية", pt: "Português", it: "Italiano", vi: "Tiếng Việt",
    th: "ไทย", id: "Bahasa Indonesia", tr: "Türkçe", nl: "Nederlands",
    pl: "Polski", fa: "فارسی", he: "עברית", hi: "हिन्दी",
  };
  const DOMAIN = {
    auto: "自动判断", math: "数学 / 统计", science: "自然科学", cs: "计算机 / 技术",
    medicine: "医学 / 生命科学", law: "法律 / 政策", history: "历史 / 人文",
    philosophy: "哲学 / 思想", literature: "文学 / 小说", business: "商业 / 经济",
    social: "社会科学", general: "通用非虚构",
  };
  const STYLE = {
    publish: "出版级（推荐）", academic: "学术严谨", literary: "文学风格",
    faithful: "贴近原文", concise: "精简表达",
  };
  const STATUS = {
    queued: "排队中", running: "翻译中", completed: "已完成", failed: "失败",
    paused: "已暂停", cancelled: "已取消", ready: "待开始", processing: "翻译中",
  };
  const STAGE = {
    queued: "等待调度", extract: "解析文档", analysis: "分析书稿", glossary: "提取术语",
    ocr: "AI 视觉识别", translation: "分章翻译", review: "AI 审校", export: "生成导出", completed: "完成",
    failed: "失败", cancelled: "已取消", interrupted: "已中断",
  };
  const EXPORTS = [
    ["docx", "Word 译本", "DOCX"], ["bilingual_docx", "Word 双语对照", "DOCX"],
    ["pdf", "PDF 译本", "PDF"],
    ["md", "Markdown", "MD"], ["bilingual_md", "Markdown 双语", "MD"],
    ["html", "网页版（双语）", "HTML"], ["epub", "EPUB 电子书", "EPUB"],
    ["txt", "纯文本", "TXT"],
  ];
  const COVERS = [
    "linear-gradient(145deg,#8f4a35,#b56447)", "linear-gradient(145deg,#42584a,#647a66)",
    "linear-gradient(145deg,#6d5a3a,#9b8151)", "linear-gradient(145deg,#44505f,#68778a)",
    "linear-gradient(145deg,#6e4356,#946075)", "linear-gradient(145deg,#4b4a3f,#77745f)",
  ];

  const state = {
    user: null, route: "", models: [], defaultModel: "",
    doc: null, docTab: "reader", jobTimer: null, libraryTimer: null,
    reader: { offset: 0, limit: 40, q: "", chapter: "", mode: "bilingual", font: "medium", loading: false, hasMore: false },
    health: null,
  };

  // ------------------------------------------------------------------ utils
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function attr(value) { return esc(value); }
  function fmtNumber(n) {
    n = Number(n || 0);
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + "万";
    return n.toLocaleString("zh-CN");
  }
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n > 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }
  function fmtDate(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  }
  function fmtTime(value) {
    if (!value) return "–";
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? "–" : d.toLocaleString("zh-CN", { hour12: false });
  }
  function cover(seed) {
    let h = 0;
    for (const ch of String(seed)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return COVERS[h % COVERS.length];
  }
  function initial(title) { return (String(title || "书").trim()[0] || "书").toUpperCase(); }
  function lang(code) { return LANG[code] || code || "—"; }
  function statusLabel(status) { return STATUS[status] || status || "—"; }
  function friendlyError(message) {
    if (/HTTP 402|provider returned HTTP 402|额度/i.test(message || "")) {
      return "模型额度不足（HTTP 402）。请切换到 deepseek-flash 后点「继续翻译」；已有 OCR / 译文会保留。";
    }
    return message || "";
  }
  function stageLabel(stage) { return STAGE[stage] || stage || ""; }
  function statusClass(status) {
    if (["processing", "running", "queued"].includes(status)) return "processing";
    if (["completed"].includes(status)) return "completed";
    if (["failed"].includes(status)) return "failed";
    if (["paused", "cancelled"].includes(status)) return "paused";
    return "";
  }
  function isActive(job) { return job && ["queued", "running"].includes(job.status); }
  function toast(message, type = "") {
    const root = $("#toast-root");
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateY(6px)"; }, 3400);
    setTimeout(() => el.remove(), 3800);
  }
  function setLoading(btn, loading, label = "处理中…") {
    if (!btn) return;
    if (loading) {
      btn.dataset.label = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<span class="spin"></span>${esc(label)}`;
    } else {
      btn.disabled = false;
      if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
    }
  }

  class ApiError extends Error {
    constructor(message, status, detail, hint) {
      super(message); this.status = status; this.detail = detail; this.hint = hint;
    }
  }
  async function api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      showAuth();
      throw new ApiError("登录已过期，请重新登录", 401);
    }
    if (!res.ok) {
      let message = "请求失败", hint = "";
      const detail = data && data.detail;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object") { message = detail.message || message; hint = detail.hint || ""; }
      else if (data && data.message) message = data.message;
      throw new ApiError(message, res.status, detail, hint);
    }
    return data;
  }

  // ------------------------------------------------------------------ modal
  function openModal(inner, { wide = false } = {}) {
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-backdrop"><div class="modal" style="${wide ? "width:min(640px,100%)" : ""}">${inner}</div></div>`;
    const backdrop = $(".modal-backdrop", root);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeModal(); });
    const cancel = $("[data-modal-cancel]", root);
    if (cancel) cancel.addEventListener("click", closeModal);
    return $(".modal", root);
  }
  function closeModal() { $("#modal-root").innerHTML = ""; }
  function confirmDialog({ title, message, confirm = "确认", danger = false }) {
    return new Promise((resolve) => {
      const modal = openModal(`
        <h3>${esc(title)}</h3>
        <p>${esc(message)}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" data-modal-cancel>取消</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" id="modal-confirm">${esc(confirm)}</button>
        </div>`);
      $("#modal-confirm", modal).addEventListener("click", () => { closeModal(); resolve(true); });
      modal.closest(".modal-backdrop").addEventListener("click", (e) => {
        if (e.target.classList.contains("modal-backdrop")) resolve(false);
      });
      $("[data-modal-cancel]", modal).addEventListener("click", () => resolve(false));
    });
  }

  // ------------------------------------------------------------------ auth
  function showAuth() {
    state.user = null;
    clearInterval(state.jobTimer); state.jobTimer = null;
    clearTimeout(state.libraryTimer); state.libraryTimer = null;
    $("#app-view").classList.add("hidden");
    $("#auth-view").classList.remove("hidden");
    switchAuthTab("login");
  }
  function switchAuthTab(tab) {
    $$(".auth-tab").forEach((b) => b.classList.toggle("active", b.dataset.authTab === tab));
    $("#login-form").classList.toggle("hidden", tab !== "login");
    $("#register-form").classList.toggle("hidden", tab !== "register");
  }
  async function enterApp() {
    $("#auth-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    $("#user-name").textContent = state.user.name || "读者";
    $("#user-email").textContent = state.user.email || "";
    $("#user-avatar").textContent = (state.user.name || "读").trim()[0] || "读";
    try {
      const data = await api("/api/models");
      state.models = data.items || [];
      state.defaultModel = data.default || (state.models[0] && state.models[0].id) || "";
    } catch (_) {
      state.models = [{ id: "deepseek-v4.1-flash", label: "deepseek-v4.1-flash" }];
      state.defaultModel = "deepseek-v4.1-flash";
    }
    loadHealth();
    if (!location.hash) history.replaceState(null, "", "#/library");
    await route();
  }
  async function loadHealth() {
    try {
      const data = await api("/api/health");
      state.health = data;
      const ok = data.harness && data.harness.ok;
      const dot = ok ? "ok" : "bad";
      const text = ok ? "mini-harness 在线" : "翻译引擎未连接";
      $("#harness-status").innerHTML = `<span class="status-dot ${dot}"></span><div><strong>翻译引擎</strong><span>${esc(text)}</span></div>`;
      $("#engine-pill").innerHTML = `<span class="status-dot ${dot}"></span><span>${ok ? "AI 引擎在线" : "引擎离线"}</span>`;
    } catch (_) { /* keep subdued */ }
  }
  async function doAuth(form, endpoint) {
    const btn = $("button[type=submit]", form);
    const body = Object.fromEntries(new FormData(form).entries());
    setLoading(btn, true, "登录中…");
    try {
      const data = await api(endpoint, { method: "POST", body });
      state.user = data.user;
      await enterApp();
    } catch (err) {
      toast(err.message || "操作失败", "error");
    } finally {
      setLoading(btn, false);
    }
  }

  // ------------------------------------------------------------------ routing
  async function route() {
    if (!state.user) return;
    clearInterval(state.jobTimer); state.jobTimer = null;
    clearTimeout(state.libraryTimer); state.libraryTimer = null;
    closeModal();
    const hash = location.hash || "#/library";
    const [base, param] = hash.replace(/^#\/?/, "").split("/");
    state.route = base || "library";
    $$("#main-nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === state.route));
    const page = $("#page");
    page.innerHTML = `<div class="spinner-block"><span class="spin"></span>正在打开…</div>`;
    try {
      if (state.route === "library") { await renderLibrary(); }
      else if (state.route === "new") { renderUpload(); }
      else if (state.route === "reference") { await renderReference(); }
      else if (state.route === "doc" && param) { await renderDoc(param); }
      else { location.hash = "#/library"; }
    } catch (err) {
      if (err.status === 401) return;
      page.innerHTML = `<div class="empty"><div class="glyph">!</div><h3>页面加载失败</h3><p>${esc(err.message || "请稍后重试")}</p><button class="btn btn-ghost" onclick="location.reload()">刷新页面</button></div>`;
    }
  }

  function crumb(title, parent) {
    const el = $("#crumbs");
    if (!parent) el.innerHTML = `<span>${esc(title)}</span>`;
    else el.innerHTML = `<a href="${attr(parent.href)}" style="color:var(--muted)">${esc(parent.label)}</a><span class="sep">/</span><strong>${esc(title)}</strong>`;
  }

  // ------------------------------------------------------------------ library
  function docRow(doc) {
    const st = doc.job_status || doc.status;
    const active = ["queued", "running", "processing"].includes(st);
    const progress = Math.max(0, Math.min(100, Number(doc.progress || 0)));
    return `<div class="doc-row" data-doc="${attr(doc.id)}" role="link" tabindex="0">
      <div class="doc-cover" style="background:${cover(doc.id)}">${esc(initial(doc.translated_title || doc.title))}</div>
      <div class="doc-main">
        <div class="doc-title">${esc(doc.title)}</div>
        <div class="doc-sub">${doc.translated_title ? `<span>${esc(doc.translated_title)}</span><span class="dot">·</span>` : ""}<span>${esc(fmtNumber(doc.char_count))} 字符</span><span class="dot">·</span><span>${esc(fmtDate(doc.created_at))}</span></div>
      </div>
      <div class="doc-lang">${esc(lang(doc.source_lang))} → ${esc(lang(doc.target_lang))}<span>${esc(doc.ext.replace(".", "").toUpperCase())} · ${esc(fmtNumber(doc.page_count || doc.segment_count))} ${doc.page_count ? (doc.ext === ".epub" ? "节" : "页") : "段"}</span></div>
      <div>
        <span class="status-pill ${statusClass(st)}">${esc(statusLabel(st))}</span>
        ${active ? `<div class="mini-track"><div class="mini-fill" style="width:${progress}%"></div></div><div class="doc-progress">${Math.round(progress)}% · ${esc(stageLabel(doc.job_stage))}</div>` : `<div class="doc-progress">${esc(stageLabel(doc.job_stage))}</div>`}
      </div>
      <div class="doc-status"><span class="badge">${esc(fmtNumber(doc.tokens_total || 0))} tok</span></div>
      <div class="chev">›</div>
    </div>`;
  }
  function bindDocRows(root) {
    $$("[data-doc]", root).forEach((row) => {
      const go = () => { location.hash = `#/doc/${row.dataset.doc}`; };
      row.addEventListener("click", go);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }
  async function refreshLibraryDocs() {
    if (state.route !== "library") return;
    try {
      const [docsData, statsData] = await Promise.all([api("/api/documents"), api("/api/stats")]);
      const list = $("#doc-list");
      if (!list) return;
      list.innerHTML = docsData.items.map(docRow).join("");
      bindDocRows(list);
      const active = docsData.items.some((d) => ["queued", "running", "processing"].includes(d.job_status || d.status));
      if (active) scheduleLibraryRefresh();
      const s = statsData.stats;
      if ($("#stat-docs")) $("#stat-docs").textContent = fmtNumber(s.documents);
      if ($("#stat-queue") && statsData.queue) $("#stat-queue").innerHTML = `${statsData.queue.running}<small>/ ${statsData.queue.queued} 排队</small>`;
      if ($("#stat-chars")) $("#stat-chars").textContent = fmtNumber(s.translated_chars);
      if ($("#stat-tokens")) $("#stat-tokens").textContent = fmtNumber(s.tokens);
    } catch (_) { /* silent */ }
  }
  function scheduleLibraryRefresh() {
    clearTimeout(state.libraryTimer);
    state.libraryTimer = setTimeout(refreshLibraryDocs, 5000);
  }
  async function renderLibrary() {
    crumb("我的书架");
    const [docsData, statsData] = await Promise.all([api("/api/documents"), api("/api/stats")]);
    const docs = docsData.items || [];
    const stats = statsData.stats || {};
    const active = docs.some((d) => ["queued", "running", "processing"].includes(d.job_status || d.status));
    $("#library-badge").textContent = statsData.library_count || "";
    $("#page").innerHTML = `
      <div class="page-head">
        <div>
          <div class="eyebrow">Your translation shelf</div>
          <h1 class="page-title">我的书架</h1>
          <p class="page-sub">上传一本原版书，选择语言与风格。AI 会先读懂全书，再逐段翻译、统一术语并生成可导出的版本。</p>
        </div>
        <a class="btn btn-primary" href="#/new">＋ 导入新书</a>
      </div>
      <div class="stat-grid">
        <div class="stat-card accent"><div class="label"><span>书架文档</span><span>DOCS</span></div><div class="value" id="stat-docs">${fmtNumber(stats.documents)}</div><div class="foot">${stats.completed || 0} 本已完成翻译</div></div>
        <div class="stat-card"><div class="label"><span>译文规模</span><span>CHARS</span></div><div class="value" id="stat-chars">${fmtNumber(stats.translated_chars)}</div><div class="foot">原文共 ${fmtNumber(stats.source_chars)} 字符</div></div>
        <div class="stat-card"><div class="label"><span>AI 消耗</span><span>TOKENS</span></div><div class="value" id="stat-tokens">${fmtNumber(stats.tokens)}</div><div class="foot">由 mini-harness 计量</div></div>
        <div class="stat-card"><div class="label"><span>翻译队列</span><span>RUNNING</span></div><div class="value" id="stat-queue">${statsData.queue ? statsData.queue.running : 0}<small>/ ${statsData.queue ? statsData.queue.queued : 0} 排队</small></div><div class="foot">已完成 ${stats.completed || 0} 本</div></div>
      </div>
      <div class="section-bar">
        <h2 class="section-title">全部文档 <span class="count">${docs.length}</span></h2>
        <span class="tiny muted">${active ? "有任务进行中，列表会自动刷新" : "按最近导入排序"}</span>
      </div>
      <div class="doc-list" id="doc-list">${docs.length ? docs.map(docRow).join("") : `
        <div class="empty" style="border:0;background:transparent">
          <div class="glyph">⌘</div>
          <h3>书架还空着</h3>
          <p>上传 PDF、DOCX、EPUB、Markdown 或 TXT。数学书里的公式、定理编号和术语都会被保留。</p>
          <div class="empty-actions"><a class="btn btn-primary" href="#/new">导入第一本书</a><a class="btn btn-ghost" href="#/reference">看看参考书目</a></div>
        </div>`}
      </div>`;
    bindDocRows($("#doc-list"));
    if (active) scheduleLibraryRefresh();
  }

  // ------------------------------------------------------------------ upload
  let uploadFile = null;
  function renderUpload() {
    crumb("导入新书");
    uploadFile = null;
    const langOptions = Object.entries(LANG).map(([code, label]) => `<option value="${attr(code)}">${esc(label)}</option>`).join("");
    const targetOptions = Object.entries(LANG).filter(([code]) => !["auto"].includes(code)).map(([code, label]) => `<option value="${attr(code)}" ${code === "zh-CN" ? "selected" : ""}>${esc(label)}</option>`).join("");
    const modelOptions = state.models.map((m) => `<option value="${attr(m.id)}" ${m.id === state.defaultModel ? "selected" : ""}>${esc(m.label || m.id)}</option>`).join("");
    $("#page").innerHTML = `
      <div class="page-head">
        <div><div class="eyebrow">New translation</div><h1 class="page-title">导入一本新书</h1>
        <p class="page-sub">先选择文档和语言。解析完成后任务自动开始，AI 会分析整本书、生成术语表并逐段翻译。</p></div>
      </div>
      <div class="upload-grid">
        <div class="card card-pad">
          <form id="upload-form">
            <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="选择文件">
              <div class="dz-icon">↥</div>
              <h3>把书稿拖到这里</h3>
              <p>或点击选择文件 · 最大 ${window.__MAX_MB__ || 100} MB</p>
              <div class="formats"><span class="fmt-chip">PDF</span><span class="fmt-chip">DOCX</span><span class="fmt-chip">EPUB</span><span class="fmt-chip">TXT</span><span class="fmt-chip">MD</span><span class="fmt-chip">HTML</span><span class="fmt-chip">DJVU</span></div>
              <input type="file" id="file-input" hidden accept=".pdf,.docx,.epub,.txt,.md,.markdown,.html,.htm,.djvu">
            </div>
            <div class="file-picked" id="file-picked">
              <div class="file-icon" id="file-ext">PDF</div>
              <div class="file-meta"><strong id="file-name"></strong><span id="file-size"></span></div>
              <button class="icon-btn" type="button" id="clear-file" title="移除">×</button>
            </div>
            <div class="form-section-title">书籍信息</div>
            <div class="form-stack">
              <label>书名（可选，留空自动识别）<input name="title" type="text" placeholder="例如：Graph Theory" maxlength="300"></label>
              <div class="field-row">
                <label>原文语言<select name="source_lang">${langOptions}</select></label>
                <label>译为<select name="target_lang">${targetOptions}</select></label>
              </div>
              <div class="field-row">
                <label>内容领域<select name="domain">${Object.entries(DOMAIN).map(([c, l]) => `<option value="${attr(c)}">${esc(l)}</option>`).join("")}</select></label>
                <label>翻译风格<select name="style">${Object.entries(STYLE).map(([c, l]) => `<option value="${attr(c)}">${esc(l)}</option>`).join("")}</select></label>
              </div>
              <label>翻译模型<select name="model">${modelOptions}</select></label>
              <label>PDF 文字识别<select name="ocr_mode">
                <option value="auto">自动（有文字层用文本，扫描版转 AI 识别）</option>
                <option value="vision">AI 视觉识别（公式/扫描书推荐）</option>
                <option value="text">仅文本层（最快，公式可能乱码）</option>
              </select></label>
              <p class="hint" style="margin-top:-8px">数学书、公式较多的 PDF 建议选「AI 视觉识别」：模型会逐页读取图片，把公式写成 LaTeX。会增加 OCR token 与耗时。</p>
              <label>给 AI 的额外提示（可选）<textarea name="custom_prompt" rows="3" maxlength="4000" placeholder="例如：保留英文术语对照；公式一律用 LaTeX；图注不要意译；人名首次出现时附原名。"></textarea></label>
              <label class="check"><input type="checkbox" name="glossary" checked><span><strong>自动生成术语表</strong><span>统一人名、地名、学科术语与数学概念</span></span></label>
              <label class="check"><input type="checkbox" name="thinking"><span><strong>启用模型思考（更慢，质量更高）</strong><span>默认关闭思考以加快翻译；复杂数学书可开启</span></span></label>
              <label class="check"><input type="checkbox" name="review"><span><strong>启用 AI 二审（更慢、更贵）</strong><span>逐段比对原文，修正漏译与术语不一致</span></span></label>
              <div class="notice"><strong>长文档提示：</strong>每段译文都会即时写入本地数据库。关闭页面不会中断任务；任务失败也可从已翻译位置继续。</div>
              <button class="btn btn-primary btn-wide" type="submit" id="upload-submit">开始解析并翻译</button>
            </div>
          </form>
        </div>
        <div class="upload-aside">
          <div class="card aside-card">
            <h3>✦ AI 翻译流水线</h3>
            <div class="pipeline">
              <div class="pipe-step"><span class="pipe-num">1</span><div><strong>解析与分段</strong><span>保留章节、列表、公式与段落结构</span></div></div>
              <div class="pipe-step"><span class="pipe-num">2</span><div><strong>读懂全书</strong><span>识别文类、语气、读者与翻译策略</span></div></div>
              <div class="pipe-step"><span class="pipe-num">3</span><div><strong>生成术语表</strong><span>统一专名、学科词汇和定理名称</span></div></div>
              <div class="pipe-step"><span class="pipe-num">4</span><div><strong>逐段翻译</strong><span>并行调用 mini-harness，逐段落库</span></div></div>
              <div class="pipe-step"><span class="pipe-num">5</span><div><strong>审校与导出</strong><span>可选二审，生成 Word / EPUB / 网页</span></div></div>
            </div>
          </div>
          <div class="card aside-card">
            <h3>◈ 适合哪些书？</h3>
            <p class="hint">学术专著、小说、传记、法律与医学文档、技术手册都可以。数学与理工书会重点保留 LaTeX、变量名、编号和公式符号。</p>
            <div class="divider"></div>
            <p class="hint"><strong>扫描版 PDF / 无字层 DJVU</strong> 需要先 OCR。平台会明确提示，不会假装翻译。</p>
          </div>
        </div>
      </div>`;
    const dropzone = $("#dropzone");
    const input = $("#file-input");
    const pick = (file) => {
      if (!file) return;
      uploadFile = file;
      $("#file-ext").textContent = (file.name.split(".").pop() || "FILE").toUpperCase().slice(0, 5);
      $("#file-name").textContent = file.name;
      $("#file-size").textContent = fmtBytes(file.size);
      $("#file-picked").classList.add("show");
    };
    dropzone.addEventListener("click", () => input.click());
    dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") input.click(); });
    input.addEventListener("change", () => pick(input.files[0]));
    ["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
    dropzone.addEventListener("drop", (e) => pick(e.dataTransfer.files[0]));
    $("#clear-file").addEventListener("click", () => {
      uploadFile = null; input.value = ""; $("#file-picked").classList.remove("show");
    });
    $("#upload-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!uploadFile) { toast("请先选择要翻译的文件", "error"); return; }
      const form = e.currentTarget;
      const btn = $("#upload-submit");
      const fd = new FormData(form);
      fd.set("file", uploadFile);
      if (!fd.get("glossary")) fd.set("glossary", "false");
      fd.set("review", form.review.checked ? "true" : "false");
      fd.set("thinking", form.thinking.checked ? "true" : "false");
      setLoading(btn, true, "上传并解析中…");
      try {
        const data = await xhrUpload("/api/documents", fd, (pct) => {
          if (pct < 100) btn.innerHTML = `<span class="spin"></span>上传中 ${pct}%`;
          else btn.innerHTML = `<span class="spin"></span>解析书稿中…`;
        });
        toast("书稿已导入，AI 已开始工作", "success");
        location.hash = `#/doc/${data.id}`;
      } catch (err) {
        toast((err.message || "上传失败") + (err.hint ? `：${err.hint}` : ""), "error");
        setLoading(btn, false);
      }
    });
  }
  function xhrUpload(path, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100)); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else {
          const d = data && data.detail;
          const msg = typeof d === "string" ? d : (d && d.message) || (data && data.message) || "上传失败";
          reject(new ApiError(msg, xhr.status, d, d && d.hint));
        }
      };
      xhr.onerror = () => reject(new ApiError("网络连接失败", 0));
      xhr.send(fd);
    });
  }

  // ------------------------------------------------------------------ reference
  async function renderReference() {
    crumb("参考书目");
    const data = await api("/api/library");
    const entries = data.items || [];
    $("#library-badge").textContent = entries.length || "";
    $("#page").innerHTML = `
      <div class="page-head">
        <div><div class="eyebrow">Local reference shelf</div><h1 class="page-title">参考书目</h1>
        <p class="page-sub">来自本地书架 <span class="mono">${esc(data.directory_label || "ocr1")}</span> 的 ${entries.length} 本原版书。可直接导入翻译，无需重新上传。</p></div>
        <button class="btn btn-ghost" id="refresh-ref">↻ 刷新目录</button>
      </div>
      ${data.directory_exists ? "" : `<div class="notice" style="margin-bottom:18px">参考书目目录当前不可访问。请检查 <span class="mono">REFERENCE_BOOKS_DIR</span> 配置。</div>`}
      ${entries.length ? `<div class="ref-grid">${entries.map((e) => `
        <article class="ref-card">
          <div class="ref-top">
            <div class="ref-spine" style="background:${cover(e.name)}">${esc(e.format.toUpperCase())}</div>
            <div class="ref-title">${esc(e.title)}</div>
          </div>
          <div class="ref-tags">${(e.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}<span class="tag">${esc(e.size_mb)} MB</span>${e.supported ? "" : `<span class="tag warn">需转换</span>`}</div>
          ${e.note ? `<div class="ref-note">${esc(e.note)}</div>` : ""}
          <div class="ref-foot"><span>${esc(e.format.toUpperCase())} · 本地文件</span>
            <button class="btn btn-small ${e.supported ? "btn-ghost" : ""}" data-import="${attr(e.name)}" ${e.supported ? "" : "disabled"}>${e.supported ? "导入翻译" : "不可直接导入"}</button>
          </div>
        </article>`).join("")}</div>` : `<div class="empty"><div class="glyph">✦</div><h3>目录里还没有书</h3><p>把 PDF、DOCX 或 EPUB 放进参考书目目录，刷新后即可一键导入。</p></div>`}`;
    $("#refresh-ref").addEventListener("click", () => route());
    $$("[data-import]").forEach((btn) => btn.addEventListener("click", () => openImportDialog(btn.dataset.import)));
  }
  function openImportDialog(name) {
    const targetOptions = Object.entries(LANG).filter(([c]) => c !== "auto").map(([c, l]) => `<option value="${attr(c)}" ${c === "zh-CN" ? "selected" : ""}>${esc(l)}</option>`).join("");
    const modal = openModal(`
      <h3>导入参考书</h3>
      <p class="mono" style="font-size:11px;word-break:break-all;margin-bottom:16px">${esc(name)}</p>
      <form id="import-form" class="form-stack">
        <div class="field-row">
          <label>原文语言<select name="source_lang"><option value="auto">自动识别</option>${Object.entries(LANG).filter(([c]) => c !== "auto").map(([c, l]) => `<option value="${attr(c)}">${esc(l)}</option>`).join("")}</select></label>
          <label>译为<select name="target_lang">${targetOptions}</select></label>
        </div>
        <div class="field-row">
          <label>内容领域<select name="domain">${Object.entries(DOMAIN).map(([c, l]) => `<option value="${attr(c)}">${esc(l)}</option>`).join("")}</select></label>
          <label>翻译风格<select name="style">${Object.entries(STYLE).map(([c, l]) => `<option value="${attr(c)}">${esc(l)}</option>`).join("")}</select></label>
        </div>
        <label>PDF 文字识别<select name="ocr_mode">
          <option value="auto">自动</option>
          <option value="vision">AI 视觉识别（公式/扫描书）</option>
          <option value="text">仅文本层</option>
        </select></label>
        <label>给 AI 的额外提示（可选）<textarea name="custom_prompt" rows="3" maxlength="4000" placeholder="例如：保留英文术语；公式用 LaTeX；图注保持简洁。"></textarea></label>
        <label class="check"><input type="checkbox" name="thinking"><span><strong>启用模型思考</strong><span>默认关闭以加快速度</span></span></label>
        <label class="check"><input type="checkbox" name="review"><span><strong>启用 AI 二审</strong><span>更精确，但耗时与 token 消耗增加</span></span></label>
        <div class="modal-actions">
          <button type="button" class="btn btn-ghost" data-modal-cancel>取消</button>
          <button type="submit" class="btn btn-primary">导入并开始翻译</button>
        </div>
      </form>`, { wide: true });
    $("#import-form", modal).addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const btn = $("button[type=submit]", form);
      setLoading(btn, true, "正在导入…");
      try {
        const data = await api("/api/library/import", {
          method: "POST",
          body: {
            name,
            source_lang: form.source_lang.value,
            target_lang: form.target_lang.value,
            domain: form.domain.value,
            style: form.style.value,
            model: state.defaultModel,
            review: form.review.checked,
            glossary: true,
            thinking: form.thinking.checked,
            custom_prompt: form.custom_prompt.value,
            ocr_mode: form.ocr_mode.value,
          },
        });
        closeModal();
        toast("已从参考书目导入", "success");
        location.hash = `#/doc/${data.id}`;
      } catch (err) {
        toast((err.message || "导入失败") + (err.hint ? `：${err.hint}` : ""), "error");
        setLoading(btn, false);
      }
    });
  }

  // ------------------------------------------------------------------ document
  function jobBanner(job) {
    if (!job) return "";
    const active = isActive(job);
    const cls = job.status === "failed" ? "failed" : job.status === "completed" ? "done" : "";
    const pct = Math.max(0, Math.min(100, Number(job.progress || 0)));
    const done = Number(job.translated_segments || 0);
    const total = Number(job.total_segments || 0);
    return `<div class="job-banner ${cls}" id="job-banner">
      <div class="job-line">
        <span class="status-dot ${active ? "pending" : job.status === "completed" ? "ok" : job.status === "failed" ? "bad" : ""}"></span>
        <strong>${esc(statusLabel(job.status))}</strong>
        <span class="job-stage">${esc(stageLabel(job.stage))}</span>
        <span class="job-pct">${Math.round(pct)}%</span>
      </div>
      <div class="track"><div class="track-fill ${job.status === "completed" ? "done" : ""}" style="width:${pct}%"></div></div>
      <div class="job-meta">
        <span>段落 ${done}${total ? ` / ${total}` : ""}</span>
        ${job.reviewed_segments ? `<span>审校 ${job.reviewed_segments}</span>` : ""}
        <span>tokens ${fmtNumber(job.tokens_total)}</span>
        <span>${esc(job.model || "")}</span>
        <span>${job.thinking_enabled ? "思考开启" : "快速模式"}</span>
        ${job.started_at ? `<span>开始 ${esc(fmtTime(job.started_at))}</span>` : ""}
        ${active ? `<span>可离开页面，任务在后台继续</span>` : ""}
      </div>
      ${job.error ? `<div class="job-err">${esc(friendlyError(job.error))}</div>` : ""}
    </div>`;
  }

  function docActions(doc) {
    const job = doc.job || {};
    const active = isActive(job);
    const completed = doc.status === "completed" || job.status === "completed";
    return `
      <div class="doc-actions">
        ${completed ? `<div class="dropdown" id="export-dropdown">
          <button class="btn btn-primary" id="export-toggle">下载译本 <span style="font-size:10px">▾</span></button>
          <div class="dropdown-menu">${EXPORTS.map(([key, label, ext]) => `<a href="/api/documents/${attr(doc.id)}/download?fmt=${encodeURIComponent(key)}"><span class="ext">${esc(ext)}</span>${esc(label)}</a>`).join("")}</div>
        </div>` : ""}
        ${active ? `<button class="btn btn-ghost" id="cancel-job">停止任务</button>` : ""}
        ${!active && job.status === "failed" ? `<button class="btn btn-primary" id="retry-resume">继续翻译</button>` : ""}
        ${!active && ["failed", "cancelled", "completed", "paused"].includes(job.status) ? `<button class="btn btn-ghost" id="retry-fresh">${completed ? "重新翻译" : "从头翻译"}</button>` : ""}
        <button class="btn btn-ghost" id="edit-prompt">AI 提示</button>
        <button class="btn btn-quiet" id="delete-doc">删除</button>
      </div>`;
  }

  async function renderDoc(id) {
    const doc = await api(`/api/documents/${id}`);
    if (state.doc && state.doc.id === id) {
      // preserve active tab when auto-refreshing after completion
      state.doc = doc;
    } else {
      state.doc = doc;
      state.docTab = "reader";
    }
    const analysis = doc.analysis || {};
    const title = analysis.title_translation || doc.title;
    crumb(doc.title, { href: "#/library", label: "我的书架" });
    $("#page").innerHTML = `
      <div class="doc-hero">
        <div class="big-cover" style="background:${cover(doc.id)}">${esc(initial(title))}</div>
        <div class="doc-hero-main">
          <div class="eyebrow" style="margin-bottom:5px">${esc((doc.ext || "").replace(".", "").toUpperCase())} · ${esc(doc.filename)}</div>
          <h1>${esc(doc.title)}</h1>
          ${analysis.title_translation && analysis.title_translation !== doc.title ? `<div class="translated">${esc(analysis.title_translation)}</div>` : ""}
          <div class="doc-meta">
            ${doc.author ? `<span>✎ ${esc(doc.author)}</span>` : ""}
            <span>${esc(lang(doc.source_lang))} → ${esc(lang(doc.target_lang))}</span>
            ${doc.metadata && doc.metadata.vision_ocr ? `<span class="badge-soft">AI 视觉 OCR</span>` : `<span class="badge-soft">PDF 文本层</span>`}
            <span>${esc(fmtNumber(doc.char_count))} 字符</span>
            ${doc.page_count ? `<span>${esc(doc.page_count)} ${doc.ext === '.epub' ? '节' : '页'}</span>` : ""}
            ${doc.ocr_progress && doc.ocr_progress.done ? `<span id="ocr-progress">OCR ${esc(doc.ocr_progress.done)} / ${esc(doc.ocr_progress.total)} 页</span>` : ""}
            <span>${esc(doc.stats.translated)} / ${esc(doc.stats.translatable)} 段已译</span>
            <span>${esc(fmtDate(doc.created_at))}</span>
          </div>
          ${docActions(doc)}
        </div>
      </div>
      ${jobBanner(doc.job)}
      <div class="tabs" id="doc-tabs">
        <button class="tab ${state.docTab === "reader" ? "active" : ""}" data-tab="reader">双语阅读</button>
        <button class="tab ${state.docTab === "glossary" ? "active" : ""}" data-tab="glossary">术语表 <span class="count">${doc.glossary.length}</span></button>
        <button class="tab ${state.docTab === "info" ? "active" : ""}" data-tab="info">文档信息</button>
      </div>
      <div id="tab-content"></div>`;
    bindDocActions(doc);
    $$("#doc-tabs .tab").forEach((tab) => tab.addEventListener("click", () => {
      state.docTab = tab.dataset.tab;
      $$("#doc-tabs .tab").forEach((t) => t.classList.toggle("active", t === tab));
      renderTabContent();
    }));
    await renderTabContent();
    if (isActive(doc.job)) startJobPolling(id);
  }

  function bindDocActions(doc) {
    const toggle = $("#export-toggle");
    if (toggle) {
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
        $("#export-dropdown").classList.toggle("open");
      });
      document.addEventListener("click", () => { const d = $("#export-dropdown"); if (d) d.classList.remove("open"); }, { once: true });
    }
    const cancel = $("#cancel-job");
    if (cancel) cancel.addEventListener("click", async () => {
      if (!(await confirmDialog({ title: "停止翻译？", message: "已完成的段落会保留，之后可以从当前位置继续。", confirm: "停止任务", danger: true }))) return;
      try { await api(`/api/documents/${doc.id}/cancel`, { method: "POST" }); toast("已请求停止任务"); await route(); }
      catch (err) { toast(err.message, "error"); }
    });
    const resume = $("#retry-resume");
    if (resume) resume.addEventListener("click", () => doRetry(doc.id, "resume"));
    const fresh = $("#retry-fresh");
    if (fresh) fresh.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "从头重新翻译？", message: "所有已完成的译文会被清除，并从解析后的段落重新开始。", confirm: "重新开始", danger: true });
      if (ok) doRetry(doc.id, "fresh");
    });
    const editPrompt = $("#edit-prompt");
    if (editPrompt) editPrompt.addEventListener("click", () => openPromptDialog(doc));
    const del = $("#delete-doc");
    if (del) del.addEventListener("click", async () => {
      if (!(await confirmDialog({ title: "删除这份文档？", message: "原文件、译文和导出文件都会从本地删除，无法恢复。", confirm: "永久删除", danger: true }))) return;
      try { await api(`/api/documents/${doc.id}`, { method: "DELETE" }); toast("文档已删除"); location.hash = "#/library"; }
      catch (err) { toast(err.message, "error"); }
    });
  }
  function openPromptDialog(doc) {
    const job = doc.job || {};
    const active = isActive(job);
    const modal = openModal(`
      <h3>AI 提示与速度</h3>
      <p>提示词会注入分析、翻译和审校 prompt。运行中的任务从下一批开始生效；模型思考开关只影响新任务或重试。</p>
      <div class="form-stack" style="margin-top:18px">
        <label>追加提示词<textarea id="prompt-text" rows="6" maxlength="4000" placeholder="例如：保留英文术语对照；公式用 LaTeX；图注不要意译；人名首次出现时附原名。">${esc(doc.custom_prompt || "")}</textarea></label>
        <label class="check"><input type="checkbox" id="prompt-thinking" ${job.thinking_enabled ? "checked" : ""}><span><strong>启用模型思考</strong><span>关闭后更快；复杂数学书可开启</span></span></label>
        ${active ? `<div class="notice">当前任务正在运行：提示词保存后下一批生效；思考开关不会改变正在运行的任务。</div>` : ""}
      </div>
      <div class="modal-actions">
        <button class="btn btn-ghost" type="button" data-modal-cancel>取消</button>
        <button class="btn btn-primary" id="prompt-save">保存</button>
      </div>`);
    $("#prompt-save", modal).addEventListener("click", async () => {
      const btn = $("#prompt-save", modal);
      setLoading(btn, true, "保存中…");
      try {
        const data = await api(`/api/documents/${doc.id}/prompt`, {
          method: "PATCH",
          body: {
            prompt: $("#prompt-text", modal).value,
            thinking: $("#prompt-thinking", modal).checked,
          },
        });
        state.doc.custom_prompt = data.custom_prompt;
        if (state.doc.job) state.doc.job.thinking_enabled = data.thinking_enabled;
        closeModal();
        toast("提示词已保存", "success");
        await renderDoc(doc.id);
      } catch (err) {
        toast(err.message, "error");
        setLoading(btn, false);
      }
    });
  }

  async function doRetry(id, mode) {
    try {
      const data = await api(`/api/documents/${id}/retry`, {
        method: "POST", body: { mode, model: state.defaultModel },
      });
      toast("任务已重新入队");
      await route();
    } catch (err) { toast(err.message, "error"); }
  }

  function startJobPolling(id) {
    clearInterval(state.jobTimer);
    state.jobTimer = setInterval(async () => {
      if (state.route !== "doc" || !state.doc || state.doc.id !== id) { clearInterval(state.jobTimer); return; }
      try {
        const data = await api(`/api/documents/${id}/job`);
        const job = data.job;
        if (data.ocr_progress && data.ocr_progress.done) {
          const el = $("#ocr-progress");
          if (el) el.textContent = `OCR ${data.ocr_progress.done} / ${data.ocr_progress.total} 页`;
        }
        const current = $("#job-banner");
        if (current && job) {
          const wrap = document.createElement("div");
          wrap.innerHTML = jobBanner(job);
          current.replaceWith(wrap.firstElementChild);
        }
        if (job && !isActive(job)) {
          clearInterval(state.jobTimer); state.jobTimer = null;
          if (job.status === "completed") toast("翻译完成，导出文件已生成", "success");
          else if (job.status === "failed") toast("翻译任务失败，可继续或重新开始", "error");
          await renderDoc(id);
        }
      } catch (_) { /* transient */ }
    }, 1800);
  }

  async function renderTabContent() {
    const el = $("#tab-content");
    if (!state.doc || !el) return;
    if (state.docTab === "reader") { await renderReader(el); }
    else if (state.docTab === "glossary") { renderGlossary(el); }
    else { renderInfo(el); }
  }

  // ------------------------------------------------------------------ reader
  async function renderReader(root) {
    const doc = state.doc;
    const r = state.reader;
    const chapterOptions = (doc.chapters || []).map((c) => `<option value="${attr(c)}" ${r.chapter === c ? "selected" : ""}>${esc(c.length > 42 ? c.slice(0, 42) + "…" : c)}</option>`).join("");
    root.innerHTML = `
      <div class="reader-shell">
        <div class="reader-toolbar">
          <div class="segmented" id="mode-switch">
            <button data-mode="bilingual" class="${r.mode === "bilingual" ? "active" : ""}">对照</button>
            <button data-mode="target" class="${r.mode === "target" ? "active" : ""}">译文</button>
            <button data-mode="source" class="${r.mode === "source" ? "active" : ""}">原文</button>
          </div>
          <select id="chapter-select"><option value="">全部章节</option>${chapterOptions}</select>
          <div class="font-controls"><button data-font="small" title="小号">A−</button><button data-font="medium" title="中号">A</button><button data-font="large" title="大号">A＋</button></div>
          <div class="reader-search"><input id="reader-search" type="search" placeholder="搜索原文或译文" value="${attr(r.q)}"></div>
          <button class="icon-btn" id="reader-refresh" title="刷新当前页">↻</button>
        </div>
        <div class="reader-body font-${attr(r.font)} mode-${attr(r.mode)}" id="reader-body"><div class="spinner-block"><span class="spin"></span>载入段落…</div></div>
        <div class="reader-note">译文由 AI 生成，建议在出版前由人工抽检。原文与译文逐段保存。</div>
      </div>`;
    $("#mode-switch", root).addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mode]"); if (!btn) return;
      r.mode = btn.dataset.mode;
      $$("#mode-switch button", root).forEach((b) => b.classList.toggle("active", b === btn));
      applyReaderPrefs();
    });
    $("#chapter-select", root).addEventListener("change", (e) => { r.chapter = e.target.value; r.offset = 0; loadSegments(true); });
    $$("[data-font]", root).forEach((btn) => btn.addEventListener("click", () => { r.font = btn.dataset.font; applyReaderPrefs(); }));
    let searchTimer;
    $("#reader-search", root).addEventListener("input", (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { r.q = e.target.value.trim(); r.offset = 0; loadSegments(true); }, 350);
    });
    $("#reader-refresh", root).addEventListener("click", () => { r.offset = 0; loadSegments(true); });
    applyReaderPrefs();
    await loadSegments(true);
  }
  function renderMath(root) {
    if (!window.renderMathInElement || !root) return;
    try {
      window.renderMathInElement(root, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
        strict: false,
        trust: false,
      });
    } catch (err) {
      console.warn("KaTeX render failed", err);
    }
  }
  function applyReaderPrefs() {
    const body = $("#reader-body");
    if (!body) return;
    body.className = `reader-body font-${state.reader.font} mode-${state.reader.mode}`;
  }
  async function loadSegments(reset) {
    const r = state.reader;
    if (r.loading) return;
    r.loading = true;
    const body = $("#reader-body");
    if (!body) { r.loading = false; return; }
    try {
      if (reset) {
        body.innerHTML = `<div class="spinner-block"><span class="spin"></span>载入段落…</div>`;
        r.offset = 0;
      }
      const params = new URLSearchParams({ offset: String(r.offset), limit: String(r.limit), q: r.q, chapter: r.chapter });
      const data = await api(`/api/documents/${state.doc.id}/segments?${params}`);
      if (reset) body.innerHTML = "";
      if (!data.items.length && r.offset === 0) {
        body.innerHTML = `<div class="reader-empty"><div class="glyph">◌</div><p>${r.q || r.chapter ? "没有匹配的段落" : "正在等待译文，请稍后刷新。"}</p></div>`;
      } else {
        body.insertAdjacentHTML("beforeend", data.items.map(segmentHtml).join(""));
        renderMath(body);
      }
      r.offset += data.items.length;
      r.hasMore = data.has_more;
      $$(".reader-load", body).forEach((el) => el.remove());
      if (data.has_more) {
        body.insertAdjacentHTML("beforeend", `<div class="reader-load"><button class="btn btn-ghost btn-small" id="load-more">加载更多段落</button></div>`);
        $("#load-more").addEventListener("click", () => loadSegments(false));
      }
    } catch (err) {
      if (reset) body.innerHTML = `<div class="reader-empty"><p>${esc(err.message)}</p></div>`;
      else toast(err.message, "error");
    } finally {
      r.loading = false;
    }
  }
  function mathText(raw, formula) {
    const safe = esc(raw || "");
    if (formula && !/[$]|\\\(|\\\[/.test(raw || "")) return `$$${safe}$$`;
    return safe;
  }
  function segmentHtml(seg) {
    const level = Number(seg.level || 0);
    const classes = ["seg", `seg-${seg.block_type || "paragraph"}`];
    if (level) classes.push("seg-heading", `level-${level}`);
    if (!seg.translatable) classes.push("seg-skipped");
    const isFormula = seg.block_type === "formula";
    const source = mathText(seg.source_text, isFormula);
    const target = seg.translated_text
      ? mathText(seg.translated_text, isFormula)
      : (!seg.translatable ? source : `<span style="color:var(--faint);font-style:italic">待翻译…</span>`);
    const image = seg.asset_path
      ? `<figure class="seg-figure"><img src="/api/documents/${encodeURIComponent(state.doc.id)}/assets/${encodeURIComponent(seg.asset_path)}" alt="${esc(seg.source_text || "插图")}" loading="lazy"></figure>`
      : "";
    return `<div class="${classes.join(" ")}">
      <span class="seg-marker">${seg.page ? "p." + seg.page : seg.idx + 1}</span>
      ${image}
      <div class="seg-pair">
        <div class="seg-source"><div class="seg-text">${source}</div></div>
        <div class="seg-target"><div class="seg-text">${target}</div></div>
      </div>
    </div>`;
  }

  // ------------------------------------------------------------------ glossary
  function renderGlossary(root) {
    const doc = state.doc;
    const rows = doc.glossary || [];
    root.innerHTML = `<div class="panel">
      <div class="panel-head"><h3>全书术语表</h3><span class="tiny muted">AI 生成 · 可编辑 · 后续翻译会优先采用</span></div>
      ${rows.length ? `<table class="glossary-table"><thead><tr><th style="width:26%">原文</th><th style="width:26%">译文</th><th>备注</th><th style="width:30px"></th></tr></thead><tbody>
        ${rows.map((t) => `<tr data-term="${t.id}">
          <td class="src">${esc(t.source_term)}</td>
          <td><input class="g-target" value="${attr(t.target_term)}" data-original="${attr(t.target_term)}" aria-label="译文"></td>
          <td><input class="g-note" value="${attr(t.note || "")}" data-original="${attr(t.note || "")}" aria-label="备注"></td>
          <td><button class="icon-btn del" title="删除" data-del="${t.id}">×</button></td>
        </tr>`).join("")}
      </tbody></table>` : `<div class="empty" style="border:0;background:transparent;padding:40px"><p class="muted">还没有术语。翻译开始后会自动抽取，也可以手动添加。</p></div>`}
      <form class="add-term" id="add-term">
        <input name="source" placeholder="原文术语" required maxlength="300">
        <input name="target" placeholder="译文" required maxlength="300">
        <input name="note" placeholder="备注（可选）" maxlength="500">
        <button class="btn btn-small btn-primary" type="submit">添加</button>
      </form>
    </div>`;
    $$(".glossary-table input", root).forEach((input) => {
      input.addEventListener("blur", async () => {
        if (input.value === input.dataset.original) return;
        const tr = input.closest("tr");
        const id = tr.dataset.term;
        const target = $(".g-target", tr).value;
        const note = $(".g-note", tr).value;
        try {
          const data = await api(`/api/documents/${doc.id}/glossary/${id}`, { method: "PATCH", body: { target, note } });
          doc.glossary = data.items;
          input.dataset.original = input.value;
          toast("术语已更新", "success");
        } catch (err) { toast(err.message, "error"); }
      });
    });
    $$("[data-del]", root).forEach((btn) => btn.addEventListener("click", async () => {
      try {
        const data = await api(`/api/documents/${doc.id}/glossary/${btn.dataset.del}`, { method: "DELETE" });
        doc.glossary = data.items;
        renderGlossary(root);
        toast("术语已删除");
      } catch (err) { toast(err.message, "error"); }
    }));
    $("#add-term", root).addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      try {
        const data = await api(`/api/documents/${doc.id}/glossary`, {
          method: "POST",
          body: { source: form.source.value, target: form.target.value, note: form.note.value },
        });
        doc.glossary = data.items;
        renderGlossary(root);
        toast("术语已添加", "success");
      } catch (err) { toast(err.message, "error"); }
    });
  }

  // ------------------------------------------------------------------ info
  function renderInfo(root) {
    const doc = state.doc;
    const a = doc.analysis || {};
    const job = doc.job || {};
    const m = doc.metadata || {};
    root.innerHTML = `<div class="info-grid">
      <div class="card">
        <div class="panel-head"><h3>AI 阅读笔记</h3><span class="badge">${esc(job.model || "—")}</span></div>
        <div class="info-block">
          ${a.summary ? `<h3>内容概要</h3><p>${esc(a.summary)}</p>` : `<p class="muted">AI 分析尚未完成。任务开始后会自动生成全书摘要与翻译策略。</p>`}
          ${a.translation_strategy ? `<div class="divider"></div><h3>翻译策略</h3><p>${esc(a.translation_strategy)}</p>` : ""}
          ${a.tone || a.target_reader || a.genre ? `<div class="divider"></div><div class="kv"><span>文类</span><strong>${esc(a.genre || "—")}</strong></div><div class="kv"><span>语气</span><strong>${esc(a.tone || "—")}</strong></div><div class="kv"><span>目标读者</span><strong>${esc(a.target_reader || "—")}</strong></div>` : ""}
        </div>
      </div>
      <div class="card">
        <div class="panel-head"><h3>任务与文件</h3></div>
        <div class="info-block">
          <div class="kv"><span>状态</span><strong>${esc(statusLabel(job.status || doc.status))}</strong></div>
          <div class="kv"><span>阶段</span><strong>${esc(stageLabel(job.stage))}</strong></div>
          <div class="kv"><span>模型</span><strong>${esc(job.model || "—")}</strong></div>
          <div class="kv"><span>思考模式</span><strong>${job.thinking_enabled ? "开启" : "关闭（快速）"}</strong></div>
          <div class="kv"><span>累计 Token</span><strong>${esc(fmtNumber(doc.tokens_total))}</strong></div>
          <div class="kv"><span>本次任务</span><strong>${esc(fmtNumber(job.tokens_total))}</strong></div>
          <div class="kv"><span>原文</span><strong>${esc(doc.filename)}</strong></div>
          <div class="kv"><span>格式 / 篇幅</span><strong>${esc((doc.ext || "").replace(".", "").toUpperCase())} · ${esc(doc.page_count || "—")}</strong></div>
          ${m.vision_ocr ? `<div class="kv"><span>文字识别</span><strong>AI 视觉 OCR · ${esc(m.ocr_pages || doc.page_count)} 页${m.ocr_failed_pages && m.ocr_failed_pages.length ? ` · ${m.ocr_failed_pages.length} 页失败` : ""}</strong></div>` : ""}
          ${m.figures_count ? `<div class="kv"><span>原书插图</span><strong>${esc(m.figures_count)} 张</strong></div>` : ""}
          <div class="kv"><span>导入时间</span><strong>${esc(fmtTime(doc.created_at))}</strong></div>
          <div class="kv"><span>文件大小</span><strong>${esc(fmtBytes(doc.size_bytes))}</strong></div>
          <div class="divider"></div>
          ${doc.custom_prompt ? `<div class="divider"></div><h3>AI 追加提示</h3><p class="hint" style="white-space:pre-wrap">${esc(doc.custom_prompt)}</p>` : ""}
          <div class="divider"></div>
          <h3>导出</h3>
          ${doc.exports && doc.exports.length ? `<div class="export-list">${doc.exports.map((e) => {
            const label = (EXPORTS.find((x) => x[0] === e.key) || [e.key, e.filename, ""])[1];
            const ext = (EXPORTS.find((x) => x[0] === e.key) || ["", "", ""])[2];
            return `<a class="export-item" href="/api/documents/${attr(doc.id)}/download?fmt=${encodeURIComponent(e.key)}"><span class="ext">${esc(ext)}</span>${esc(label)}<span>↓</span></a>`;
          }).join("")}</div>` : `<p class="hint">翻译完成后会生成 Word、EPUB、Markdown、HTML 和纯文本版本。</p>`}
        </div>
      </div>
    </div>`;
  }

  // ------------------------------------------------------------------ bootstrap
  function bindShell() {
    $$(".auth-tab").forEach((tab) => tab.addEventListener("click", () => switchAuthTab(tab.dataset.authTab)));
    $("#login-form").addEventListener("submit", (e) => { e.preventDefault(); doAuth(e.currentTarget, "/api/auth/login"); });
    $("#register-form").addEventListener("submit", (e) => { e.preventDefault(); doAuth(e.currentTarget, "/api/auth/register"); });
    $("#demo-login").addEventListener("click", () => {
      const form = $("#login-form");
      form.email.value = "demo@folio.local";
      form.password.value = "folio2026";
      form.requestSubmit();
    });
    $("#logout-btn").addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      showAuth(); location.hash = "";
    });
    $("#mobile-menu").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
    $$("#main-nav a").forEach((a) => a.addEventListener("click", () => $(".sidebar").classList.remove("open")));
    window.addEventListener("hashchange", route);
  }

  async function bootstrap() {
    bindShell();
    try {
      const data = await api("/api/auth/me");
      if (data.user) { state.user = data.user; await enterApp(); }
      else showAuth();
    } catch (_) { showAuth(); }
  }

  bootstrap();
})();
