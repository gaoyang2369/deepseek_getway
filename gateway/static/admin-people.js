import {
  $,
  node,
  money,
  date,
  confirmAction,
  api,
  button,
  badge,
  table,
  actions,
  toolbar,
  field,
  yuan,
  models,
  listPage,
  listPagination,
  showError,
  stacked,
  moreActions,
  disclosure,
  periodUnit,
  progress,
} from "./common.js";
import {
  initialPasswordField,
  openForm,
  closeModal,
  showKey,
  deleteEntry,
  showStudentEnrollment,
  budgetForm,
  keyForm,
  batchEnrollmentForm,
  editStudentForm,
} from "./forms.js";
import { openAnalysis, setReportFilters, bounds } from "./consumption.js";
export async function teamsView(cache, load) {
  const labels = cache.labels;
  const budgets = new Map(
    (await api("/budget-status?include_month_cost=true")).map((b) => [
      b.owner_id,
      b,
    ]),
  );
  return [
    toolbar(
      "项目组与上游凭证",
      button("＋ 创建项目组", () =>
        openForm(
          "创建项目组",
          [
            field("项目组名称", "name"),
            field("DeepSeek 官方 Key（加密保存）", "upstream_key", "password"),
            field("允许模型，以英文逗号分隔", "models"),
            field("组预算 / 元", "budget", "number", "0"),
            field("预算周期", "period", "text", "month", [
              ["month", "自然月"],
              ["day", "自然日"],
            ]),
            field("每分钟请求数（0＝不限制）", "rpm", "number", "0"),
            field("最大并发（0＝不限制）", "concurrency", "number", "0"),
            field(
              "单次输出 Token 上限（不是上下文长度）",
              "max_output",
              "number",
              "2048",
            ),
          ],
          async (d) => {
            await api("/teams", "POST", {
              ...d,
              models: models(d.models),
              budget: yuan(d.budget),
              rpm: +d.rpm,
              concurrency: +d.concurrency,
              max_output: +d.max_output,
            });
            closeModal();
            await load();
          },
        ),
      ),
    ),
    table(
      [
        "名称",
        { label: "本月已用 / 预算上限", numeric: true },
        "状态",
        { label: "模型", secondary: true },
        { label: "RPM / 并发", secondary: true },
        "操作",
      ],
      cache.teams.map((t) => [
        stacked(t.name, labels.test_team_ids.includes(t.id) ? "测试数据" : ""),
        stacked(
          money(budgets.get(t.id)?.month_cost || 0),
          "上限 " +
            money(budgets.get(t.id)?.limit ?? t.budget).slice(1) +
            " " +
            periodUnit(t.period),
        ),
        badge(t.active),
        t.models.join(", "),
        (t.rpm || "不限") + " / " + (t.concurrency || "不限"),
        actions(
          button("查看消费", () => {
            setReportFilters({ ...bounds("month"), team_id: t.id }, "month");
            return openAnalysis(cache, {}, "member");
          }),
          moreActions(
            button("管理", () =>
              openForm(
                "管理项目组 · " + t.name,
                [
                  field("项目组名称", "name", "text", t.name),
                  field(
                    "允许模型，以英文逗号分隔",
                    "models",
                    "text",
                    t.models.join(","),
                  ),
                  field("每分钟请求数（0＝不限制）", "rpm", "number", t.rpm),
                  field("最大并发（0＝不限制）", "concurrency", "number", t.concurrency),
                  field(
                    "单次输出 Token 上限",
                    "max_output",
                    "number",
                    t.max_output,
                  ),
                  node(
                    "p",
                    "预算调整、更换凭证及停用操作见项目的更多菜单。",
                    "section-note",
                  ),
                ],
                async (d) => {
                  await api("/teams/" + t.id, "PATCH", {
                    ...d,
                    models: models(d.models),
                    rpm: +d.rpm,
                    concurrency: +d.concurrency,
                    max_output: +d.max_output,
                  });
                  closeModal();
                  await load();
                },
              ),
            ),
            button(
              labels.test_team_ids.includes(t.id)
                ? "移除测试标签"
                : "标记测试项目",
              async () => {
                await api("/teams/" + t.id + "/report-label", "PUT", {
                  is_test: !labels.test_team_ids.includes(t.id),
                });
                await load();
              },
            ),
            ...(t.active
              ? [
                  button("批量添加成员", () =>
                    batchEnrollmentForm(cache, load, t.id),
                  ),
                ]
              : []),
            button(t.active ? "停用" : "启用", async () => {
              await api("/teams/" + t.id, "PATCH", { active: !t.active });
              await load();
            }),
            button("更换官方 Key", () =>
              openForm(
                "更换上游凭证",
                [field("新 DeepSeek 官方 Key", "upstream_key", "password")],
                async (d) => {
                  await api("/teams/" + t.id + "/credential", "POST", d);
                  closeModal();
                  await load();
                },
              ),
            ),
            button("调整预算", () => budgetForm("team", t.id, t.budget, load)),
            button(
              "删除项目组",
              () =>
                deleteEntry(
                  "/teams/" + t.id,
                  "项目组“" + t.name + "”",
                  "组内全部成员关系会被移除，相关 API Key 将失效。学生账号及其在其他组的权限保留。",
                  load,
                ),
              "danger",
            ),
          ),
        ),
      ]),
      "compact-table team-table",
    ),
  ];
}
function enrollmentToolbar(cache, load, memberTeam = "") {
  return toolbar(
    "成员目录",
    button("＋ 批量导入学号", () =>
      batchEnrollmentForm(cache, load, undefined, true),
    ),
    button("＋ 批量添加成员", () =>
      batchEnrollmentForm(cache, load, memberTeam),
    ),
    button("＋ 添加人员", () => {
      if (!cache.teams.length) throw new Error("请先创建项目组");
      openForm(
        "添加人员 · 学生自行登录领 Key",
        [
          field("姓名或服务身份名称", "name"),
          field("学号 / 服务身份唯一编号（保留前导零）", "student_number"),
          field("身份类型", "kind", "text", "student", [
            ["student", "学生"],
            ["service", "公共脚本 / 服务身份"],
          ]),
          field("负责人（服务身份必填；学生留空）", "owner"),
          initialPasswordField(cache.config, "学生初始密码"),
          field(
            "项目组",
            "team_id",
            "text",
            memberTeam || cache.teams[0].id,
            cache.teams.map((t) => [t.id, t.name]),
          ),
          field("个人预算 / 元", "budget", "number", "0"),
          field("周期", "period", "text", "month", [
            ["month", "自然月"],
            ["day", "自然日"],
          ]),
          field(
            "允许模型（英文逗号分隔）",
            "models",
            "text",
            (
              cache.teams.find((t) => t.id === memberTeam) || cache.teams[0]
            ).models.join(","),
          ),
          field("到期时间（可留空）", "expires_at", "datetime-local"),
        ],
        async (d) => {
          const result = await api("/enroll", "POST", {
            ...d,
            operation_id: $("#modal-form").dataset.operation,
            owner: d.owner || null,
            models: models(d.models),
            budget: yuan(d.budget),
            expires_at: d.expires_at
              ? new Date(d.expires_at).toISOString()
              : null,
          });
          if (d.kind === "student")
            showStudentEnrollment(result, d.student_number);
          else showKey(result);
          await load();
        },
      );
      $("#modal-fields [name=owner]").required = false;
      $("#modal-fields [name=expires_at]").required = false;
    }),
  );
}
const peoplePage = { offset: 0, size: 10 };
let peopleSearch = "",
  peopleTeam = "";
function accountState(p) {
  return p.kind === "service"
    ? "服务身份"
    : p.must_change_password === null
      ? "待设置初始密码"
      : p.must_change_password
        ? "待首次改密"
        : "已设置密码";
}
function resetPassword(p, load) {
  openForm(
    "核实身份后生成临时密码",
    [
      node(
        "p",
        "旧会话和该学生全部旧 API Key 将失效。已发送请求仍可能执行，已用预算保留。",
        "key-warning",
      ),
      field("身份核实记录与重置原因", "reason", "textarea"),
    ],
    async (d) => {
      const result = await api(
        "/students/" + p.student_id + "/reset-password",
        "POST",
        { reason: d.reason },
      );
      openForm(
        "临时密码（仅显示一次）",
        [
          node(
            "p",
            "请核实身份后通过可靠渠道单独交给对应同学。",
            "key-warning",
          ),
          node("pre", p.student_number + "\t" + result.temporary_password),
          node("p", "学生入口：" + location.origin + "/student"),
        ],
        null,
      );
      await load();
    },
  );
}
async function memberDetail(p, cache, load, keyPage = { offset: 0, size: 10 }) {
  const [members, keys, budgets, spend] = await Promise.all([
    api(
      "/memberships?include_deleted=true&student_id=" +
        encodeURIComponent(p.student_id),
    ),
    listPage("/keys", { student_id: p.student_id }, keyPage),
    api(
      "/budget-status?owner_type=membership&student_id=" +
        encodeURIComponent(p.student_id),
    ),
    api(
      "/consumption?" +
        new URLSearchParams({
          ...bounds("month"),
          student_id: p.student_id,
          include_trend: false,
          limit: 1,
        }),
    ),
  ]);
  const refresh = async () => {
    closeModal();
    await load();
  };
  const projects = members.map((m) => {
    const card = node("section", undefined, "member-project");
    card.append(
      stacked(
        m.team + (m.deleted_at ? "（已删除）" : ""),
        "个人额度 " + money(m.budget).slice(1) + " " + periodUnit(m.period),
      ),
      node("p", "允许模型：" + m.models.join("、"), "section-note"),
      node("p", "个人容量保护：每分钟 " + (m.rpm || "不单独限制") + "；并发 " + (m.concurrency || "不单独限制"), "section-note"),
    );
    const budget = budgets.find((b) => b.owner_id === m.id);
    if (budget)
      card.append(
        stacked(
          "当前周期已用 " + money(budget.spent),
          "剩余额度 " + money(budget.available),
        ),
        progress(budget.spent, budget.limit),
      );
    if (!m.deleted_at)
      card.append(
        actions(
          button("调整预算", () =>
            budgetForm("membership", m.id, m.budget, refresh),
          ),
          ...(m.kind === "service"
            ? [button("签发服务 Key", () => keyForm(m, refresh))]
            : []),
          moreActions(
            button("容量保护（可选）", () => openForm(
              "个人容量保护",
              [
                node("p", "默认 0，不单独限制个人；项目组容量保护仍生效。预算按费用独立检查。", "section-note"),
                field("每分钟请求数（0＝不单独限制）", "rpm", "number", m.rpm),
                field("最大并发（0＝不单独限制）", "concurrency", "number", m.concurrency),
              ],
              async (d) => {
                await api("/memberships/" + m.id + "/limits", "PATCH", {rpm: +d.rpm, concurrency: +d.concurrency});
                await refresh();
              },
            )),
            button(
              "关闭成员关系",
              async () => {
                if (
                  !(await confirmAction(
                    "关闭成员关系？",
                    "该成员在此组的全部 Key 将失效，已发送的请求可能继续。",
                    "确认关闭",
                  ))
                )
                  return;
                await api("/memberships/" + m.id + "/disable", "POST", {});
                await refresh();
              },
              "danger",
            ),
            button(
              "删除成员",
              () =>
                deleteEntry(
                  "/memberships/" + m.id,
                  "成员“" + m.name + " / " + m.team + "”",
                  "仅移除此组成员关系并使相关 Key 失效，账号和其他项目组保留。",
                  refresh,
                ),
              "danger",
            ),
          ),
        ),
      );
    return card;
  });
  openForm(
    "成员详情 · " + p.name,
    [
      stacked(p.name + " · " + p.student_number, accountState(p)),
      toolbar(
        "本月已入账 " + money(spend.summary.cost),
        button("查看消费构成 →", async () => {
          closeModal();
          setReportFilters(
            { ...bounds("month"), student_id: p.student_id },
            "month",
          );
          await openAnalysis(cache, {}, "model");
        }),
      ),
      ...(p.kind === "student"
        ? [
            actions(
              button("编辑信息", () => editStudentForm(p, refresh)),
              moreActions(
                button("重置密码", () => resetPassword(p, load)),
                button(
                  "删除学生",
                  () =>
                    deleteEntry(
                      "/students/" + p.student_id,
                      "学生“" + p.name + "”",
                      "所有项目组成员关系、API Key 和登录会话将失效。",
                      refresh,
                    ),
                  "danger",
                ),
              ),
            ),
          ]
        : []),
      node("h3", "项目归属与个人额度"),
      ...projects,
      node("h3", "密钥 · 仅显示前缀"),
      table(
        ["前缀", "状态", { label: "模型 / 到期", secondary: true }, "操作"],
        keys.rows.map((k) => [
          k.prefix + "…",
          badge(k.active),
          stacked(k.models.join("、"), date(k.expires_at)),
          k.member.kind === "service"
            ? moreActions(
                button(
                  "吊销",
                  async () => {
                    await api("/keys/" + k.id + "/revoke", "POST", {});
                    await refresh();
                  },
                  "danger",
                ),
                button("轮换服务 Key", () => keyForm(k.member, refresh, k.id)),
              )
            : node("span", "学生自助管理", "muted"),
        ]),
        "compact-table",
      ),
      listPagination(keyPage, keys.total, "成员密钥", () =>
        memberDetail(p, cache, load, keyPage),
      ),
    ],
    null,
  );
  $("#modal").classList.add("wide-modal");
}
export async function membersView(cache, load) {
  const people = await listPage(
    "/people",
    { q: peopleSearch, team_id: peopleTeam },
    peoplePage,
  );
  const search = node("form", undefined, "filters");
  const term = field("学号 / 姓名", "people_search", "search", peopleSearch);
  const team = field("项目组", "member_team", "text", peopleTeam, [
    ["", "全部项目组"],
    ...cache.teams.map((t) => [t.id, t.name]),
  ]);
  team.querySelector("select").setAttribute("aria-label", "按项目组查看");
  const apply = () => {
    peopleSearch = term.querySelector("input").value.trim();
    peopleTeam = team.querySelector("select").value;
    peoplePage.offset = 0;
    return load();
  };
  const submit = node("button", "查询");
  submit.type = "submit";
  search.append(
    term,
    team,
    submit,
    button("清空搜索", () => {
      peopleSearch = peopleTeam = "";
      peoplePage.offset = 0;
      return load();
    }),
  );
  search.querySelectorAll("input,select").forEach((i) => {
    i.required = false;
  });
  search.onsubmit = (e) => {
    e.preventDefault();
    if (!e.isComposing) apply().catch(showError);
  };
  team.querySelector("select").onchange = () => apply().catch(showError);
  return [
    enrollmentToolbar(cache, load, peopleTeam),
    node(
      "p",
      "按成员汇总账号、项目归属与消费。学生与服务身份统一查找；密钥和预算在成员详情中管理。",
      "section-note",
    ),
    search,
    table(
      [
        "姓名 / 账号",
        { label: "本月已入账", numeric: true },
        "项目归属",
        "账号状态",
        "操作",
      ],
      people.rows.map((p) => [
        stacked(p.name, p.student_number),
        money(p.month_cost),
        projectSummary(p.teams),
        accountState(p),
        button("管理", () => memberDetail(p, cache, load)),
      ]),
      "compact-table members-table",
    ),
    listPagination(peoplePage, people.total, "成员", load),
  ];
}

function projectSummary(teams) {
  if (!teams.length) return node("span", "未分配项目");
  const group = node("div", undefined, "project-summary");
  group.append(node("span", teams[0].name));
  if (teams.length > 1) {
    const extra = disclosure(
      `另有 ${teams.length - 1} 个项目`,
      ...teams.slice(1).map((t) => node("div", t.name)),
    );
    extra.classList.add("project-expansion");
    group.append(extra);
  }
  return group;
}
