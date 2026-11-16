"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const operationId = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (v) =>
    v.toString(16).padStart(2, "0"),
  ).join("");
const money = (v) => "¥" + (Number(v || 0) / 1e6).toFixed(6);
let csrf = sessionStorage.getItem("lab_student_csrf") || "",
  profile = null,
  firstLoginPassword = null;
const errors = {
  invalid_login: "学号或密码错误，或账号尚未开通。",
  student_rate_limit: "尝试过于频繁，请稍后再试。",
  invalid_current_password: "当前密码不正确。",
  weak_or_unchanged_password:
    "请使用与当前密码、学号不同的新密码，至少包含 4 种不同字符。",
  password_change_required: "请先修改初始密码。",
  active_key_exists_use_rotate: "已有有效 Key。遗失时请使用轮换。",
  identity_disabled: "该项目组权限已暂停，请联系管理员。",
  expiry_in_past: "成员接入权限已到期，请联系管理员。",
  invalid_origin: "访问地址与服务器配置不一致，请联系管理员。",
};
function notice(text) {
  $("#student-notice").textContent = text;
}
function confirmRotation() {
  return new Promise((resolve) => {
    const d = $("#student-confirm-dialog");
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      d.close();
      resolve(value);
    };
    $("#student-confirm-cancel").onclick = () => finish(false);
    $("#student-confirm-submit").onclick = () => finish(true);
    d.oncancel = (e) => {
      e.preventDefault();
      finish(false);
    };
    d.onclose = () => finish(false);
    d.showModal();
    $("#student-confirm-cancel").focus();
  });
}
function clearKey() {
  const d = $("#student-key-dialog");
  if (d.open) d.close();
  $("#student-key-value").textContent = "";
  $("#student-key-base").textContent = "";
}
function maskPasswords(form) {
  form.querySelectorAll(".password-toggle").forEach((button) => {
    document.getElementById(button.dataset.passwordTarget).type = "password";
    button.textContent = "显示";
    button.setAttribute("aria-label", "显示" + button.dataset.passwordLabel);
    button.setAttribute("aria-pressed", "false");
  });
}
function clearPasswordForm() {
  const form = $("#student-password-form");
  form.reset();
  maskPasswords(form);
  const current = form.elements.current_password;
  current.readOnly = false;
  current.autocomplete = "current-password";
  $("#current-password-hint").textContent = "";
}
document.querySelectorAll(".password-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.passwordTarget);
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    button.textContent = visible ? "隐藏" : "显示";
    button.setAttribute(
      "aria-label",
      (visible ? "隐藏" : "显示") + button.dataset.passwordLabel,
    );
    button.setAttribute("aria-pressed", String(visible));
  });
});
window.addEventListener("pagehide", () => {
  firstLoginPassword = null;
  clearPasswordForm();
  $("#student-login-form").reset();
  maskPasswords($("#student-login-form"));
});
function loginScreen() {
  clearKey();
  firstLoginPassword = null;
  clearPasswordForm();
  maskPasswords($("#student-login-form"));
  profile = null;
  sessionStorage.removeItem("lab_student_csrf");
  csrf = "";
  $("#student-login").hidden = false;
  $("#student-password").hidden = true;
  $("#student-profile").hidden = true;
  $("#student-logout").hidden = true;
}
async function api(path, method = "GET", body) {
  const r = await fetch("/student/api" + path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await r.json();
  if (!r.ok) {
    const code = data.error?.message;
    if (code === "student_login_required") loginScreen();
    throw new Error(errors[code] || code || "请求失败");
  }
  return data;
}
function session(data) {
  csrf = data.csrf_token;
  sessionStorage.setItem("lab_student_csrf", csrf);
}
function passwordScreen() {
  clearKey();
  clearPasswordForm();
  const supplied = profile?.must_change_password && firstLoginPassword !== null;
  const current = $("#student-password-form").elements.current_password;
  if (supplied) current.value = firstLoginPassword;
  current.readOnly = !!supplied;
  current.autocomplete = supplied ? "off" : "current-password";
  $("#current-password-hint").textContent = supplied
    ? "已自动带入本次登录密码，无需再次输入。"
    : "请输入当前登录密码；新密码与确认新密码需保持一致。";
  $("#student-login").hidden = true;
  $("#student-profile").hidden = true;
  $("#student-password").hidden = false;
  $("#password-title").textContent = profile?.must_change_password
    ? "请先修改初始密码"
    : "修改登录密码";
  $("#password-cancel").hidden = !!profile?.must_change_password;
}
async function load() {
  profile = await api("/me");
  $("#student-login").hidden = true;
  $("#student-logout").hidden = false;
  if (profile.must_change_password) {
    passwordScreen();
    return;
  }
  firstLoginPassword = null;
  clearPasswordForm();
  $("#student-password").hidden = true;
  $("#student-profile").hidden = false;
  $("#student-title").textContent =
    profile.name + " / " + profile.student_number;
  $("#student-base-url").textContent = profile.base_url;
  const container = $("#student-memberships");
  container.replaceChildren();
  for (const member of profile.memberships) {
    const panel = el("section", undefined, "panel student-membership");
    panel.append(
      el("h2", member.team),
      el("p", "模型：" + member.models.join("、")),
      el("p", "本月已用：" + money(member.spent) + "（已结算）"),
    );
    if (member.key_expires_at)
      panel.append(
        el(
          "p",
          "接入到期：" +
            new Date(member.key_expires_at).toLocaleString("zh-CN"),
        ),
      );
    const active = member.keys.filter(
      (k) => k.active && (!k.expires_at || new Date(k.expires_at) > new Date()),
    );
    panel.append(
      el(
        "p",
        active.length
          ? "有效 Key：" + active.map((k) => k.prefix + "…").join("、")
          : "尚未领取有效 Key",
        "section-note",
      ),
    );
    const button = el(
      "button",
      active.length ? "轮换 Key（旧 Key 将失效）" : "领取个人 Key",
    );
    button.disabled = !member.active;
    button.dataset.membership = member.id;
    button.addEventListener("click", async () => {
      if (active.length && !(await confirmRotation())) return;
      button.disabled = true;
      notice("");
      try {
        button.dataset.operation ||= operationId();
        const result = await api("/keys", "POST", {
          operation_id: button.dataset.operation,
          membership_id: member.id,
          rotate: active.length > 0,
        });
        delete button.dataset.operation;
        if (result.key) {
          $("#student-key-value").textContent = result.key;
          $("#student-key-base").textContent = "API 地址：" + result.base_url;
          $("#student-key-dialog").showModal();
        } else {
          notice("此次操作已处理，完整 Key 无法再次读取；若未保存，请轮换。");
        }
        await load();
      } catch (e) {
        notice(e.message);
      } finally {
        button.disabled = !member.active;
      }
    });
    panel.append(button);
    container.append(panel);
  }
  if (!profile.memberships.length)
    container.append(el("p", "尚未分配项目组，请联系管理员。"));
  StudentGuide.render(profile);
}
$("#student-login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  notice("");
  try {
    const credentials = Object.fromEntries(new FormData(e.target));
    const result = await api("/login", "POST", credentials);
    session(result);
    // Keep the verified initial password only in this page's memory until changed.
    firstLoginPassword = result.must_change_password
      ? credentials.password
      : null;
    credentials.password = "";
    e.target.reset();
    maskPasswords(e.target);
    await load();
  } catch (err) {
    notice(err.message);
  } finally {
    b.disabled = false;
  }
});
$("#student-password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  if (data.new_password !== data.confirmation) {
    notice("两次输入的新密码不一致。");
    return;
  }
  delete data.confirmation;
  // Ignore password-manager overwrites during the initial change: use the
  // exact password verified by the immediately preceding login.
  if (profile?.must_change_password && firstLoginPassword !== null) {
    data.current_password = firstLoginPassword;
    e.target.elements.current_password.value = firstLoginPassword;
  }
  const b = e.submitter;
  b.disabled = true;
  notice("");
  try {
    session(await api("/password", "POST", data));
    firstLoginPassword = null;
    clearPasswordForm();
    await load();
    notice("密码已修改，请妥善保管。");
  } catch (err) {
    notice(err.message);
  } finally {
    b.disabled = false;
  }
});
$("#student-logout").addEventListener("click", async () => {
  try {
    await api("/logout", "POST", {});
  } catch (e) {
    notice(e.message);
  } finally {
    loginScreen();
  }
});
$("#change-password").addEventListener("click", passwordScreen);
$("#password-cancel").addEventListener("click", () =>
  load().catch((e) => notice(e.message)),
);
$("#student-key-close").addEventListener("click", clearKey);
$("#student-copy-base").addEventListener("click", (e) =>
  StudentGuide.copy(profile?.base_url || "", e.currentTarget),
);
$("#student-key-copy").addEventListener("click", (e) =>
  StudentGuide.copy($("#student-key-value").textContent, e.currentTarget),
);
$("#student-key-guide").addEventListener("click", () => {
  clearKey();
  $("#student-guide").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#guide-title").focus({ preventScroll: true });
});
$("#student-key-dialog").addEventListener("cancel", clearKey);
api("/config")
  .then((c) => {
    $("#initial-hint").textContent = c.demo
      ? "模拟测试：若管理员选择学号作为初始密码，初次密码即学号。"
      : "初始密码由管理员单独提供。";
    if (c.support_email) {
      const a = el("a", "联系管理员重置密码");
      a.href = "mailto:" + encodeURIComponent(c.support_email);
      $("#recovery-contact").replaceChildren(
        "忘记密码？",
        a,
        "。管理员核实身份后重置。",
      );
    }
  })
  .catch(() => {});
load().catch((e) => {
  if (profile) notice(e.message);
});
