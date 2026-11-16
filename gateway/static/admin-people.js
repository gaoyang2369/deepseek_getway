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
} from "./forms.js";
export async function teamsView(cache, load) {
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
            field("每分钟请求数", "rpm", "number", "60"),
            field("最大并发", "concurrency", "number", "10"),
            field("单次输出 Token 上限", "max_output", "number", "2048"),
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
      ["名称", "状态", "模型", "预算", "RPM / 并发", "操作"],
      cache.teams.map((t) => [
        t.name,
        badge(t.active),
        t.models.join(", "),
        money(t.budget) + " / " + t.period,
        t.rpm + " / " + t.concurrency,
        actions(
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
      ]),
    ),
  ];
}
const memberPage = { offset: 0, size: 10 };
const keyPage = { offset: 0, size: 10 };
let memberTeam = "";
export async function membersView(cache, load) {
  if (memberTeam && !cache.teams.some((t) => t.id === memberTeam)) {
    memberTeam = "";
    memberPage.offset = keyPage.offset = 0;
  }
  const filters = memberTeam ? { team_id: memberTeam } : {};
  const [members, keys] = await Promise.all([
    listPage("/memberships", filters, memberPage),
    listPage("/keys", filters, keyPage),
  ]);
  const teamFilter = field("项目组", "member_team", "text", memberTeam, [
    ["", "全部"],
    ...cache.teams.map((t) => [t.id, t.name + (t.active ? "" : "（已停用）")]),
  ]);
  teamFilter.className = "table-header-filter";
  teamFilter.querySelector("select").setAttribute("aria-label", "按项目组查看");
  teamFilter.querySelector("select").addEventListener("change", (e) => {
    memberTeam = e.target.value;
    memberPage.offset = keyPage.offset = 0;
    load().catch(showError);
  });
  const memberPager = () =>
    listPagination(memberPage, members.total, "成员", load);
  const keyPager = () => listPagination(keyPage, keys.total, "密钥", load);
  return [
    toolbar(
      "成员关系",
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
    ),
    table(
      ["姓名", "学号 / 编号", teamFilter, "状态", "预算", "操作"],
      members.rows.map((m) => [
        m.name,
        m.student_number,
        m.team,
        badge(m.active),
        money(m.budget) + " / " + m.period,
        actions(
          ...(m.kind === "service"
            ? [button("签发服务 Key", () => keyForm(m, load))]
            : []),
          button("调整预算", () =>
            budgetForm("membership", m.id, m.budget, load),
          ),
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
              await load();
            },
            "danger",
          ),
          button(
            "删除成员",
            () =>
              deleteEntry(
                "/memberships/" + m.id,
                "成员“" + m.name + " / " + m.team + "”",
                "仅移除此组成员关系并使相关 Key 失效，学生账号和其他项目组不受影响。",
                load,
              ),
            "danger",
          ),
        ),
      ]),
    ),
    memberPager(),
    toolbar("个人密钥（仅显示前缀）"),
    table(
      ["前缀", "成员", "模型", "状态", "到期", "操作"],
      keys.rows.map((k) => [
        k.prefix + "…",
        k.member.name,
        k.models.join(","),
        badge(k.active),
        date(k.expires_at),
        k.member.kind === "service"
          ? actions(
              button(
                "吊销",
                async () => {
                  await api("/keys/" + k.id + "/revoke", "POST", {});
                  await load();
                },
                "danger",
              ),
              button("轮换服务 Key", () => keyForm(k.member, load, k.id)),
            )
          : node("span", "学生自助管理", "muted"),
      ]),
    ),
    keyPager(),
  ];
}
