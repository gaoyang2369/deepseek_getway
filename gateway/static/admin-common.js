"use strict";
import { actionMenu } from "./floating.js";
export const $ = (s) => document.querySelector(s);
export const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
export const session = { csrf: sessionStorage.getItem("lab_csrf") || "" };
export const number = (x) => Number(x).toLocaleString("zh-CN");
export function money(x, precise = false) {
  if (x === null || x === undefined) return "金额未知";
  const value = Number(x) / 1e6;
  return (
    "¥" +
    value.toLocaleString("zh-CN", {
      minimumFractionDigits: precise ? 6 : 2,
      maximumFractionDigits:
        precise || (value !== 0 && Math.abs(value) < 0.0001) ? 6 : 4,
    })
  );
}
export const periodUnit = (p) => (p === "day" ? "元／日" : "元／月");
export function stacked(primary, secondary) {
  const d = node("div", undefined, "stacked");
  d.append(primary instanceof Node ? primary : node("span", primary));
  if (secondary) d.append(node("small", secondary));
  return d;
}
export function disclosure(title, ...children) {
  const d = node("details", undefined, "disclosure");
  d.append(node("summary", title), ...children);
  return d;
}
export const moreActions = actionMenu;
export function progress(spent, limit) {
  const d = node("div", undefined, "budget-progress");
  const ratio = limit > 0 ? (spent / limit) * 100 : null;
  const label =
    ratio === null
      ? spent > 0
        ? "已超额 · 上限为零"
        : "未配置额度"
      : (ratio > 0 && ratio < 0.1 ? "<0.1" : number(Number(ratio.toFixed(1)))) +
        "%";
  d.append(node("span", label, spent > limit ? "error-text" : ""));
  const bar = node("progress");
  bar.max = 100;
  bar.value = Math.min(100, ratio ?? (spent ? 100 : 0));
  bar.setAttribute("aria-label", "预算使用率 " + label);
  d.append(bar);
  return d;
}
export const date = (x) =>
  x
    ? new Date(x).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "—";
export const short = (x) => (x ? String(x).slice(0, 8) : "—");
export const operationId = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (v) =>
    v.toString(16).padStart(2, "0"),
  ).join("");
const errorMessages = {
  bill_amount_mismatch:
    "账单金额与当前入账金额不一致，或请求尚未入账，未记录核验结果。请先核查费用差异。",
  invalid_time_range: "请选择有效的时间范围，结束时间必须晚于开始时间。",
  event_requires_reconciliation:
    "费用尚未核对，请先点击“查看请求 / 核对”完成结算；标记处理不会释放预算。",
  official_pricing_unavailable:
    "暂时无法获取 DeepSeek 官网价格，原配置保持不变，请稍后重试。",
  official_pricing_format_changed:
    "DeepSeek 官网价格页面格式发生变化，未导入任何价格；请检查官网后再更新。",
  price_not_found: "这条计价记录已不存在，请刷新页面。",
  invalid_request_parameters:
    "填写内容未通过校验，请检查当前表单的必填项、字段长度和格式。",
  invalid_login: "管理员账号或密码不正确。",
  login_rate_limit: "登录尝试过于频繁，请等待 5 分钟后重试。",
  admin_login_required: "登录已过期，请重新登录。",
  csrf_required: "登录状态已失效，请刷新页面后重新登录。",
  record_conflict: "已有相同记录，请检查名称或学号后重试。",
  student_identity_conflict: "该学号对应的姓名或身份与现有记录不一致。",
  student_number_exists: "该学号已被其他人员使用，请核对后重新填写。",
  student_not_found: "该学生不存在或已删除，请刷新列表。",
  team_unavailable: "项目组不存在或已停用，请重新选择。",
  models_outside_team: "所选模型不在该项目组允许范围内。",
  test_cleanup_requires_mock_environment:
    "仅连接模拟上游的测试环境支持清理测试用量。",
  test_cleanup_requests_in_progress:
    "该组仍有请求正在运行，请等待请求完成后再清理。",
  confirmation_name_mismatch: "输入的项目组名称不一致，请重新填写。",
  student_self_service_required: "学生 Key 由本人登录学生入口领取。",
  production_requires_random_initial_password:
    "正式环境请使用随机临时密码开通账号。",
  invalid_origin: "访问地址与服务器配置不一致，请使用配置的地址登录。",
  expiry_in_past: "到期时间必须晚于当前时间。",
  database_unavailable: "数据库暂时不可用，请稍后重试。",
};
let noticeTimer;
export function notice(message, kind = "error") {
  clearTimeout(noticeTimer);
  const host = $("#notice");
  host.replaceChildren();
  if (!message) return;
  host.className = "toast " + kind;
  const text = node("div", undefined, "toast-copy");
  text.append(
    node("strong", kind === "success" ? "操作成功" : "操作未完成"),
    node("p", message),
  );
  host.append(
    node("span", kind === "success" ? "✓" : "!", "toast-icon"),
    text,
    button("×", () => host.replaceChildren(), "toast-close"),
  );
  if (kind === "success")
    noticeTimer = setTimeout(() => host.replaceChildren(), 6000);
}

export function showError(error) {
  const message = errorMessages[error.message] || error.message;
  if ($("#modal").open) {
    $("#modal-error").textContent = message;
    $("#modal-error").hidden = false;
  } else notice(message);
}

export function confirmAction(title, description, label = "确认删除") {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-description").textContent = description;
    $("#confirm-submit").textContent = label;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    $("#confirm-cancel").onclick = () => finish(false);
    $("#confirm-submit").onclick = () => finish(true);
    dialog.oncancel = (e) => {
      e.preventDefault();
      finish(false);
    };
    dialog.onclose = () => finish(false);
    dialog.showModal();
    $("#confirm-cancel").focus();
  });
}

export async function api(path, method = "GET", body) {
  const r = await fetch("/admin/api" + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": session.csrf,
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 401) {
      $("#console").hidden = true;
      $("#login-screen").hidden = false;
    }
    const message =
      errorMessages[data.error?.message] || "请求未能完成，请稍后重试。";
    const detail = data.error?.details;
    throw new Error(
      data.error?.message === "student_identity_conflict" && detail?.line
        ? `名单第 ${detail.line} 人（学号 ${detail.student_number}）：${message} 本批未添加任何成员，请修改后重试。`
        : message,
    );
  }
  return data;
}

export function button(label, action, cls = "secondary") {
  const b = node("button", label, cls);
  b.type = "button";
  b.addEventListener("click", async () => {
    if (b.disabled) return;
    b.disabled = true;
    try {
      await action();
    } catch (e) {
      showError(e);
    } finally {
      b.disabled = false;
    }
  });
  return b;
}

export function badge(value) {
  return node(
    "span",
    value === true ? "启用" : value === false ? "停用" : value,
    "badge" + (value === false ? " off" : value === "pending" ? " warn" : ""),
  );
}

export function table(headers, rows, cls = "") {
  const wrap = node("div", undefined, "table-wrap"),
    t = node("table"),
    head = node("thead"),
    tr = node("tr");
  wrap.className += " " + cls;
  headers.forEach((h) => {
    const cell = node("th");
    if (h instanceof Node) cell.append(h);
    else cell.textContent = typeof h === "object" ? h.label : h;
    if (h?.numeric) cell.classList.add("numeric");
    if (h?.secondary) cell.classList.add("secondary-column");
    tr.append(cell);
  });
  head.append(tr);
  t.append(head);
  const body = node("tbody");
  for (const row of rows) {
    const line = node("tr");
    for (const [index, value] of row.entries()) {
      const cell = node("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? "—";
      if (headers[index]?.numeric) cell.classList.add("numeric");
      if (headers[index]?.secondary) cell.classList.add("secondary-column");
      cell.dataset.label =
        typeof headers[index] === "object"
          ? headers[index].label || ""
          : headers[index];
      line.append(cell);
    }
    body.append(line);
  }
  t.append(body);
  wrap.append(t);
  if (!rows.length) wrap.append(node("div", "暂无记录", "empty"));
  return wrap;
}

export function actions(...buttons) {
  const d = node("div", undefined, "row-actions");
  d.append(...buttons);
  return d;
}

export function toolbar(title, ...buttons) {
  const d = node("div", undefined, "toolbar");
  d.append(node("h3", title), actions(...buttons));
  return d;
}

export function field(label, name, type = "text", value = "", options) {
  const l = node("label", label),
    input = node(
      options ? "select" : type === "textarea" ? "textarea" : "input",
    );
  input.name = name;
  if (!options && type !== "textarea") input.type = type;
  if (options)
    for (const [v, t] of options) {
      const o = node("option", t);
      o.value = v;
      input.append(o);
    }
  input.value = value;
  input.required = true;
  if (type === "number") {
    input.min = "0";
    input.step = "0.000001";
  }
  if (name === "reason") {
    input.maxLength = 500;
    input.placeholder = "填写原因，如：测试";
  }
  l.append(input);
  return l;
}

export function yuan(value) {
  const text = String(value);
  if (!/^\d+(\.\d{1,6})?$/.test(text))
    throw new Error("请输入非负金额，最多 6 位小数");
  const [a, b = ""] = text.split(".");
  return Number(BigInt(a) * 1000000n + BigInt(b.padEnd(6, "0")));
}

export function models(value) {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
export function teamName(cache, id) {
  const t = cache.teamHistory.find((t) => t.id === id);
  return t
    ? t.name +
        (t.deleted_at ? "（已删除）" : "") +
        (cache.labels?.test_team_ids.includes(id) ? "（测试）" : "")
    : short(id);
}
export function studentName(cache, id) {
  return (
    cache.memberHistory.find((m) => m.student_id === id)?.name || short(id)
  );
}

// Roster pages keep separate cursors, while sharing the same pagination controls.
export async function listPage(path, filters, state) {
  const size = state.size,
    offset = state.offset;
  const params = new URLSearchParams({
    ...filters,
    paginated: "true",
    limit: size,
    offset,
  });
  let result = await api(path + "?" + params);
  const last = Math.max(0, (Math.ceil(result.total / size) - 1) * size);
  if (offset > last) {
    if (state.offset === offset && state.size === size) state.offset = last;
    params.set("offset", last);
    result = await api(path + "?" + params);
  }
  return result;
}

export function listPagination(state, total, label, load) {
  const { size, offset } = state;
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.floor(offset / size) + 1;
  const bar = node("div", undefined, "record-pagination");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", label + "分页");
  async function change(next) {
    state.offset = next;
    await load();
  }
  const select = field("每页条数", "page_size", "text", String(size), [
    ["10", "10 条"],
    ["20", "20 条"],
    ["50", "50 条"],
  ]);
  select.querySelector("select").addEventListener("change", (e) => {
    state.size = Number(e.target.value);
    change(0).catch(showError);
  });
  if (pages === 1) {
    bar.append(node("span", `共 ${total} 条`, "pagination-summary"), select);
    return bar;
  }
  const buttons = node("div", undefined, "pagination-buttons");
  const previous = button("上一页", () => change(Math.max(0, offset - size)));
  previous.disabled = offset === 0;
  buttons.append(previous);
  const visible = new Set([1, pages]);
  for (let n = Math.max(1, current - 2); n <= Math.min(pages, current + 2); n++)
    visible.add(n);
  let last = 0;
  for (const n of [...visible].sort((a, b) => a - b)) {
    if (last && n > last + 1)
      buttons.append(node("span", "…", "pagination-gap"));
    const page = button(String(n), () => change((n - 1) * size));
    page.setAttribute("aria-label", `第 ${n} 页`);
    if (n === current) {
      page.setAttribute("aria-current", "page");
      page.disabled = true;
    }
    buttons.append(page);
    last = n;
  }
  const next = button("下一页", () => change(offset + size));
  next.disabled = offset + size >= total;
  buttons.append(next);
  bar.append(
    node(
      "span",
      `共 ${total} 条 · 第 ${current} / ${pages} 页`,
      "pagination-summary",
    ),
    select,
    buttons,
  );
  return bar;
}
