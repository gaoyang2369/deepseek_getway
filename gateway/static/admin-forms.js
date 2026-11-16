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
        "将删除所选项目组全部模拟调用、费用记录、周期账本与相关异常事件。项目组、成员、学生、Key 和预算配置保留。此操作不能在页面撤回。",
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
