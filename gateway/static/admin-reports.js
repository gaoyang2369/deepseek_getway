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
let filters = {};
export async function overviewView(cache, load) {
  const [stats, metrics] = await Promise.all([
    api("/statistics"),
    api("/metrics"),
  ]);
  const cards = node("div", undefined, "cards");
  for (const [label, value] of [
    ["累计请求", stats.reduce((a, r) => a + r.calls, 0)],
    ["已结算费用", money(stats.reduce((a, r) => a + r.cost, 0))],
    ["待核对金额", money(metrics.pending_micro_cny)],
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
    node("p", "按项目组和个人归因 · 金额按准入时计价版本计算", "section-note"),
    cards,
    toolbar("用量明细"),
    table(
      [
        "项目组",
        "人员",
        "Key",
        "模型",
        "请求 / 成功",
        "输入 / 输出",
        "缓存命中",
        "费用",
      ],
      stats.map((r) => [
        teamName(cache, r.team_id),
        studentName(cache, r.student_id),
        short(r.key_id),
        r.model,
        r.calls + " / " + r.success,
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
      "这里同时展示项目组与个人的周期预算。组记录汇总全组使用情况，个人记录用于个人限额；两者是同一笔调用的两个归属，请勿相加。可用金额 = 预算上限 − 已结算 − 未结算预留。",
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
        "预留",
        "可用",
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
        money(p.reserved),
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
  );
  form.querySelectorAll("input,select").forEach((i) => (i.required = false));
  const submit = node("button", "筛选");
  form.append(submit);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    filters = Object.fromEntries(new FormData(form));
    load().catch((e) => showError(e));
  });
  const params = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v),
  );
  params.set("limit", "1000");
  const rows = await api("/requests?" + params);
  const a = node("a", "导出当前筛选 CSV（最多 1000 行）");
  a.href = "/admin/api/export.csv?" + params;
  return [
    form,
    toolbar("最近请求"),
    a,
    table(
      [
        "时间",
        "人员 / 组",
        "模型",
        "状态",
        "费用",
        "预留",
        "输入 / 输出",
        "操作",
      ],
      rows.map((r) => [
        date(r.started_at),
        r.name + " / " + teamName(cache, r.team_id),
        r.model,
        badge(r.status),
        r.cost === null
          ? "待核对"
          : money(r.cost) + (r.estimated ? " 估算" : ""),
        money(r.reservation),
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
            ? [
                button("人工核对", () =>
                  openForm(
                    "核对费用并结算",
                    [
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
                  ),
                ),
              ]
            : []),
        ),
      ]),
    ),
  ];
}
export async function eventsView(cache, load) {
  const rows = await api("/events");
  return [
    node(
      "p",
      "新来源 IP 仅提示网络变化，不代表违规。待核对请求可能占用预算和保守并发名额，请核实上游执行结果后处理。",
      "section-note",
    ),
    table(
      ["时间", "事件", "项目组", "详情", "状态", "操作"],
      rows.map((r) => [
        date(r.created_at),
        r.kind,
        teamName(cache, r.team_id),
        JSON.stringify(r.detail),
        r.resolved ? "已处理" : "待处理",
        button("标记处理", async () => {
          await api("/events/" + r.id + "/resolve", "POST", {});
          await load();
        }),
      ]),
    ),
  ];
}
export async function auditView(cache, load) {
  const rows = await api("/audit");
  return [
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
  ];
}
