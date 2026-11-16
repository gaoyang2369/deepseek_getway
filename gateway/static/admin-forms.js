import {
  $,
  node,
  operationId,
  notice,
  showError,
  confirmAction,
  api,
  field,
  yuan,
  models,
  button,
  table,
} from "./common.js";
let submitAction = null,
  modalComposing = false;
export function openForm(title, fields, action) {
  modalComposing = false;
  $("#modal-title").textContent = title;
  $("#modal-error").hidden = true;
  $("#modal-error").textContent = "";
  $("#modal").classList.remove("danger-modal");
  $("#modal-submit").textContent = "保存";
  $("#modal-fields").replaceChildren(...fields);
  $("#modal-submit").hidden = !action;
  $("#modal-form").dataset.operation = operationId();
  submitAction = action;
  if (!$("#modal").open) $("#modal").showModal();
}

export function closeModal() {
  modalComposing = false;
  $("#modal").close();
  $("#modal-fields").replaceChildren();
  $("#modal-error").hidden = true;
  submitAction = null;
}

export function showKey(result) {
  openForm(
    result.replayed ? "操作已完成" : "服务 Key 已签发",
    [
      node(
        "p",
        result.replayed
          ? "这是重复提交。完整 Key 无法再次读取，若原响应丢失，请轮换。"
          : "完整 Key 仅在此处显示一次。请安全交给对应同学。关闭后无法再次读取。",
        "key-warning",
      ),
      node("pre", result.key || "仅保留前缀：" + result.prefix),
      node("p", "API 地址：" + (result.base_url || location.origin + "/v1")),
      node(
        "small",
        "同一成员的所有 Key 共享个人预算。请勿把个人 Key 填入管理接口。",
      ),
    ],
    null,
  );
}

export async function deleteEntry(path, label, impact, load) {
  if (
    !(await confirmAction(
      "删除" + label + "？",
      impact + "\n历史调用、费用和账本保留；已发送的请求可能继续。",
    ))
  )
    return;
  await api(path, "DELETE");
  await load();
  notice("已删除" + label + "。", "success");
}

export function editStudentForm(student, load) {
  openForm(
    "编辑学生信息",
    [
      node(
        "p",
        "修改会同步到该学生所属的所有项目组，原账号、个人 Key、预算和使用记录保留。",
        "section-note",
      ),
      field("姓名", "name", "text", student.name),
      field(
        "学号（登录账号，保留前导零）",
        "student_number",
        "text",
        student.student_number,
      ),
      node(
        "p",
        "修改学号后，请通知学生使用新学号登录，密码仍为原密码。尚未修改初始密码的学生也继续使用原初始密码。",
        "section-note",
      ),
    ],
    async (d) => {
      await api("/students/" + student.student_id, "PATCH", d);
      closeModal();
      await load();
      notice("学生信息已更新。", "success");
    },
  );
  $("#modal-fields [name=name]").maxLength = 100;
  $("#modal-fields [name=student_number]").maxLength = 64;
}

export function showStudentEnrollment(result, number) {
  const message = result.account_created
    ? "网页登录已自动开通。"
    : "已添加成员关系，学生继续使用原登录密码。";
  const fields = [
    node("p", message + " 学生本人登录、首次改密后领取 Key。", "section-note"),
    node("p", "学生入口：" + result.student_url),
    node("p", "登录学号：" + number),
  ];
  if (result.temporary_password)
    fields.push(
      node("p", "随机临时密码仅显示一次，请单独交给学生。", "key-warning"),
      node("pre", result.temporary_password),
    );
  else if (
    result.account_created &&
    result.initial_password_mode === "student_number"
  )
    fields.push(
      node(
        "p",
        "当前为模拟环境，初始密码与学号相同，首次登录必须改密。",
        "key-warning",
      ),
    );
  openForm("学生账号已开通", fields, null);
}

export function clearTestUsageForm(teams, load, selected) {
  openForm(
    "清理项目组测试记录",
    [
      node(
        "p",
        "将删除所选项目组全部模拟调用、拒绝记录、费用记录、周期账本与相关告警。项目组、成员、学生、Key 和预算配置保留。此操作不能在页面撤回。",
        "key-warning",
      ),
      field(
        "选择项目组",
        "team_id",
        "text",
        selected || teams[0].id,
        teams.map((t) => [t.id, t.name + (t.deleted_at ? "（已删除）" : "")]),
      ),
      field("输入完整项目组名称以确认", "confirm_name"),
    ],
    async (d) => {
      const result = await api(
        "/teams/" + d.team_id + "/clear-test-usage",
        "POST",
        { confirm_name: d.confirm_name },
      );
      closeModal();
      await load();
      notice(
        "已清理 " +
          result.removed.calls +
          " 条模拟调用及对应账本。项目组和账号配置已保留。",
        "success",
      );
    },
  );
  $("#modal").classList.add("danger-modal");
  $("#modal-submit").textContent = "确认清理测试记录";
}

export function budgetForm(kind, id, budget, load) {
  openForm(
    "调整预算上限",
    [
      field("新上限 / 元", "amount", "number", String(budget / 1e6)),
      field("调整原因", "reason", "textarea"),
    ],
    async (d) => {
      await api("/budgets/" + kind + "/" + id, "POST", {
        amount: yuan(d.amount),
        reason: d.reason,
      });
      closeModal();
      await load();
    },
  );
}

export function keyForm(member, load, revoke) {
  if (!member) throw new Error("成员不存在");
  openForm(
    revoke ? "轮换个人 Key" : "为现有成员新增 Key",
    [
      field("允许模型", "models", "text", member.models.join(",")),
      field("到期时间（可留空）", "expires_at", "datetime-local"),
    ],
    async (d) => {
      const r = await api("/memberships/" + member.id + "/keys", "POST", {
        operation_id: $("#modal-form").dataset.operation,
        models: models(d.models),
        expires_at: d.expires_at ? new Date(d.expires_at).toISOString() : null,
        revoke_key_id: revoke || null,
      });
      showKey(r);
      await load();
    },
  );
  $("#modal-fields [name=expires_at]").required = false;
}

function validateModalForm() {
  for (const input of $("#modal-form").querySelectorAll(
    "input,select,textarea",
  )) {
    if (!input.willValidate) continue;
    if (input.name === "reason") input.value = input.value.trim();
    if (input.validity.valid) continue;
    const label =
      input.closest("label")?.firstChild?.textContent?.trim() || "该项";
    const message = input.validity.valueMissing
      ? "请填写" + label + "。"
      : "请检查" + label + "的格式、长度或范围。";
    $("#modal-error").textContent = message;
    $("#modal-error").hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return false;
  }
  return true;
}
$("#close-modal").addEventListener("click", closeModal);
$("#modal").addEventListener("cancel", closeModal);

// IME commits and implicit Enter submissions must never save an admin change.
$("#modal-form").addEventListener("submit", (e) => e.preventDefault());
$("#modal-form").addEventListener("compositionstart", () => {
  modalComposing = true;
});
$("#modal-form").addEventListener("compositionend", () => {
  modalComposing = false;
});
$("#modal-form").addEventListener("input", (e) => {
  e.target.removeAttribute("aria-invalid");
  $("#modal-error").hidden = true;
});
$("#modal-submit").addEventListener("click", async () => {
  if (modalComposing || !submitAction || $("#modal-submit").disabled) return;
  $("#modal-error").hidden = true;
  if (!validateModalForm()) return;
  $("#modal-submit").disabled = true;
  try {
    await submitAction(Object.fromEntries(new FormData($("#modal-form"))));
    if (!$("#modal").open && !$("#notice").textContent)
      notice("更改已保存。", "success");
  } catch (err) {
    showError(err);
  } finally {
    $("#modal-submit").disabled = false;
  }
});

export function initialPasswordField(config, label = "初始密码方式") {
  return field(
    label,
    "initial_password_mode",
    "text",
    config.initial_password_mode,
    config.demo
      ? [
          ["student_number", "学号（当前模拟环境）"],
          ["random", "随机临时密码"],
        ]
      : [["random", "随机临时密码"]],
  );
}

export function batchEnrollmentForm(
  cache,
  load,
  selected,
  allowNumberOnly = false,
) {
  const teams = cache.teams.filter((t) => t.active);
  if (!teams.length) throw new Error("请先创建或启用项目组");
  const team = teams.find((t) => t.id === selected) || teams[0];
  const rosterField = field("成员名单（学号、姓名）", "roster", "textarea");
  const rosterInput = rosterField.querySelector("textarea");
  rosterInput.rows = 6;
  rosterInput.placeholder = "0012026001,张三\n0012026002,李四";
  const preview = node("div");
  preview.id = "roster-preview";
  preview.setAttribute("aria-live", "polite");
  function parseRoster() {
    const students = [],
      seen = new Map();
    for (const [index, raw] of rosterInput.value.split(/\r?\n/).entries()) {
      const line = raw.trim();
      if (!line) continue;
      const parts = line.split(/[,，\t]/).map((part) => part.trim());
      const [student_number, name] = parts;
      const prefix = `第 ${index + 1} 行：`;
      if (parts.length > 2 || !student_number || (!allowNumberOnly && !name))
        throw new Error(
          prefix + "请填写“学号,姓名”，也可从 Excel 粘贴这两列。",
        );
      if (!/^[a-zA-Z0-9_-]{1,64}$/.test(student_number))
        throw new Error(
          prefix + "学号仅支持字母、数字、下划线和短横线，最多 64 个字符。",
        );
      if (name && name.length > 100)
        throw new Error(prefix + "姓名不能超过 100 个字符。");
      if (seen.has(student_number))
        throw new Error(
          prefix +
            `学号 ${student_number} 与第 ${seen.get(student_number)} 行重复，请删除重复行。`,
        );
      seen.set(student_number, index + 1);
      students.push({ student_number, ...(name ? { name } : {}) });
    }
    if (!students.length) throw new Error("请至少填写一位成员。");
    if (students.length > 100)
      throw new Error("每批最多添加 100 人，请分批提交。");
    return students;
  }
  const previewButton = button("预览名单", () => {
    preview.replaceChildren();
    const students = parseRoster();
    preview.append(
      node("p", `共 ${students.length} 人，请核对学号和姓名。`),
      table(
        ["序号", "学号", "姓名"],
        students.map((s, i) => [
          i + 1,
          s.student_number,
          s.name || "以学号作为姓名",
        ]),
      ),
    );
  });
  rosterInput.addEventListener("input", () => preview.replaceChildren());
  openForm(
    "批量添加成员",
    [
      node(
        "p",
        "每行填写“学号,姓名”，支持中文逗号或从 Excel 粘贴两列，学号保留前导零。每批最多 100 人。" +
          (allowNumberOnly ? "也可每行只填学号。" : ""),
        "section-note",
      ),
      field(
        "项目组",
        "team_id",
        "text",
        team.id,
        teams.map((t) => [t.id, t.name]),
      ),
      rosterField,
      previewButton,
      preview,
      node(
        "p",
        "以下设置用于本批新增成员。已在组内的成员会跳过，保留原密码、预算、权限与状态；姓名冲突时整批不添加。",
        "section-note",
      ),
      initialPasswordField(cache.config),
      field("每人预算 / 元", "budget", "number", "0"),
      field("周期", "period", "text", "month", [
        ["month", "自然月"],
        ["day", "自然日"],
      ]),
      field(
        "允许模型（英文逗号分隔）",
        "models",
        "text",
        team.models.join(","),
      ),
      field("接入到期时间（可留空）", "expires_at", "datetime-local"),
    ],
    async (d) => {
      const students = parseRoster();
      const result = await api("/students/import", "POST", {
        operation_id: $("#modal-form").dataset.operation,
        team_id: d.team_id,
        students,
        initial_password_mode: d.initial_password_mode,
        budget: yuan(d.budget),
        period: d.period,
        models: models(d.models),
        expires_at: d.expires_at ? new Date(d.expires_at).toISOString() : null,
      });
      const added = result.students.filter((s) => s.membership_created).length;
      const passwords = new Map(
        result.initial_passwords.map((s) => [s.student_number, s.password]),
      );
      const summary = result.replayed
        ? "此批次已处理，无需重复添加。"
        : `新增 ${added} 位成员，已有 ${result.students.length - added} 位成员。`;
      openForm(
        "批量添加结果",
        [
          node("p", summary, "section-note"),
          node("p", "学生入口：" + result.student_url),
          node(
            "p",
            result.replayed
              ? "随机初始密码无法再次显示；若未保存，请在“学生账号”中为对应学生重置密码。"
              : "新账号首次登录须修改密码，再自行领取 Key。随机临时密码仅显示一次，请单独交给对应学生。",
            "key-warning",
          ),
          table(
            ["学号", "姓名", "成员状态", "登录密码"],
            result.students.map((s) => [
              s.student_number,
              s.name ||
                students.find(
                  (item) => item.student_number === s.student_number,
                )?.name ||
                s.student_number,
              result.replayed
                ? "已处理"
                : s.membership_created
                  ? "已添加"
                  : "已存在，保留原设置",
              passwords.get(s.student_number) ||
                (!s.account_created
                  ? "使用原密码"
                  : result.initial_password_mode === "student_number"
                    ? "初始密码与学号相同"
                    : "仅首次响应显示，遗失请重置"),
            ]),
          ),
        ],
        null,
      );
      await load();
      notice(summary, "success");
    },
  );
  $("#modal-submit").textContent = "批量添加";
  $("#modal-fields [name=expires_at]").required = false;
  $("#modal-fields [name=team_id]").addEventListener("change", (e) => {
    $("#modal-fields [name=models]").value = teams
      .find((t) => t.id === e.target.value)
      .models.join(",");
  });
}
