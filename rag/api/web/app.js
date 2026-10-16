"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  active: "启用",
  disabled: "停用",
  archived: "归档",
  pending: "排队中",
  running: "处理中",
  succeeded: "处理完成",
  failed: "失败",
  ready: "可启用",
  empty: "无可检索正文",
  validation: "校验",
  parsing: "解析",
  chunking: "切块",
  publishing: "发布文件",
  persisting: "保存结果",
  interrupted: "中断待恢复",
  superseded: "已被替代",
  deferred: "已延期",
};
const errors = {
  EMPTY_UPLOAD: "文件不能为空",
  UPLOAD_TOO_LARGE: "文件或请求超过服务限制",
  INVALID_DOCUMENT_FORMAT: "仅支持 PDF 和 DOCX",
  INVALID_REQUEST: "请检查填写的参数",
  INVALID_PATH: "请填写相对逻辑路径",
  UNSAFE_PATH: "逻辑路径不能包含上级目录或绝对路径",
  RESOURCE_DISABLED: "知识库或文档已停用",
  PROCESSING_MODELS_MISSING: "本地模型文件缺失，请检查模型目录",
  PROCESSING_KEY_REQUIRED: "处理功能需要配置 DEEPSEEK_API_KEY",
  PROCESSING_FAILED: "解析失败，请检查文件是否损坏及处理环境",
  PROCESSING_IDENTITY_CHANGED: "处理环境已变化，请重新提交此版本",
  PROCESSING_TEMPORARY: "处理依赖暂不可用",
  DATABASE_ERROR: "数据库暂不可用",
  SEARCH_UNAVAILABLE: "Elasticsearch 暂不可用",
  SEARCH_ERROR: "检索服务请求失败，请检查配置",
  INDEX_NOT_FOUND: "索引尚未建立，请等待首次同步",
  INDEX_MODEL_MISMATCH: "查询模型与索引不一致，需要重新同步",
  BUILD_NOT_READY: "该处理结果没有可启用的正文",
  BUILD_INCOMPLETE: "处理结果不完整，不能启用",
  TASK_RETRY_NOT_ALLOWED: "只有失败任务可以重试",
  INDEX_SYNC_NOT_FAILED: "当前索引任务不能重试",
  TASK_INTERRUPTED: "处理曾中断，将由租约机制恢复",
  INDEX_SYNC_INTERRUPTED: "同步曾中断，将由租约机制恢复",
};
function node(tag, text, attrs = {}) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else n.setAttribute(k, v);
  }
  return n;
}
function button(text, action, disabled = false, cls = "secondary") {
  const n = node("button", text, { type: "button", class: cls });
  n.disabled = disabled;
  n.onclick = action;
  return n;
}
function badge(value, text) {
  return node("span", text || labels[value] || value, {
    class:
      "badge" +
      (["failed", "pending", "disabled", "empty"].includes(value)
        ? " warning"
        : ""),
  });
}
function notice(message, kind = "") {
  return node("div", message, { class: "notice " + kind });
}
function errorText(e) {
  return e.code
    ? `${errors[e.code] || e.message || "操作失败"}（${e.code}）`
    : e.message || "无法连接 API，请检查服务状态。";
}
function date(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
}
function table(headers, rows) {
  const t = node("table"),
    h = node("tr"),
    body = node("tbody");
  headers.forEach((x) => h.append(node("th", x)));
  const head = node("thead");
  head.append(h);
  t.append(head, body);
  rows.forEach((row) => {
    const tr = node("tr");
    row.forEach((value) => {
      const td = node("td");
      td.append(value instanceof Node ? value : node("span", value ?? "—"));
      tr.append(td);
    });
    body.append(tr);
  });
  return t;
}
let saved = {};
try {
  saved = JSON.parse(localStorage.getItem("rag-ui") || "{}");
} catch {}
const state = {
  kb: saved.kb || null,
  doc: saved.doc || null,
  task: saved.task || null,
  tab: ["documents", "tasks", "search"].includes(saved.tab)
    ? saved.tab
    : "documents",
  docOffset: 0,
  taskOffset: 0,
  versionOffset: 0,
  buildOffset: 0,
  attemptOffset: 0,
  epoch: 0,
  controllers: new Set(),
  timer: null,
  runtime: null,
  checked: null,
  verifiedRevision: null,
  busy: false,
  uploading: false,
  searchToken: 0,
  refreshing: false,
};
const renderKeys = new Map();
function renderChanged(key, value, render) {
  const encoded = JSON.stringify(value);
  if (renderKeys.get(key) !== encoded) {
    render();
    renderKeys.set(key, encoded);
  }
}
function save() {
  try {
    localStorage.setItem(
      "rag-ui",
      JSON.stringify({
        kb: state.kb,
        doc: state.doc,
        task: state.task,
        tab: state.tab,
      }),
    );
  } catch {}
}
function invalidate() {
  state.epoch++;
  state.controllers.forEach((c) => c.abort());
  state.controllers.clear();
  clearTimeout(state.timer);
  save();
}
async function api(path, options = {}) {
  const c = new AbortController();
  state.controllers.add(c);
  const timeout = setTimeout(
    () => c.abort("timeout"),
    options.timeout || 30000,
  );
  try {
    const r = await fetch("/api/v1" + path, {
      ...options,
      signal: c.signal,
      headers: options.body
        ? { "Content-Type": "application/json" }
        : undefined,
    });
    let data;
    try {
      data = await r.json();
    } catch {
      throw Error("API 返回了无法读取的响应");
    }
    if (!r.ok) {
      const e = Error(data.error);
      e.code = data.error_code;
      throw e;
    }
    return data;
  } catch (e) {
    if (c.signal.reason === "timeout")
      throw Error("请求超时。写入可能已提交，请先刷新任务或版本状态再重试。");
    throw e;
  } finally {
    clearTimeout(timeout);
    state.controllers.delete(c);
  }
}
function pager(page, change) {
  const box = node("div", null, { class: "pager" });
  box.append(
    node(
      "span",
      `共 ${page.total} 条 · ${page.total ? page.offset + 1 : 0}–${Math.min(page.offset + page.limit, page.total)}`,
    ),
    button(
      "上一页",
      () => change(Math.max(0, page.offset - page.limit)),
      page.offset === 0,
    ),
    button(
      "下一页",
      () => change(page.offset + page.limit),
      page.offset + page.limit >= page.total,
    ),
  );
  return box;
}
function schedule() {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => refresh(), document.hidden ? 10000 : 2000);
}
function switchTab(tab) {
  state.tab = tab;
  invalidate();
  document
    .querySelectorAll(".workspace")
    .forEach((n) => (n.hidden = n.id !== tab));
  document
    .querySelectorAll("[data-tab]")
    .forEach((n) =>
      n.setAttribute("aria-selected", String(n.dataset.tab === tab)),
    );
  refresh();
}
function chooseDocument(id) {
  state.doc = id;
  state.versionOffset = state.buildOffset = 0;
  invalidate();
  refresh();
}
function waitingHint(task) {
  const age = (Date.now() - new Date(task.created_at)) / 1000;
  if (age < 60 || !["pending", "running"].includes(task.status)) return "";
  let hint = `已等待 ${Math.floor(age / 60)} 分钟。`;
  const rt = state.runtime;
  if (rt?.signal !== "fresh")
    return hint + "监督信号缺失或过期，无法确认 worker；请运行状态查询命令。";
  if (rt.worker.state !== "running")
    return hint + "受管 worker 尚未运行，请查看运行状态。";
  if (rt.dependencies.mysql?.state === "unavailable")
    return hint + "数据库不可用。";
  return hint + "worker 进程存活；任务可能正在排队、重试或等待中断租约恢复。";
}
function renderRuntime(value) {
  state.runtime = value;
  const box = $("runtime");
  box.replaceChildren();
  if (value.signal !== "fresh") {
    box.append(
      badge(
        "pending",
        value.signal === "stale" ? "监督信号已过期" : "无法确认 worker",
      ),
      node("small", " 手动运行的 worker 不受监督"),
    );
  } else {
    const names = {
      running: "进程存活",
      stopped: "已停止",
      waiting: "等待依赖",
      backoff: "等待重启",
      unknown: "未知",
    };
    box.append(
      node(
        "span",
        `${value.mode === "demo" ? "演示模式 · " : ""}API ${names[value.api.state]} · worker ${names[value.worker.state]}`,
      ),
    );
    for (const [name, item] of Object.entries(value.dependencies)) {
      if (item.state === "unavailable")
        box.append(notice(`${name} 不可用`, "warning"));
    }
  }
  const backup = value.backup;
  $("backup").replaceChildren(
    value.mode === "demo"
      ? node("span", "演示模式不自动备份")
      : backup
      ? node(
          "span",
          `${backup.status === "running" ? "备份中" : backup.status === "failed" ? "最近备份失败" : "备份成功"} · 上次成功 ${date(backup.last_success_at)}`,
        )
      : node("span", "尚无受管备份记录"),
  );
  if (backup?.error_code)
    $("backup").append(node("small", ` ${backup.error_code}`));
}
function renderIndex(value) {
  const sync = value.sync_task,
    box = $("index");
  box.replaceChildren();
  if (sync.paused) box.append(badge("pending", "索引同步已暂停"));
  else if (sync.status === "failed")
    box.append(badge("failed", "索引同步失败"));
  else if (
    state.checked?.sync_lag === false &&
    state.verifiedRevision === sync.desired_revision
  )
    box.append(badge("ready", "已同步"));
  else if (sync.status === "running" || sync.status === "pending")
    box.append(badge("pending", "等待索引同步"));
  else box.append(badge("pending", "数据库已完成，等待实际核验"));
  box.append(
    node("small", ` 修订 ${sync.applied_revision} / ${sync.desired_revision}`),
  );
  const actions = node("div", null, { class: "actions" });
  actions.append(button("核验实际索引", verifyIndex, state.busy));
  if (sync.status === "failed")
    actions.append(
      button(
        "重试同步",
        () =>
          mutate(
            `/knowledge-bases/${state.kb}/index-sync/retry`,
            "POST",
            {},
            box,
          ),
        state.busy,
      ),
    );
  box.append(actions);
  if (sync.error_code)
    box.append(
      notice(
        errorText({ code: sync.error_code, message: sync.error }),
        "error",
      ),
    );
  if (state.checked?.sync_lag === true)
    box.append(
      node("small", "新版本尚未全部进入检索；已替换或停用内容会被过滤。"),
    );
}
async function verifyIndex() {
  const epoch = state.epoch,
    kb = state.kb;
  if (!kb) return;
  try {
    const r = await api(`/knowledge-bases/${kb}/index-status?db_only=false`);
    if (epoch !== state.epoch) return;
    state.checked = r;
    state.verifiedRevision = r.sync_task.desired_revision;
    renderIndex(r);
  } catch (e) {
    if (epoch === state.epoch && e.name !== "AbortError")
      $("index").append(notice(errorText(e), "error"));
  }
}
function renderDocuments(page) {
  const box = $("document-list");
  box.replaceChildren();
  if (!page.total)
    box.append(node("p", "暂无文档，请先上传。", { class: "empty" }));
  else {
    const t = table(
      ["文档 / 路径", "状态", "当前版本"],
      page.items.map((d) => {
        const cell = node("div");
        cell.append(
          button(
            d.title || d.source_path,
            () => chooseDocument(d.id),
            false,
            "link",
          ),
          node("div", d.source_path, { class: "muted" }),
        );
        return [
          cell,
          badge(d.status),
          d.active_build_id ? `build #${d.active_build_id}` : "未启用",
        ];
      }),
    );
    box.append(t);
  }
  box.append(
    pager(page, (offset) => {
      state.docOffset = offset;
      invalidate();
      refresh();
    }),
  );
}
function renderDocument(doc, versions, builds) {
  const box = $("document-detail");
  box.replaceChildren(node("h2", doc.title), node("p", doc.source_path));
  const actions = node("div", null, { class: "actions" });
  actions.append(
    badge(doc.status),
    button(
      doc.status === "active" ? "停用文档" : "恢复文档",
      () => {
        if (
          confirm(
            `确认${doc.status === "active" ? "停用" : "恢复"}文档“${doc.title}”？`,
          )
        )
          mutate(
            `/documents/${doc.id}/status`,
            "PATCH",
            { status: doc.status === "active" ? "disabled" : "active" },
            box,
          );
      },
      state.busy || doc.status === "archived",
    ),
  );
  box.append(actions);
  box.append(
    notice(
      doc.active_build_id
        ? `当前启用 build #${doc.active_build_id} · ${doc.active_eligible_chunk_count} 个可检索块`
        : "尚未启用任何处理结果；处理完成后请显式启用。",
    ),
  );
  box.append(node("h3", "处理结果"));
  box.append(
    table(
      ["Build / 原文件版本", "结果", "操作"],
      builds.items.map((b) => {
        const action = button(
          b.is_active ? "当前启用" : "启用 / 回退",
          () => {
            if (
              confirm(
                `将“${doc.title}”切换到 build #${b.id}（原文件版本 #${b.document_version_id}）？切换后将等待索引同步。`,
              )
            )
              mutate(
                `/documents/${doc.id}/active-build`,
                "PUT",
                { build_id: b.id },
                box,
              );
          },
          state.busy ||
            b.is_active ||
            !b.can_activate ||
            doc.status !== "active",
        );
        return [
          `#${b.id} / 版本 #${b.document_version_id}`,
          node(
            "span",
            `${b.is_active ? "当前启用" : labels[b.status] || b.status} · ${b.chunk_count} 块`,
          ),
          action,
        ];
      }),
    ),
  );
  box.append(
    pager(builds, (offset) => {
      state.buildOffset = offset;
      invalidate();
      refresh();
    }),
  );
  box.append(node("h3", "原文件版本"));
  box.append(
    table(
      ["版本 / 时间", "文件身份", "操作"],
      versions.items.map((v) => {
        const cell = node("div");
        cell.append(
          node("div", `${v.original_filename} · ${v.file_size} 字节`),
        );
        const detail = node("details");
        detail.append(node("summary", "SHA256"), node("pre", v.source_sha256));
        cell.append(detail);
        return [
          `#${v.id}\n${date(v.created_at)}`,
          cell,
          button(
            "重新提交处理",
            () => mutate(`/versions/${v.id}/processing-tasks`, "POST", {}, box),
            state.busy || doc.status !== "active",
          ),
        ];
      }),
    ),
  );
  box.append(
    pager(versions, (offset) => {
      state.versionOffset = offset;
      invalidate();
      refresh();
    }),
  );
}
function taskActions(task) {
  const box = node("div", null, { class: "actions" });
  box.append(
    button("查看详情", () => {
      state.task = task.id;
      state.attemptOffset = 0;
      invalidate();
      refresh();
    }),
  );
  if (task.status === "failed") {
    const changed = task.error_code === "PROCESSING_IDENTITY_CHANGED";
    box.append(
      button(
        changed ? "重新提交" : "重试",
        () =>
          mutate(
            changed
              ? `/versions/${task.document_version_id}/processing-tasks`
              : `/processing-tasks/${task.id}/retry`,
            "POST",
            {},
            $("task-detail"),
          ),
        state.busy,
      ),
    );
  }
  return box;
}
function renderTasks(page) {
  const box = $("task-list");
  box.replaceChildren();
  if (!page.total) box.append(node("p", "暂无处理任务。", { class: "empty" }));
  else
    box.append(
      table(
        ["任务 / 原文件版本", "状态与阶段", "尝试 / 时间", "操作"],
        page.items.map((t) => {
          const status = node("div");
          status.append(
            badge(t.status),
            node("div", labels[t.stage] || t.stage),
          );
          if (t.error_code)
            status.append(
              notice(
                errorText({ code: t.error_code, message: t.error }),
                "error",
              ),
            );
          const hint = waitingHint(t);
          if (hint) status.append(node("small", hint));
          return [
            `#${t.id} / 版本 #${t.document_version_id}`,
            status,
            `${t.attempt_count} 次\n${date(t.updated_at)}`,
            taskActions(t),
          ];
        }),
      ),
    );
  box.append(
    pager(page, (offset) => {
      state.taskOffset = offset;
      invalidate();
      refresh();
    }),
  );
}
function renderTask(task) {
  const box = $("task-detail");
  box.hidden = false;
  box.replaceChildren(
    node("h2", `任务 #${task.id} · 原文件版本 #${task.document_version_id}`),
    badge(task.status),
    node(
      "p",
      `阶段：${labels[task.stage] || task.stage} · build ${task.build_id ? "#" + task.build_id : "尚未生成"}`,
    ),
  );
  if (task.status === "succeeded")
    box.append(
      notice(
        task.can_activate
          ? "处理完成；请在文档版本详情显式启用，再等待索引同步。"
          : "处理完成，但结果为空或不可启用。",
        task.can_activate ? "" : "warning",
      ),
    );
  if (task.error_code)
    box.append(
      notice(
        errorText({ code: task.error_code, message: task.error }),
        "error",
      ),
    );
  box.append(
    taskActions(task),
    table(
      ["尝试", "状态 / 阶段", "时间", "错误码"],
      task.attempts.items.map((a) => [
        a.ordinal,
        `${labels[a.status] || a.status} / ${labels[a.stage] || a.stage}`,
        `${date(a.started_at)} → ${date(a.finished_at)}`,
        a.error_code,
      ]),
    ),
  );
  box.append(
    pager(task.attempts, (offset) => {
      state.attemptOffset = offset;
      invalidate();
      refresh();
    }),
  );
}
async function refresh() {
  const epoch = state.epoch;
  if (state.refreshing) {
    schedule();
    return;
  }
  state.refreshing = true;
  try {
    const runtimePromise = api("/runtime-status").catch(() => null);
    if (!state.kb) {
      const rt = await runtimePromise;
      if (rt && epoch === state.epoch) renderRuntime(rt);
      return;
    }
    const kb = state.kb;
    const requests = [
      runtimePromise,
      api(`/knowledge-bases/${kb}/index-status`),
    ];
    if (state.tab === "documents") {
      requests.push(
        api(
          `/knowledge-bases/${kb}/documents?limit=10&offset=${state.docOffset}${$("document-filter").value ? "&status=" + $("document-filter").value : ""}`,
        ),
      );
      if (state.doc)
        requests.push(
          api(`/documents/${state.doc}`),
          api(
            `/documents/${state.doc}/versions?limit=10&offset=${state.versionOffset}`,
          ),
          api(
            `/documents/${state.doc}/builds?limit=10&offset=${state.buildOffset}`,
          ),
        );
    } else if (state.tab === "tasks") {
      requests.push(
        api(
          `/knowledge-bases/${kb}/processing-tasks?limit=10&offset=${state.taskOffset}${$("task-filter").value ? "&status=" + $("task-filter").value : ""}`,
        ),
      );
      if (state.task)
        requests.push(
          api(
            `/processing-tasks/${state.task}?limit=10&offset=${state.attemptOffset}`,
          ),
        );
    }
    const [rt, index, page, detail, versions, builds] =
      await Promise.all(requests);
    if (epoch !== state.epoch) return;
    $("connection").hidden = true;
    if (rt) {
      state.runtime = rt;
      renderChanged(
        "runtime",
        [
          rt.signal,
          rt.mode,
          rt.supervisor,
          rt.api.state,
          rt.worker.state,
          Object.entries(rt.dependencies).map(([name, item]) => [
            name,
            item.state,
          ]),
          rt.backup?.status,
          rt.backup?.last_success_at,
          rt.backup?.error_code,
        ],
        () => renderRuntime(rt),
      );
    } else {
      state.runtime = null;
      renderKeys.delete("runtime");
      $("runtime").replaceChildren(
        notice("运行状态暂不可读，无法确认 worker。", "warning"),
      );
    }
    if (state.verifiedRevision !== index.sync_task.desired_revision)
      state.checked = null;
    renderIndex(index);
    if (state.tab === "documents") {
      renderChanged("documents", page, () => renderDocuments(page));
      if (detail)
        renderChanged("document", [detail, versions, builds, state.busy], () =>
          renderDocument(detail, versions, builds),
        );
    } else if (state.tab === "tasks") {
      renderChanged(
        "tasks",
        [page, Math.floor(Date.now() / 60000), state.runtime, state.busy],
        () => renderTasks(page),
      );
      if (detail)
        renderChanged("task", [detail, state.busy], () => renderTask(detail));
    }
    if (
      index.sync_task.status === "succeeded" &&
      !index.sync_task.paused &&
      state.verifiedRevision !== index.sync_task.desired_revision
    )
      await verifyIndex();
  } catch (e) {
    if (epoch === state.epoch && e.name !== "AbortError") {
      $("connection").hidden = false;
      $("connection").textContent = errorText(e);
    }
  } finally {
    state.refreshing = false;
    schedule();
  }
}
async function mutate(path, method, body, box) {
  if (state.busy) return;
  state.busy = true;
  const epoch = state.epoch;
  try {
    const r = await api(path, { method, body: JSON.stringify(body) });
    if (epoch !== state.epoch) return;
    state.checked = null;
    state.verifiedRevision = null;
    if (r.task_id) {
      state.task = r.task_id;
      save();
    }
    await refresh();
    box.append(
      notice(
        r.task_id
          ? `已提交任务 #${r.task_id}，可在处理任务中查看。`
          : "操作已提交；如改变当前版本，请等待索引同步。",
      ),
    );
  } catch (e) {
    if (epoch === state.epoch && e.name !== "AbortError")
      box.append(notice(errorText(e), "error"));
  } finally {
    state.busy = false;
  }
}
$("file").addEventListener("change", () => {
  if (!$("source-path").value && $("file").files[0])
    $("source-path").value = $("file").files[0].name;
});
$("upload-form").onsubmit = (event) => {
  event.preventDefault();
  if (state.uploading || !state.kb) return;
  const file = $("file").files[0],
    path = $("source-path").value,
    box = $("upload-result");
  box.replaceChildren();
  if (!file?.size) {
    box.append(notice("文件不能为空（EMPTY_UPLOAD）", "error"));
    return;
  }
  if (!/\.(pdf|docx)$/i.test(path)) {
    box.append(
      notice("逻辑路径仅支持 PDF 和 DOCX（INVALID_DOCUMENT_FORMAT）", "error"),
    );
    return;
  }
  if (
    path.startsWith("/") ||
    path.includes(":") ||
    path.replaceAll("\\", "/").split("/").includes("..")
  ) {
    box.append(notice("请填写安全的相对路径（UNSAFE_PATH）", "error"));
    return;
  }
  const epoch = state.epoch;
  state.uploading = true;
  $("upload-button").disabled = true;
  $("kb").disabled = true;
  const form = new FormData();
  form.append("source_path", path);
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/v1/knowledge-bases/${state.kb}/documents`);
  xhr.timeout = 120000;
  $("upload-progress").hidden = false;
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable)
      $("upload-progress").value = (e.loaded / e.total) * 100;
  };
  xhr.onload = () => {
    try {
      const r = JSON.parse(xhr.responseText);
      if (xhr.status < 200 || xhr.status >= 300) {
        box.append(
          notice(errorText({ code: r.error_code, message: r.error }), "error"),
        );
        return;
      }
      box.append(
        notice(
          `上传完成，任务 #${r.task_id}：${labels[r.status]}${r.reused_version ? " · 复用已有原文件版本" : ""}。处理成功后仍需显式启用。`,
        ),
      );
      if (epoch === state.epoch) {
        state.doc = r.document_id;
        state.task = r.task_id;
        state.versionOffset = state.buildOffset = 0;
        invalidate();
        refresh();
      }
    } catch {
      box.append(
        notice(
          "上传响应无法读取，请先查询文档和任务，确认是否已提交。",
          "error",
        ),
      );
    }
  };
  xhr.onerror = xhr.ontimeout = () =>
    box.append(
      notice(
        "上传连接失败或超时；请先查询文档和任务确认是否已提交，再重试。",
        "error",
      ),
    );
  xhr.onloadend = () => {
    state.uploading = false;
    $("upload-button").disabled = false;
    $("kb").disabled = false;
    $("upload-progress").hidden = true;
  };
  xhr.send(form);
};
$("search-form").onsubmit = async (event) => {
  event.preventDefault();
  const token = ++state.searchToken,
    epoch = state.epoch,
    kb = state.kb,
    box = $("search-results");
  if (!kb) return;
  $("search-button").disabled = true;
  box.replaceChildren(notice("正在检索…"));
  try {
    const body = { query: $("query").value, top_k: Number($("top-k").value) };
    if ($("mode").value) body.mode = $("mode").value;
    const r = await api(`/knowledge-bases/${kb}/search`, {
      method: "POST",
      body: JSON.stringify(body),
      timeout: 120000,
    });
    if (epoch !== state.epoch || token !== state.searchToken) return;
    box.replaceChildren(
      notice(
        `${r.mode} · ${r.contexts.length} 条结果${r.sync_lag ? " · 索引同步滞后，新版本可能尚不可检索" : " · 索引已同步"}`,
        r.sync_lag ? "warning" : "",
      ),
    );
    if (!r.contexts.length)
      box.append(
        node(
          "p",
          r.sync_lag
            ? "当前索引尚未包含全部启用内容，请等待同步后再检索。"
            : "没有匹配的原文。可尝试其他关键词或检索模式。",
          { class: "empty" },
        ),
      );
    for (const [i, c] of r.contexts.entries()) {
      const card = node("article", null, { class: "result" });
      card.append(node("h2", `${i + 1}. ${c.title || c.source_path}`));
      const positions = [
        ...new Set(
          c.sources.flatMap((s) =>
            s.page !== undefined
              ? [`第 ${s.page} 页`]
              : s.content_index !== undefined
                ? [`内容位置 ${s.content_index}`]
                : [],
          ),
        ),
      ];
      card.append(
        node(
          "div",
          [c.source_path, c.heading_path.join(" / "), positions.join("、")]
            .filter(Boolean)
            .join(" · "),
          { class: "source" },
        ),
        node("p", c.text),
      );
      const details = node("details");
      details.append(
        node("summary", "引用详情"),
        node(
          "pre",
          JSON.stringify(
            {
              引用: c.id,
              版本: c.version_id,
              build: c.build_id,
              SHA256: c.document_sha256,
              sources: c.sources,
              derived_sources: c.derived_sources,
              asset_refs: c.asset_refs,
            },
            null,
            2,
          ),
        ),
      );
      card.append(details);
      box.append(card);
    }
  } catch (e) {
    if (
      epoch === state.epoch &&
      token === state.searchToken &&
      e.name !== "AbortError"
    )
      box.replaceChildren(notice(errorText(e), "error"));
  } finally {
    $("search-button").disabled = false;
  }
};
$("kb").onchange = () => {
  renderKeys.clear();
  state.kb = Number($("kb").value);
  state.doc = state.task = null;
  state.docOffset = state.taskOffset = 0;
  state.checked = null;
  state.verifiedRevision = null;
  $("document-detail").replaceChildren(
    node("h2", "版本详情"),
    node("p", "选择文档查看版本。"),
  );
  $("task-detail").hidden = true;
  $("search-results").replaceChildren();
  invalidate();
  refresh();
};
$("document-filter").onchange = () => {
  state.docOffset = 0;
  invalidate();
  refresh();
};
$("task-filter").onchange = () => {
  state.taskOffset = 0;
  invalidate();
  refresh();
};
$("refresh").onclick = () => {
  state.verifiedRevision = null;
  if (!$("kb").options.length) loadKnowledgeBases();
  else refresh();
};
document
  .querySelectorAll("[data-tab]")
  .forEach((n) => (n.onclick = () => switchTab(n.dataset.tab)));
document.addEventListener("visibilitychange", schedule);
window.addEventListener("pagehide", () => {
  invalidate();
  clearTimeout(state.timer);
});
window.addEventListener("pageshow", (e) => {
  if (e.persisted) refresh();
});
async function loadKnowledgeBases() {
  try {
    const items = [];
    let offset = 0,
      page;
    do {
      page = await api(`/knowledge-bases?limit=100&offset=${offset}`);
      items.push(...page.items);
      offset += page.limit;
    } while (offset < page.total);
    $("kb").replaceChildren(
      ...items.map((k) =>
        node(
          "option",
          `${k.name}${k.status !== "active" ? "（" + labels[k.status] + "）" : ""}`,
          { value: k.id },
        ),
      ),
    );
    if (!items.some((k) => k.id === state.kb)) {
      state.kb = items[0]?.id || null;
      state.doc = state.task = null;
    }
    if (state.kb) {
      $("kb").value = state.kb;
      $("upload-button").disabled = false;
    } else {
      $("upload-button").disabled = true;
      $("connection").hidden = false;
      $("connection").textContent = "暂无知识库，请先通过管理 CLI 创建。";
    }
    switchTab(state.tab);
  } catch (e) {
    $("connection").hidden = false;
    $("connection").textContent = errorText(e);
  }
}
loadKnowledgeBases();
