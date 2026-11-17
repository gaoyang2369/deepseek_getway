import {
  $,
  node,
  number,
  money,
  date,
  short,
  api,
  button,
  badge,
  table,
  toolbar,
  field,
  yuan,
  stacked,
  disclosure,
  progress,
  teamName,
  studentName,
  listPagination,
  showError,
} from "./common.js";
import { openForm } from "./forms.js";
import { searchableSelect } from "./select.js";

function localDay() {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" }).format(
    new Date(),
  );
}
function dayAfter(day, offset = 1) {
  const value = new Date(day + "T00:00:00+08:00");
  value.setUTCDate(value.getUTCDate() + offset);
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" }).format(
    value,
  );
}
export function bounds(period) {
  const today = localDay();
  return {
    start:
      (period === "month"
        ? today.slice(0, 8) + "01"
        : period === "week"
          ? dayAfter(today, -6)
          : today) + "T00:00",
    end: dayAfter(today) + "T00:00",
  };
}
export const reportState = {
  filters: bounds("month"),
  period: "month",
  group: "team",
  page: { offset: 0, size: 20 },
};
const groupNames = {
  team: "项目组",
  member: "成员",
  model: "模型",
  key: "Key",
};
let analysisTrail = [],
  requestOrigin = null;
function snapshot(cache, group = reportState.group) {
  return {
    filters: { ...reportState.filters },
    period: reportState.period,
    group,
    page: { ...reportState.page },
    scroll: window.scrollY,
    view: cache.view,
  };
}
async function restore(cache, saved) {
  Object.assign(reportState, {
    filters: { ...saved.filters },
    period: saved.period,
    group: saved.group,
    page: { ...saved.page },
    revision: (reportState.revision || 0) + 1,
  });
  await cache.navigate(saved.view);
  requestAnimationFrame(() =>
    window.scrollTo({ top: saved.scroll, behavior: "instant" }),
  );
}
export function clearRequestOrigin() {
  requestOrigin = null;
}
export async function openRequestList(cache, changes = {}) {
  requestOrigin = { saved: snapshot(cache), trail: [...analysisTrail] };
  reportState.filters = { ...reportState.filters, ...changes };
  reportState.revision = (reportState.revision || 0) + 1;
  await cache.navigate("requests");
}
export async function returnToAnalysis(cache) {
  if (!requestOrigin) return cache.navigate("consumption");
  const origin = requestOrigin;
  requestOrigin = null;
  analysisTrail = origin.trail;
  await restore(cache, origin.saved);
}
function drillPath(cache) {
  const nav = node("nav", undefined, "drill-path");
  nav.setAttribute("aria-label", "消费下钻路径");
  async function back(index) {
    const saved = analysisTrail[index];
    analysisTrail = analysisTrail.slice(0, index);
    await restore(cache, saved);
  }
  if (analysisTrail.length)
    nav.append(button("← 返回上一级", () => back(analysisTrail.length - 1)));
  analysisTrail.forEach((saved, index) =>
    nav.append(
      button(groupNames[saved.group], () => back(index)),
      node("span", "→", "muted"),
    ),
  );
  const current = node("strong", groupNames[reportState.group]);
  current.setAttribute("aria-current", "page");
  nav.append(current);
  return nav;
}
export const outcomes = {
  in_progress: "执行中",
  success: "成功",
  upstream_failure: "上游失败",
  client_disconnected: "客户端断开",
  not_sent: "未发送",
  unknown: "结果未知",
};
export const settlements = {
  reserved: "进行中",
  running: "进行中",
  pending: "待核对",
  settled: "已入账",
};
export const reportParams = (extra = {}) =>
  new URLSearchParams(
    Object.entries({ ...reportState.filters, ...extra }).filter(
      ([, value]) => value !== "" && value != null,
    ),
  );
export function setReportFilters(filters, period = "custom") {
  analysisTrail = [];
  reportState.filters = filters;
  reportState.revision = (reportState.revision || 0) + 1;
  reportState.period = period;
  reportState.page.offset = 0;
}
export function scopeText() {
  const f = reportState.filters;
  return `${f.start ? f.start.replace("T", " ") : "全部历史"} — ${f.end ? f.end.replace("T", " ") + "（不含）" : "至今"} · 北京时间`;
}
export function reportFilterForm(cache, load, onChange = () => {}) {
  const f = reportState.filters,
    form = node("form", undefined, "report-filters");
  const common = node("div", undefined, "filter-grid");
  const period = field("统计周期", "period", "text", reportState.period, [
    ["month", "本月"],
    ["today", "今日"],
    ["week", "近 7 天"],
    ["custom", "自定义"],
  ]);
  const dates = node("div", undefined, "custom-dates");
  dates.append(
    field("起始时间 · 北京时间", "start", "datetime-local", f.start || ""),
    field("结束时间 · 不含", "end", "datetime-local", f.end || ""),
  );
  dates.hidden = reportState.period !== "custom";
  period.querySelector("select").onchange = (e) => {
    dates.hidden = e.target.value !== "custom";
  };
  const team = field("项目组", "team_id", "text", f.team_id || "", [
    ["", "全部项目组"],
    ...cache.teamHistory.map((t) => [t.id, teamName(cache, t.id)]),
  ]);
  function memberOptions(teamId) {
    const members = [
      ...new Map(
        cache.memberHistory
          .filter((m) => !teamId || m.team_id === teamId)
          .map((m) => [m.student_id, m]),
      ).values(),
    ];
    return [
      ["", "全部成员"],
      ...members.map((m) => [m.student_id, `${m.name} · ${m.student_number}`]),
    ];
  }
  const member = searchableSelect(
    "成员",
    "student_id",
    f.student_id || "",
    memberOptions(f.team_id),
  );
  const modelNames = [
    ...new Set([
      ...(cache.reportOptions?.models || []),
      ...(f.model ? [f.model] : []),
    ]),
  ].sort();
  const model = searchableSelect("模型", "model", f.model || "", [
    ["", "全部模型"],
    ...modelNames.map((m) => [m, m]),
  ]);
  const selectionNote = node("p", "", "filter-selection-note");
  selectionNote.setAttribute("role", "status");
  selectionNote.hidden = true;
  team.querySelector("select").onchange = (e) => {
    const cleared = member.setOptions(memberOptions(e.target.value));
    selectionNote.textContent = cleared
      ? "原选成员不属于当前项目，已改为全部成员。应用筛选后更新结果。"
      : "";
    selectionNote.hidden = !cleared;
  };
  common.append(period, team, member.element, model.element);
  const advanced = disclosure(
    "高级筛选",
    field("Key ID", "key_id", "text", f.key_id || ""),
    field("请求 ID", "request_id", "text", f.request_id || ""),
    field("入账状态", "status", "text", f.status || "", [
      ["", "全部"],
      ["in_progress", "进行中"],
      ["pending", "待核对"],
      ["settled", "已入账"],
    ]),
    field("调用结果", "outcome", "text", f.outcome || "", [
      ["", "全部"],
      ...Object.entries(outcomes),
    ]),
    field("数据标签 · 按项目", "data_scope", "text", f.data_scope || "", [
      ["", "全部数据"],
      ["regular", "排除测试项目"],
      ["test", "仅测试项目"],
    ]),
  );
  advanced.classList.add("advanced-filters");
  advanced.open = [
    "key_id",
    "request_id",
    "status",
    "outcome",
    "data_scope",
  ].some((k) => f[k]);
  const footer = node("div", undefined, "filter-footer");
  const submit = node("button", "应用筛选");
  submit.type = "submit";
  footer.append(
    node("span", scopeText(), "muted"),
    button("重置", async () => {
      setReportFilters(bounds("month"), "month");
      onChange();
      await load();
    }),
    submit,
  );
  form.append(common, dates, selectionNote, advanced, footer);
  form.querySelectorAll("input,select").forEach((input) => {
    input.required = false;
  });
  form.onsubmit = (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form)),
      selectedPeriod = data.period;
    delete data.period;
    if (selectedPeriod !== "custom")
      Object.assign(data, bounds(selectedPeriod));
    if (data.start && data.end && data.start >= data.end) {
      showError(new Error("结束时间必须晚于开始时间。"));
      return;
    }
    setReportFilters(data, selectedPeriod);
    onChange();
    load().catch(showError);
  };
  return form;
}
function metric(label, value, caption, emphasis = false) {
  const card = node(
    "div",
    undefined,
    "card" + (emphasis ? " metric-primary" : ""),
  );
  card.append(
    node("div", label, "label"),
    node("div", value, "number"),
    node("small", caption),
  );
  return card;
}
export function accountingNote() {
  return disclosure(
    "费用口径",
    node(
      "p",
      "已入账指网关账本已记录费用，不代表已与上游账单核对。自动计费依据请求当时费率估算；人工核对的依据保留在单次详情。待核对金额未知，不计入已入账合计。项目组与个人是同一笔消费的不同视角，不能相加。历史未知结果不视为失败。",
      "section-note",
    ),
  );
}
export function summaryBar(summary) {
  const d = node("div", undefined, "summary-bar");
  d.append(
    stacked("当前筛选 · 已入账", money(summary.cost)),
    stacked("已准入调用", number(summary.calls) + " 次"),
    stacked(
      "待核对",
      number(summary.pending) + " 次" + (summary.pending ? " · 金额未知" : ""),
    ),
    stacked("进行中", number(summary.running) + " 次"),
  );
  d.dataset.cost = summary.cost;
  return d;
}
function objectName(cache, row, group) {
  if (group === "team") return teamName(cache, row.id);
  if (group === "member") {
    const member = cache.memberHistory.find((m) => m.student_id === row.id);
    return stacked(studentName(cache, row.id), member?.student_number);
  }
  return group === "key" ? stacked("Key · " + short(row.id), row.id) : row.id;
}
export async function openAnalysis(cache, changes = {}, group = "member") {
  requestOrigin = null;
  setReportFilters({ ...reportState.filters, ...changes }, reportState.period);
  reportState.group = group;
  await cache.navigate("consumption");
}
function groupTable(cache, data, group, overview = false) {
  return table(
    [
      "统计对象",
      { label: "已入账费用", numeric: true },
      { label: "占比", numeric: true, secondary: true },
      { label: "调用次数", numeric: true, secondary: true },
      { label: "待核对", numeric: true },
      "操作",
    ],
    data.rows.map((r) => [
      objectName(cache, r, group),
      stacked(money(r.cost), r.pending ? "另有金额待核对" : ""),
      data.summary.cost
        ? ((r.cost / data.summary.cost) * 100).toFixed(1) + "%"
        : "—",
      number(r.calls),
      number(r.pending),
      button(overview ? "查看消费" : "查看构成", async () => {
        const key = {
          team: "team_id",
          member: "student_id",
          model: "model",
          key: "key_id",
        }[group];
        if (group === "key") return openRequestList(cache, { [key]: r.id });
        analysisTrail.push(snapshot(cache, group));
        reportState.filters = { ...reportState.filters, [key]: r.id };
        reportState.revision = (reportState.revision || 0) + 1;
        reportState.group = { team: "member", member: "model", model: "key" }[
          group
        ];
        reportState.page.offset = 0;
        await cache.navigate("consumption");
      }),
    ]),
    "compact-table consumption-table",
  );
}
function trendPanel(data) {
  const panel = node("section", undefined, "panel trend-panel");
  panel.append(
    toolbar("每日费用趋势"),
    node("p", "按调用开始日期归属 · 已入账费用（元）", "section-note"),
  );
  let points = data.trend;
  const first = reportState.filters.start?.slice(0, 10),
    end = reportState.filters.end;
  if (first && end) {
    const map = new Map(points.map((p) => [p.day, p]));
    const filled = [];
    for (
      let day = first, i = 0;
      day + "T00:00" < end && i < 367;
      day = dayAfter(day), i++
    )
      filled.push(map.get(day) || { day, cost: 0, pending: 0 });
    if (filled.length <= 366) points = filled;
  }
  if (!points.length) {
    panel.append(node("div", "所选时间暂无调用", "empty"));
    return panel;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 720 190");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    `每日已入账费用趋势，共 ${points.length} 天；具体数据可展开下方每日费用。`,
  );
  const max = Math.max(...points.map((p) => p.cost), 1),
    width = 660 / Math.max(points.length, 1);
  const add = (tag, attributes, content) => {
    const element = document.createElementNS(svg.namespaceURI, tag);
    Object.entries(attributes).forEach(([key, value]) =>
      element.setAttribute(key, value),
    );
    if (content) element.textContent = content;
    svg.append(element);
    return element;
  };
  add("line", { x1: 40, x2: 705, y1: 155, y2: 155, stroke: "#dbe5dc" });
  points.forEach((p, i) => {
    const h = (p.cost / max) * 120;
    const bar = add("rect", {
      x: 40 + i * width + width * 0.15,
      y: 155 - h,
      width: Math.max(1, width * 0.7),
      height: h,
      rx: 2,
      fill: "#39765a",
    });
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent =
      p.day +
      " · " +
      money(p.cost, true) +
      (p.pending ? ` · ${p.pending} 次待核对` : "");
    bar.append(title);
  });
  add(
    "text",
    { x: 40, y: 18, fill: "#62756b", "font-size": 12 },
    money(Math.max(...points.map((p) => p.cost))),
  );
  add(
    "text",
    { x: 40, y: 179, fill: "#62756b", "font-size": 12 },
    points[0].day,
  );
  add(
    "text",
    { x: 705, y: 179, fill: "#62756b", "font-size": 12, "text-anchor": "end" },
    points.at(-1).day,
  );
  panel.append(
    svg,
    disclosure(
      "每日费用与待核对次数",
      table(
        [
          "日期",
          { label: "已入账费用", numeric: true },
          { label: "待核对", numeric: true },
        ],
        points.map((p) => [p.day, money(p.cost, true), number(p.pending)]),
        "compact-table",
      ),
    ),
  );
  return panel;
}
export async function overviewView(cache, load) {
  const [data, budgets, metrics, events] = await Promise.all([
    api("/consumption?" + reportParams({ group_by: "team", limit: 5 })),
    api("/budget-status"),
    api("/metrics"),
    api("/events?resolved=false&paginated=true&limit=1"),
  ]);
  const s = data.summary;
  // Day and month budgets never share a denominator. This card explicitly describes current monthly projects.
  const monthly = budgets.filter(
    (b) =>
      b.period === "month" &&
      b.active &&
      (!reportState.filters.team_id ||
        b.team_id === reportState.filters.team_id) &&
      (reportState.filters.data_scope !== "test" || b.is_test) &&
      (reportState.filters.data_scope !== "regular" || !b.is_test),
  );
  const budget = monthly.reduce((n, b) => n + b.limit, 0),
    spent = monthly.reduce((n, b) => n + b.spent, 0);
  const cards = node("div", undefined, "cards management-cards");
  cards.append(
    metric(
      "筛选范围 · 已入账费用",
      money(s.cost),
      "含按费率估算 · 非上游账单",
      true,
    ),
    metric(
      "当前月项目预算使用",
      budget
        ? (spent > 0 && spent / budget < 0.001
            ? "<0.1"
            : ((spent / budget) * 100).toFixed(1)) + "%"
        : "未配置",
      "按项目与数据标签 · 当前月上限 " + money(budget),
    ),
    metric(
      "活跃成员",
      number(s.active_members) + " 人",
      "筛选范围内有准入调用的成员",
    ),
    metric(
      "待核对调用",
      number(s.pending) + " 次",
      s.pending ? "金额未知 · 未计入已入账合计" : "筛选范围内暂无待核对",
    ),
  );
  const status = node("div", undefined, "status-strip");
  status.append(
    node(
      "span",
      `已准入调用 ${number(s.calls)} 次 · 当前进行中 ${number(metrics.current_concurrency)} 次（全局实时）`,
    ),
    button("查看消费分析 →", () => cache.navigate("consumption")),
  );
  const attention = node("section", undefined, "attention-panel");
  attention.append(
    stacked(
      "需要关注",
      `${number(s.pending)} 次费用待核对 · ${number(events.total)} 条全局未处理事件`,
    ),
    button("查看待核对", () => {
      setReportFilters(
        { ...reportState.filters, status: "pending" },
        reportState.period,
      );
      return cache.navigate("requests");
    }),
    button("处理告警", () => cache.navigate("events")),
  );
  const warnings = budgets.filter((b) =>
    b.limit ? b.spent >= b.limit * 0.8 : b.spent > 0,
  );
  if (warnings.length)
    attention.append(
      node(
        "p",
        warnings
          .map(
            (b) =>
              `${b.name}：${b.spent >= b.limit ? "已达上限" : "使用超过 80%"}`,
          )
          .join("；"),
        "error-text",
      ),
    );
  return [
    reportFilterForm(cache, load),
    cards,
    status,
    ...(metrics.rejection_record_failures
      ? [
          node(
            "p",
            `本次运行有 ${number(metrics.rejection_record_failures)} 次拒绝记录写入失败，拒绝统计可能不完整。`,
            "key-warning",
          ),
        ]
      : []),
    trendPanel(data),
    toolbar(
      "项目消费排行",
      button("查看全部", () => {
        reportState.group = "team";
        return cache.navigate("consumption");
      }),
    ),
    groupTable(cache, data, "team", true),
    attention,
    accountingNote(),
  ];
}
export async function consumptionView(cache, load) {
  const data = await api(
    "/consumption?" +
      reportParams({
        group_by: reportState.group,
        offset: reportState.page.offset,
        limit: reportState.page.size,
        include_trend: false,
      }),
  );
  const last = Math.max(
    0,
    (Math.ceil(data.total / reportState.page.size) - 1) * reportState.page.size,
  );
  if (reportState.page.offset > last) {
    reportState.page.offset = last;
    return consumptionView(cache, load);
  }
  const tabs = node("div", undefined, "segment-tabs");
  for (const [value, label] of [
    ["team", "项目组"],
    ["member", "成员"],
    ["model", "模型"],
    ["key", "Key"],
  ]) {
    const b = button("按" + label, () => {
      analysisTrail = [];
      reportState.group = value;
      reportState.page.offset = 0;
      return load();
    });
    b.setAttribute("aria-pressed", String(value === reportState.group));
    tabs.append(b);
  }
  return [
    drillPath(cache),
    reportFilterForm(cache, load),
    summaryBar(data.summary),
    tabs,
    toolbar(
      "消费汇总",
      button("查看单次调用 →", () => openRequestList(cache)),
    ),
    node(
      "p",
      "按费用从高到低排列；同一成员的多个 Key 合并统计。切换维度不改变筛选范围与合计。",
      "section-note",
    ),
    ...(data.total
      ? []
      : [
          node(
            "p",
            "当前筛选范围没有调用记录，可调整日期、项目或成员后重试。",
            "empty",
          ),
        ]),
    groupTable(cache, data, reportState.group),
    listPagination(reportState.page, data.total, "消费汇总", load),
    accountingNote(),
  ];
}
function detailAmount(value) {
  return (
    "¥" +
    Number(value).toLocaleString("zh-CN", {
      minimumFractionDigits: 6,
      maximumFractionDigits: 12,
    })
  );
}
export async function showRequestDetail(id, cache, reconcile) {
  const r = await api("/requests/" + encodeURIComponent(id));
  const hero = node("div", undefined, "detail-hero");
  const identity = node("div"),
    tags = node("div", undefined, "detail-tags");
  tags.append(
    badge(outcomes[r.outcome] || "结果未知"),
    badge(settlements[r.status]),
    badge(
      r.upstream_verified
        ? "已与上游账单核对"
        : r.cost_basis === "manual_reconciliation"
          ? "人工核对入账"
          : "按费率估算",
    ),
  );
  identity.append(
    tags,
    node(
      "h3",
      r.name + " / " + r.team_name + (r.team_deleted ? "（已删除）" : ""),
    ),
    node("p", r.model + " · " + date(r.started_at), "muted"),
  );
  hero.append(
    identity,
    stacked(
      "本次入账费用",
      r.cost === null
        ? r.status === "pending"
          ? "金额待核对"
          : "尚未入账"
        : money(r.cost, true),
    ),
  );
  const parts = table(
    [
      "计费项",
      { label: "Token 数", numeric: true },
      { label: "元／百万 Token", numeric: true },
      { label: "金额（元）", numeric: true },
    ],
    r.components.map((p) => [
      {
        input_miss: "输入 · 未命中",
        input_hit: "输入 · 缓存命中",
        output: "输出",
      }[p.kind],
      number(p.tokens),
      money(p.rate, true).slice(1),
      detailAmount(p.amount_yuan).slice(1),
    ]),
    "compact-table fee-table",
  );
  const performance = node("div", undefined, "performance-grid");
  for (const [label, value] of [
    ["首 Token", r.first_delta_ms],
    ["总耗时", r.total_ms],
    ["准入耗时", r.admission_ms],
  ])
    performance.append(
      stacked(label, value === null ? "未记录" : number(value / 1000) + " 秒"),
    );
  performance.append(
    stacked(
      "输入缓存命中率",
      r.usage?.prompt_tokens
        ? (
            (r.usage.prompt_cache_hit_tokens / r.usage.prompt_tokens) *
            100
          ).toFixed(1) + "%"
        : "—",
    ),
  );
  const technical = disclosure(
    "技术字段与原始记录",
    stacked("请求 ID", r.id),
    stacked("Key ID", r.key_id),
    node("pre", JSON.stringify(r, null, 2), "detail"),
  );
  const fields = [
    hero,
    node("h3", "费用构成"),
    ...(r.components.length
      ? [
          parts,
          node(
            "p",
            `按费率计算合计 ${money(r.calculated_cost, true)}`,
            "section-note",
          ),
        ]
      : [
          node(
            "p",
            "没有可用的 Token 用量，无法拆分费用。缺失用量不按零用量计算。",
            "section-note",
          ),
        ]),
    performance,
    node("h3", "费用依据"),
    node(
      "p",
      r.upstream_verified
        ? `管理员已记录上游账单核对：${r.bill_verification.actor} · ${date(r.bill_verification.created_at)} · ${r.bill_verification.detail.reason}`
        : "上游账单尚未核验。价格是否标记为固定精确价，并不代表此笔调用已核对上游账单。",
      "section-note",
    ),
    disclosure(
      "计价依据",
      stacked("当时的计价版本", r.price.version),
      stacked("价格来源与说明", r.price.note),
      ...(r.components.length
        ? [
            node(
              "p",
              `逐项保留精度，整笔向上取整到 0.000001 元；取整差额 ${detailAmount(r.rounding_yuan)}。`,
              "section-note",
            ),
          ]
        : []),
    ),
    ...(r.reconciliation
      ? [
          node(
            "p",
            `人工核对：${r.reconciliation.actor} · ${date(r.reconciliation.created_at)} · ${r.reconciliation.detail.reason}`,
          ),
        ]
      : []),
    ...(r.cost !== null &&
    r.calculated_cost !== null &&
    r.cost !== r.calculated_cost
      ? [
          node(
            "p",
            "入账金额与费率计算金额不同，以人工核对后的账本金额为准。",
            "key-warning",
          ),
        ]
      : []),
    ...(r.status === "settled" && !r.upstream_verified
      ? [
          button("记录上游账单核对", () =>
            openForm(
              "记录上游账单核对",
              [
                node(
                  "p",
                  "仅在已核对上游账单后填写。金额必须与当前入账一致；此操作仅保存核对依据，不改变账本金额。",
                  "section-note",
                ),
                field(
                  "上游账单对应金额 / 元",
                  "cost",
                  "number",
                  (r.cost / 1e6).toFixed(6),
                ),
                field("账单标识、对应记录与核对依据", "reason", "textarea"),
              ],
              async (d) => {
                await api("/requests/" + r.id + "/bill-verification", "POST", {
                  cost: yuan(d.cost),
                  reason: d.reason,
                });
                await showRequestDetail(id, cache, reconcile);
              },
            ),
          ),
        ]
      : []),
    technical,
    ...(r.status === "pending" && reconcile
      ? [button("人工核对", () => reconcile(r))]
      : []),
  ];
  openForm("单次调用明细", fields, null);
  $("#modal").classList.add("wide-modal");
}
