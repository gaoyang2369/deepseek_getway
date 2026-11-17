import {
  node,
  money,
  date,
  short,
  showError,
  api,
  button,
  badge,
  table,
  actions,
  toolbar,
  field,
  yuan,
  teamName,
  studentName,
} from "./common.js";
import { openForm, closeModal, clearTestUsageForm } from "./forms.js";
let filters = {},
  rejectionFilters = {};
let requestOffset = 0,
  rejectionOffset = 0,
  eventOffset = 0,
  auditOffset = 0;
let eventResolved = "false";
const pageSizes = { requests: 10, rejections: 10, events: 10, audit: 10 };
const outcomes = {
  in_progress: "执行中",
  success: "成功",
  upstream_failure: "上游失败",
  client_disconnected: "客户端断开",
  not_sent: "未发送",
  unknown: "结果未知",
};
const settlements = {
  reserved: "进行中",
  running: "进行中",
  pending: "待核对",
  settled: "已结算",
};
const eventNames = {
  new_source: "新来源提示",
  budget_80: "预算达到 80%",
  budget_100: "预算达到 100%",
  pending_usage: "费用待核对",
  recovery_pending: "重启后费用待核对",
  reservation_exceeded: "实际费用超出预留",
  budget_exhausted: "预算不足",
  rate_limit: "调用频率超限",
  concurrency_limit: "并发超限",
};
const rejectionNames = {
  budget_exhausted: "预算不足",
  rate_limit: "调用频率超限",
  concurrency_limit: "并发超限",
  invalid_api_key: "Key 无效或已过期",
  identity_disabled: "身份已停用",
  model_not_allowed: "模型无权限",
  invalid_request_parameters: "请求参数不合法",
  request_too_large: "请求体过大",
  input_too_large: "输入过大",
  price_not_configured: "未配置计价",
  admission_busy: "网关准入繁忙",
  admission_queue_full: "准入队列已满",
  rate_service_unavailable: "限流服务不可用",
  team_upstream_unavailable: "上游凭证不可用",
  database_unavailable: "数据库不可用",
  query_parameters_not_supported: "不支持查询参数",
  ambiguous_output_limit: "输出上限参数冲突",
  internal_error: "网关内部错误",
  request_rejected: "请求被拒绝",
};
function pagination(kind, offset, total, change) {
  const size = pageSizes[kind];
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.floor(offset / size) + 1;
  const bar = node("div", undefined, "record-pagination");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "日志分页");
  const summary = node(
    "span",
    `共 ${total} 条 · 第 ${current} / ${pages} 页`,
    "pagination-summary",
  );
  const select = field("每页条数", "page_size", "text", String(size), [
    ["10", "10 条"],
    ["20", "20 条"],
    ["50", "50 条"],
  ]);
  select.querySelector("select").addEventListener("change", (e) => {
    pageSizes[kind] = Number(e.target.value);
    change(0).catch(showError);
  });
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
  bar.append(summary, select, buttons);
  return bar;
}
async function recordRows(path, params, kind, offset, setOffset) {
  const size = pageSizes[kind];
  params.set("limit", String(size));
  params.set("offset", String(offset));
  params.set("paginated", "true");
  let result = await api(path + "?" + params);
  const lastOffset = Math.max(0, (Math.ceil(result.total / size) - 1) * size);
  if (offset > lastOffset) {
    setOffset(lastOffset);
    params.set("offset", String(lastOffset));
    result = await api(path + "?" + params);
  }
  return result;
}
function reconcileForm(r, load) {
  openForm(
    "核对费用并结算",
    [
      node(
        "p",
        "核对只调整费用结算，不修改调用结果。完成后会自动处理关联的费用待核对告警。",
        "section-note",
      ),
      field("核实费用 / 元", "cost", "number"),
      field("核对依据与原因", "reason", "textarea"),
    ],
    async (d) => {
      await api("/requests/" + r.id + "/reconcile", "POST", {
        cost: yuan(d.cost),
        reason: d.reason,
      });
      closeModal();
      await load();
    },
  );
}
export async function overviewView(cache, load) {
  const [stats, metrics, rejected] = await Promise.all([
    api("/statistics"),
    api("/metrics"),
    api("/rejections?limit=1"),
  ]);
  const cards = node("div", undefined, "cards overview-cards");
  for (const [label, value] of [
    ["已准入调用", stats.reduce((a, r) => a + r.calls, 0)],
    ["被拒绝请求", rejected.total],
    ["已结算费用", money(stats.reduce((a, r) => a + r.cost, 0))],
    ["待核对请求", metrics.pending_requests],
    ["当前进行中", metrics.current_concurrency],
  ]) {
    const card = node("div", undefined, "card");
    card.append(
      node("div", label, "label"),
      node("div", String(value), "number"),
    );
    cards.append(card);
  }
  return [
    node(
      "p",
      "调用结果与费用结算分别统计；待核对不等于失败。拒绝记录从本次升级开始累计，历史调用结果未知。",
      "section-note",
    ),
    ...(metrics.rejection_record_failures
      ? [
          node(
            "p",
            `本次运行有 ${metrics.rejection_record_failures} 次拒绝记录写入失败，拒绝统计可能不完整。`,
            "section-note",
          ),
        ]
      : []),
    cards,
    toolbar("用量明细"),
    table(
      [
        "项目组",
        "人员",
        "Key",
        "模型",
        "准入 / 成功 / 失败",
        "执行中 / 未知",
        "断开 / 未发送",
        "待核对",
        "输入 / 输出",
        "缓存命中",
        "费用",
      ],
      stats.map((r) => [
        teamName(cache, r.team_id),
        studentName(cache, r.student_id),
        short(r.key_id),
        r.model,
        r.calls + " / " + r.success + " / " + r.failures,
        r.in_progress + " / " + r.unknown,
        r.client_disconnected + " / " + r.not_sent,
        r.pending,
        r.input_tokens + " / " + r.output_tokens,
        r.cache_hit_tokens,
        money(r.cost),
      ]),
    ),
  ];
}
export async function budgetsView(cache, load) {
  const rows = await api("/budgets");
  const cleanupTeams = [...new Set(rows.map((p) => p.team_id).filter(Boolean))]
    .map((id) => cache.teamHistory.find((t) => t.id === id))
    .filter(Boolean);
  return [
    node(
      "p",
      "这里同时展示项目组与个人的周期预算，同一笔调用的两个归属请勿相加。剩余额度 = 预算上限 − 已结算。每次请求前检查已用费用，不预留预算；进行中请求结算后可能超出上限，随后停止新请求。",
      "section-note",
    ),
    toolbar(
      "周期预算与用量",
      ...(cache.config.test_cleanup_enabled && cleanupTeams.length
        ? [
            button(
              "清理测试记录",
              () => clearTestUsageForm(cleanupTeams, load),
              "danger",
            ),
          ]
        : []),
    ),
    ...(cache.config.test_cleanup_enabled
      ? [
          node(
            "p",
            "模拟环境支持按项目组删除测试调用、用量和账本，同时清零该组模拟用量。项目组、学生、Key 和预算配置保留。",
            "section-note",
          ),
        ]
      : []),
    table(
      [
        "归属",
        "对象",
        "开始 / 北京时间",
        "结束",
        "预算上限",
        "已结算",
        "剩余额度",
        ...(cache.config.test_cleanup_enabled ? ["操作"] : []),
      ],
      rows.map((p) => [
        p.owner_type === "team" ? "项目组" : "个人",
        p.owner_type === "team"
          ? teamName(cache, p.owner_id)
          : cache.memberHistory.find((m) => m.id === p.owner_id)?.name ||
            short(p.owner_id),
        date(p.starts_at),
        date(p.ends_at),
        money(p.limit),
        money(p.spent),
        money(p.available),
        ...(cache.config.test_cleanup_enabled
          ? [
              button(
                "删除该组测试记录",
                () => clearTestUsageForm(cleanupTeams, load, p.team_id),
                "danger",
              ),
            ]
          : []),
      ]),
    ),
  ];
}
export async function requestsView(cache, load) {
  const form = node("form", undefined, "filters");
  form.append(
    field(
      "起始时间 / 北京时间",
      "start",
      "datetime-local",
      filters.start || "",
    ),
    field("结束时间 / 不含", "end", "datetime-local", filters.end || ""),
    field("项目组", "team_id", "text", filters.team_id || "", [
      ["", "全部"],
      ...cache.teamHistory.map((t) => [t.id, teamName(cache, t.id)]),
    ]),
    field("人员", "student_id", "text", filters.student_id || "", [
      ["", "全部"],
      ...cache.memberHistory.map((m) => [
        m.student_id,
        m.name + " / " + m.team + (m.deleted_at ? "（已删除）" : ""),
      ]),
    ]),
    field("Key ID（可留空）", "key_id", "text", filters.key_id || ""),
    field("模型（可留空）", "model", "text", filters.model || ""),
    field("请求 ID", "request_id", "text", filters.request_id || ""),
    field("结算状态", "status", "text", filters.status || "", [
      ["", "全部"],
      ...Object.entries(settlements),
    ]),
    field("调用结果", "outcome", "text", filters.outcome || "", [
      ["", "全部"],
      ...Object.entries(outcomes),
    ]),
  );
  form.querySelectorAll("input,select").forEach((i) => (i.required = false));
  const submit = node("button", "筛选");
  form.append(submit);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    filters = Object.fromEntries(new FormData(form));
    requestOffset = 0;
    load().catch((e) => showError(e));
  });
  const params = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v),
  );
  const { rows, total } = await recordRows(
    "/requests",
    params,
    "requests",
    requestOffset,
    (n) => (requestOffset = n),
  );
  const a = node("a", "导出当前筛选 CSV（最多 10000 行，从首条开始）");
  const exportParams = new URLSearchParams(params);
  exportParams.set("offset", "0");
  exportParams.set("limit", "10000");
  a.href = "/admin/api/export.csv?" + exportParams;
  const controls = () =>
    pagination("requests", requestOffset, total, async (n) => {
      requestOffset = n;
      await load();
      document.querySelector("#record-tabs").scrollIntoView({ block: "start" });
    });
  return [
    form,
    toolbar("调用明细"),
    node(
      "p",
      "此处显示已准入调用；预算、权限和限流等拦截请查看“拒绝记录”。费用待核对与调用结果相互独立。",
      "section-note",
    ),
    controls(),
    a,
    table(
      [
        "时间",
        "人员 / 组",
        "模型",
        "调用结果",
        "结算状态",
        "费用",
        "输入 / 输出",
        "操作",
      ],
      rows.map((r) => [
        date(r.started_at),
        r.name + " / " + teamName(cache, r.team_id),
        r.model,
        outcomes[r.outcome] || r.outcome,
        badge(settlements[r.status] || r.status),
        r.cost === null
          ? "待核对"
          : money(r.cost) + (r.estimated ? " 估算" : ""),
        (r.usage?.prompt_tokens ?? "—") +
          " / " +
          (r.usage?.completion_tokens ?? "—"),
        actions(
          button("详情", () => {
            openForm(
              "请求详情",
              [node("pre", JSON.stringify(r, null, 2), "detail")],
              null,
            );
          }),
          ...(r.status === "pending"
            ? [button("人工核对", () => reconcileForm(r, load))]
            : []),
        ),
      ]),
    ),
    controls(),
  ];
}
export async function rejectionsView(cache, load) {
  const form = node("form", undefined, "filters");
  form.append(
    field(
      "起始时间 / 北京时间",
      "start",
      "datetime-local",
      rejectionFilters.start || "",
    ),
    field(
      "结束时间 / 不含",
      "end",
      "datetime-local",
      rejectionFilters.end || "",
    ),
    field("项目组", "team_id", "text", rejectionFilters.team_id || "", [
      ["", "全部"],
      ...cache.teamHistory.map((t) => [t.id, teamName(cache, t.id)]),
    ]),
    field("人员", "student_id", "text", rejectionFilters.student_id || "", [
      ["", "全部"],
      ...[
        ...new Map(cache.memberHistory.map((m) => [m.student_id, m])).values(),
      ].map((m) => [m.student_id, m.name]),
    ]),
    field("Key ID", "key_id", "text", rejectionFilters.key_id || ""),
    field("模型", "model", "text", rejectionFilters.model || ""),
    field("拒绝原因", "reason", "text", rejectionFilters.reason || "", [
      ["", "全部"],
      ...Object.entries(rejectionNames),
    ]),
    field("请求 ID", "request_id", "text", rejectionFilters.request_id || ""),
  );
  form.querySelectorAll("input,select").forEach((i) => (i.required = false));
  form.append(node("button", "筛选"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    rejectionFilters = Object.fromEntries(new FormData(form));
    rejectionOffset = 0;
    load().catch(showError);
  });
  const params = new URLSearchParams(
    Object.entries(rejectionFilters).filter(([, v]) => v),
  );
  const { rows, total } = await recordRows(
    "/rejections",
    params,
    "rejections",
    rejectionOffset,
    (n) => (rejectionOffset = n),
  );
  const controls = () =>
    pagination("rejections", rejectionOffset, total, async (n) => {
      rejectionOffset = n;
      await load();
      document.querySelector("#record-tabs").scrollIntoView({ block: "start" });
    });
  return [
    node(
      "p",
      "每行是一次未获准调用的请求，不产生费用。无效 Key 可能无法归属人员；不会保存完整 Key、提示词或回答。",
      "section-note",
    ),
    form,
    controls(),
    table(
      [
        "时间",
        "人员 / 项目组",
        "模型",
        "原因",
        "限制归属",
        "HTTP",
        "请求 ID",
        "详情",
      ],
      rows.map((r) => [
        date(r.created_at),
        studentName(cache, r.student_id) + " / " + teamName(cache, r.team_id),
        r.model,
        rejectionNames[r.reason] || r.reason,
        { team: "项目组", membership: "个人", both: "项目组及个人" }[
          r.limit_scope
        ] || "—",
        r.http_status,
        r.id,
        button("详情", () =>
          openForm(
            "拒绝详情",
            [node("pre", JSON.stringify(r, null, 2), "detail")],
            null,
          ),
        ),
      ]),
    ),
    controls(),
  ];
}
export async function eventsView(cache, load) {
  const params = new URLSearchParams();
  if (eventResolved !== "") params.set("resolved", eventResolved);
  const { rows, total } = await recordRows(
    "/events",
    params,
    "events",
    eventOffset,
    (n) => (eventOffset = n),
  );
  const statusFilter = field("处理状态", "resolved", "text", eventResolved, [
    ["false", "待处理"],
    ["true", "已处理"],
    ["", "全部"],
  ]);
  statusFilter.querySelector("select").addEventListener("change", (e) => {
    eventResolved = e.target.value;
    eventOffset = 0;
    load().catch(showError);
  });
  const openRequests = (r) => {
    filters = r.detail.request_id
      ? { request_id: r.detail.request_id }
      : { status: "pending" };
    requestOffset = 0;
    return cache.navigate("requests");
  };
  const controls = () =>
    pagination("events", eventOffset, total, async (n) => {
      eventOffset = n;
      await load();
      document.querySelector("#record-tabs").scrollIntoView({ block: "start" });
    });
  return [
    node(
      "p",
      "新来源仅提示网络变化。标记处理不会释放预算或结算费用；费用待核对须先进入请求核对，结算后关联待办自动处理。重复拒绝按小时汇总次数。",
      "section-note",
    ),
    statusFilter,
    controls(),
    table(
      [
        "首次 / 最近发生",
        "级别 / 事件",
        "次数",
        "项目组",
        "详情",
        "处理记录",
        "操作",
      ],
      rows.map((r) => [
        date(r.created_at) + " / " + date(r.last_seen_at),
        { info: "提示", warning: "预警", action: "待办" }[r.severity] +
          " / " +
          (eventNames[r.kind] || r.kind),
        r.occurrences,
        teamName(cache, r.team_id),
        JSON.stringify(r.detail),
        r.resolved
          ? `已处理 · ${r.resolved_by || "历史记录"} · ${date(r.resolved_at)} · ${r.resolution || "—"}`
          : "待处理",
        actions(
          ...(r.detail.request_id || r.kind === "recovery_pending"
            ? [button("查看请求 / 核对", () => openRequests(r))]
            : []),
          ...(r.detail.rejection_id
            ? [
                button("查看拒绝记录", () => {
                  rejectionFilters = { team_id: r.team_id, reason: r.kind };
                  rejectionOffset = 0;
                  return cache.navigate("rejections");
                }),
              ]
            : []),
          ...(!r.resolved
            ? [
                button("标记处理", () =>
                  openForm(
                    "记录处理结果",
                    [field("处理说明", "reason", "textarea")],
                    async (d) => {
                      await api("/events/" + r.id + "/resolve", "POST", {
                        reason: d.reason,
                      });
                      closeModal();
                      await load();
                    },
                  ),
                ),
              ]
            : []),
        ),
      ]),
    ),
    controls(),
  ];
}
export async function auditView(cache, load) {
  const { rows, total } = await recordRows(
    "/audit",
    new URLSearchParams(),
    "audit",
    auditOffset,
    (n) => (auditOffset = n),
  );
  const controls = () =>
    pagination("audit", auditOffset, total, async (n) => {
      auditOffset = n;
      await load();
      document.querySelector("#record-tabs").scrollIntoView({ block: "start" });
    });
  return [
    node(
      "p",
      "记录管理员及学生自助操作；配置变更保留非敏感字段的修改前后值。",
      "section-note",
    ),
    controls(),
    table(
      ["时间", "操作者", "操作", "对象", "非敏感差异", "请求 ID"],
      rows.map((r) => [
        date(r.created_at),
        r.actor,
        r.action,
        short(r.target),
        JSON.stringify(r.detail),
        short(r.request_id),
      ]),
    ),
    controls(),
  ];
}
