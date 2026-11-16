import {
  node,
  api,
  button,
  table,
  actions,
  toolbar,
  field,
  listPage,
  listPagination,
  showError,
} from "./common.js";
import {
  openForm,
  deleteEntry,
  batchEnrollmentForm,
  editStudentForm,
} from "./forms.js";
const studentPage = { offset: 0, size: 10 };
let studentSearch = "";
export async function studentAdminView(cache, load) {
  const people = await listPage("/students", { q: studentSearch }, studentPage);
  const search = node("form", undefined, "toolbar");
  search.setAttribute("role", "search");
  const searchField = field(
    "学号 / 姓名",
    "student_search",
    "search",
    studentSearch,
  );
  const input = searchField.querySelector("input");
  input.required = false;
  input.maxLength = 100;
  input.placeholder = "输入完整或部分学号、姓名";
  const submit = node("button", "查询", "secondary");
  submit.type = "submit";
  search.addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.isComposing) return;
    studentSearch = input.value.trim();
    studentPage.offset = 0;
    load().catch(showError);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.isComposing || e.keyCode === 229))
      e.preventDefault();
  });
  search.append(
    searchField,
    actions(
      submit,
      button("清空搜索", () => {
        studentSearch = "";
        studentPage.offset = 0;
        return load();
      }),
    ),
  );
  const pager = () =>
    listPagination(studentPage, people.total, "学生账号", load);
  function showTemporary(title, result) {
    openForm(
      title,
      [
        node(
          "p",
          "临时密码仅在此次响应显示。请核实身份后通过可靠渠道单独交给对应同学。",
          "key-warning",
        ),
        node("pre", result),
        node("p", "学生入口：" + location.origin + "/student"),
      ],
      null,
    );
  }
  const importButton = button("＋ 批量导入学号", () =>
    batchEnrollmentForm(cache, load, undefined, true),
  );
  return [
    toolbar("学生自助入口", importButton),
    node(
      "p",
      "学生访问 " +
        location.origin +
        "/student。首次登录必须改密，之后自行领取个人 Key。忘记密码时，通过邮箱联系管理员，管理员核实身份后重置。重置会撤销该学生的全部旧会话与 API Key，保留已用预算。",
      "section-note",
    ),
    search,
    table(
      ["姓名", "学号", "账号状态", "操作"],
      people.rows.map((p) => {
        return [
          p.name,
          p.student_number,
          p.must_change_password !== null
            ? p.must_change_password
              ? "待首次改密"
              : "已设置密码"
            : "待设置初始密码",
          actions(
            button("编辑信息", () => editStudentForm(p, load)),
            button("重置密码", () =>
              openForm(
                "核实身份后生成临时密码",
                [
                  node(
                    "p",
                    "旧会话和该学生全部旧 API Key 将失效。已发送请求仍可能执行，预算不会清零。",
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
                  showTemporary(
                    "临时密码（仅显示一次）",
                    p.student_number + "\t" + result.temporary_password,
                  );
                  await load();
                },
              ),
            ),
            button(
              "删除学生",
              () =>
                deleteEntry(
                  "/students/" + p.student_id,
                  "学生“" + p.name + " / " + p.student_number + "”",
                  "该学生在所有项目组的成员关系会被移除，全部 API Key 和登录会话将失效。项目组及其他学生保留。",
                  load,
                ),
              "danger",
            ),
          ),
        ];
      }),
    ),
    pager(),
  ];
}
