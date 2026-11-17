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
  toolbar,
  field,
  yuan,
  teamName,
  studentName,
  number,
  stacked,
  disclosure,
  progress,
  moreActions,
} from "./common.js";
import { openForm, closeModal, clearTestUsageForm } from "./forms.js";
import {
  reportState,
  reportParams,
  setReportFilters,
  reportFilterForm,
  summaryBar,
  showRequestDetail,
  returnToAnalysis,
  settlements,
  outcomes,
} from "./consumption.js";
export { overviewView } from "./consumption.js";
let rejectionFilters = {};
let requestRevision = 0;
let requestOffset = 0,
  rejectionOffset = 0,
  eventOffset = 0,
  auditOffset = 0;
let eventResolved = "false";
const pageSizes = { requests: 10, rejections: 10, events: 10, audit: 10 };
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
  image_file_too_large: "上传图片过大",
  only_image_uploads_supported: "仅支持图片文件",
  invalid_multipart_upload: "上传格式不合法",
  image_upload_requires_user_data: "图片上传参数不合法",
  file_upstream_unavailable: "上游文件服务不可用",
  file_upstream_rejected: "上游拒绝文件操作",
  file_upstream_protocol_error: "上游文件响应异常",
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
  if (pages === 1) {
    summary.textContent = `共 ${total} 条`;
    bar.append(summary, select);
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
    "核对费用并入账",
    [
      node(
        "p",
        "核对只调整费用入账，不修改调用结果。完成后会自动处理关联的费用待核对告警。",
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
let budgetKind = "team",
  budgetHistory = false;
export async function budgetsView(cache, load) {
  const rows = await api(
    budgetHistory ? "/budgets" : "/budget-status?owner_type=" + budgetKind,
  );
  const cleanupTeams = cache.teams;
  const tabs = node("div", undefined, "segment-tabs");
  for (const [value, label] of [
    ["team", "项目预算"],
    ["membership", "个人额度"],
  ]) {
    const b = button(label, () => {
      budgetKind = value;
      return load();
    });
    b.setAttribute("aria-pressed", String(budgetKind === value));
    tabs.append(b);
  }
  tabs.append(
    button(budgetHistory ? "仅看当前周期" : "查看历史周期", () => {
      budgetHistory = !budgetHistory;
      return load();
    }),
  );
  const now = new Date();
  const visible = rows.filter(
    (p) =>
      p.owner_type === budgetKind &&
      (budgetHistory ||
        (new Date(p.starts_at) <= now && new Date(p.ends_at) > now)),
  );
  const periodKey = (p) => p.starts_at + "/" + p.ends_at;
  const counts = new Map();
  visible.forEach((p) =>
    counts.set(periodKey(p), (counts.get(periodKey(p)) || 0) + 1),
  );
  const commonPeriod = [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
  const typical = visible.find((p) => periodKey(p) === commonPeriod);
  return [
    node(
      "p",
      "项目预算与个人额度是同一笔消费的两个约束，不可相加。剩余额度 = 上限 − 已入账费用。",
      "section-note",
    ),
    tabs,
    toolbar(
      budgetKind === "team" ? "项目预算" : "个人额度",
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
    ...(typical
      ? [
          node(
            "p",
            "预算周期：" +
              date(typical.starts_at) +
              " — " +
              date(typical.ends_at) +
              "（不含）· 北京时间；其他周期在行内标注。",
            "section-note budget-period",
          ),
        ]
      : []),
    table(
      [
        "对象 / 周期",
        { label: "已用 / 上限", numeric: true },
        "使用率",
        { label: "剩余额度", numeric: true },
        "状态",
      ],
      visible.map((p) => [
        stacked(
          p.name ||
            (p.owner_type === "team"
              ? teamName(cache, p.owner_id)
              : (cache.memberHistory.find((m) => m.id === p.owner_id)?.name ||
                  short(p.owner_id)) +
                " / " +
                teamName(cache, p.team_id)),
          periodKey(p) === commonPeriod
            ? ""
            : "特殊周期：" +
                date(p.starts_at) +
                " — " +
                date(p.ends_at) +
                "（不含）",
        ),
        stacked(money(p.spent), "上限 " + money(p.limit)),
        progress(p.spent, p.limit),
        money(p.available),
        badge(
          p.available < 0
            ? "已超额"
            : p.available === 0
              ? "已达上限"
              : p.spent >= p.limit * 0.8
                ? "接近上限"
                : "额度充足",
        ),
      ]),
      "compact-table budget-table",
    ),
    disclosure(
      "预算规则",
      node(
        "p",
        "每次请求前检查已入账费用，不预留预算。进行中请求可能在入账后造成超额，随后停止新请求；待核对金额未知。当前周期包含尚未使用的额度；历史视图仅显示已有账本周期（最多 2000 条）。",
        "section-note",
      ),
    ),
  ];
}
export async function requestsView(cache, load) {
  if (requestRevision !== (reportState.revision || 0)) {
    requestOffset = 0;
    requestRevision = reportState.revision || 0;
  }
  const form = reportFilterForm(cache, load, () => {
    requestOffset = 0;
  });
  const params = reportParams({ include_summary: true });
  const { rows, total, summary } = await recordRows(
    "/requests",
    params,
    "requests",
    requestOffset,
    (n) => {
      requestOffset = n;
    },
  );
  const a = node("a", "导出当前筛选 CSV（最多 10000 行，从首条开始）");
  const exportParams = reportParams({ offset: 0, limit: 10000 });
  a.href = "/admin/api/export.csv?" + exportParams;
  const controls = () =>
    pagination("requests", requestOffset, total, async (n) => {
      requestOffset = n;
      await load();
    });
  return [
    form,
    summaryBar(summary),
    toolbar(
      "单次调用",
      button("返回消费汇总", () => returnToAnalysis(cache)),
    ),
    controls(),
    a,
    table(
      [
        "成员 / 项目",
        { label: "入账费用", numeric: true },
        "模型 / 时间",
        "调用结果",
        "入账状态",
        "操作",
      ],
      rows.map((r) => [
        stacked(r.name, teamName(cache, r.team_id)),
        r.cost === null
          ? stacked("金额未知", r.status === "pending" ? "待核对" : "尚未入账")
          : money(r.cost, true),
        stacked(r.model, date(r.started_at)),
        badge(outcomes[r.outcome] || "结果未知"),
        badge(settlements[r.status] || r.status),
        button("详情", () =>
          showRequestDetail(r.id, cache, (item) => reconcileForm(item, load)),
        ),
      ]),
      "compact-table request-table",
    ),
    controls(),
    node(
      "p",
      "已入账不代表已核对上游账单。预算、权限和限流等拦截见“拒绝记录”；费用待核对与调用结果相互独立。",
      "section-note",
    ),
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
        "成员 / 项目",
        "拒绝原因",
        { label: "模型", secondary: true },
        { label: "HTTP", numeric: true },
        { label: "时间", secondary: true },
        "操作",
      ],
      rows.map((r) => [
        stacked(studentName(cache, r.student_id), teamName(cache, r.team_id)),
        stacked(
          rejectionNames[r.reason] || r.reason,
          {
            team: "项目组限制",
            membership: "个人限制",
            both: "项目组及个人限制",
          }[r.limit_scope],
        ),
        r.model,
        r.http_status,
        date(r.created_at),
        button("详情", () =>
          openForm(
            "拒绝详情",
            [
              stacked("拒绝原因", rejectionNames[r.reason] || r.reason),
              stacked("时间", date(r.created_at)),
              stacked("请求 ID", r.id),
              ...(r.details?.limit_bytes ? [
                stacked("已接收输入 / 上限", (r.details.received_bytes / 1048576).toFixed(2) + " MiB / " + (r.details.limit_bytes / 1048576).toFixed(2) + " MiB"),
                node("p", "这是输入请求大小；超过上限时即停止读取，已接收大小可能小于完整请求。图片较多时可先上传，再用 file_id 引用。", "section-note"),
              ] : []),
              ...(r.details?.team ? [
                stacked("项目组：当前 / 上限", r.details.team.current + " / " + (r.details.team.limit || "不限")),
                stacked("个人：当前 / 上限", r.details.membership.current + " / " + (r.details.membership.limit || "不限")),
                ...(r.details.waited_ms !== undefined ? [stacked("已排队", (r.details.waited_ms / 1000).toFixed(1) + " 秒")] : []),
              ] : []),
              disclosure(
                "技术字段",
                node("pre", JSON.stringify(r, null, 2), "detail"),
              ),
            ],
            null,
          ),
        ),
      ]),
      "compact-table rejection-table",
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
    setReportFilters(
      r.detail.request_id
        ? { request_id: r.detail.request_id }
        : { status: "pending" },
    );
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
        "事件",
        "项目组",
        { label: "最近发生", secondary: true },
        "处理状态",
        "操作",
      ],
      rows.map((r) => [
        stacked(
          eventNames[r.kind] || r.kind,
          ({ info: "提示", warning: "预警", action: "待办" }[r.severity] ||
            "提示") +
            " · " +
            number(r.occurrences) +
            " 次",
        ),
        teamName(cache, r.team_id),
        date(r.last_seen_at),
        stacked(
          r.resolved ? "已处理" : "待处理",
          r.resolved ? r.resolution : "",
        ),
        moreActions(
          button("详情", () =>
            openForm(
              "事件详情",
              [
                stacked(
                  eventNames[r.kind] || r.kind,
                  teamName(cache, r.team_id),
                ),
                stacked(
                  "首次 / 最近发生",
                  date(r.created_at) + " / " + date(r.last_seen_at),
                ),
                stacked(
                  "处理记录",
                  r.resolved
                    ? `${r.resolved_by || "历史记录"} · ${date(r.resolved_at)} · ${r.resolution || "—"}`
                    : "待处理",
                ),
                node("pre", JSON.stringify(r.detail, null, 2), "detail"),
              ],
              null,
            ),
          ),
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
      "compact-table event-table",
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
      [
        "时间",
        "操作者",
        "操作",
        { label: "对象", secondary: true },
        "非敏感差异",
        { label: "请求 ID", secondary: true },
      ],
      rows.map((r) => [
        date(r.created_at),
        r.actor,
        r.action,
        short(r.target),
        disclosure(
          "查看详情",
          node("pre", JSON.stringify(r.detail, null, 2), "detail"),
        ),
        short(r.request_id),
      ]),
      "compact-table audit-table",
    ),
    controls(),
  ];
}
