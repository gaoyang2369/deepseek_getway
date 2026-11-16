import { $, node, session, api, showError, notice } from "./common.js";
import { teamsView, membersView } from "./people.js";
import {
  overviewView,
  budgetsView,
  requestsView,
  rejectionsView,
  eventsView,
  auditView,
} from "./reports.js";
import { pricesView } from "./prices.js";
import { studentAdminView } from "./students.js";
let view = "overview",
  loadVersion = 0;
const names = {
  students: "学生账号",
  overview: "使用概览",
  teams: "项目组",
  members: "人员与密钥",
  budgets: "预算账本",
  requests: "请求记录",
  prices: "模型与计价",
  events: "告警与待办",
  rejections: "拒绝记录",
  audit: "管理审计",
};
const views = {
  overview: overviewView,
  teams: teamsView,
  members: membersView,
  budgets: budgetsView,
  requests: requestsView,
  rejections: rejectionsView,
  prices: pricesView,
  events: eventsView,
  audit: auditView,
  students: studentAdminView,
};
async function load() {
  const loading = ++loadVersion,
    currentView = view;
  notice("");
  const isRecord = ["requests", "rejections", "events", "audit"].includes(view);
  $("#view-title").textContent = isRecord
    ? "记录与追溯 · " + names[view]
    : names[view];
  $("#record-tabs").hidden = !isRecord;
  document
    .querySelectorAll("nav button")
    .forEach((b) =>
      b.classList.toggle(
        "selected",
        b.dataset.view === view || (b.dataset.view === "records" && isRecord),
      ),
    );
  const c = $("#content");
  c.replaceChildren(node("p", "加载中…", "muted"));
  const cache = {
    navigate: async (next) => {
      view = next;
      await load();
    },
  };
  [cache.teamHistory, cache.memberHistory, cache.config] = await Promise.all([
    api("/teams?include_deleted=true"),
    ["members", "students"].includes(currentView)
      ? Promise.resolve([])
      : api("/memberships?include_deleted=true"),
    api("/config"),
  ]);
  cache.teams = cache.teamHistory.filter((t) => !t.deleted_at);
  cache.members = cache.memberHistory.filter((m) => !m.deleted_at);
  const nodes = await views[currentView](cache, load);
  if (loading === loadVersion) c.replaceChildren(...nodes);
}
$("#login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = e.submitter;
  b.disabled = true;
  try {
    const result = await api(
      "/login",
      "POST",
      Object.fromEntries(new FormData(e.target)),
    );
    session.csrf = result.csrf_token;
    sessionStorage.setItem("lab_csrf", session.csrf);
    e.target.password.value = "";
    $("#login-screen").hidden = true;
    $("#console").hidden = false;
    await load();
  } catch (err) {
    showError(err);
  } finally {
    b.disabled = false;
  }
});
function navigate(e) {
  if (e.target.dataset.view) {
    view =
      e.target.dataset.view === "records" ? "requests" : e.target.dataset.view;
    load().catch((e) => showError(e));
  }
}
$("#nav").addEventListener("click", navigate);
$("#record-tabs").addEventListener("click", navigate);
$("#refresh").addEventListener("click", () =>
  load().catch((e) => showError(e)),
);
$("#logout").addEventListener("click", async () => {
  try {
    await api("/logout", "POST", {});
  } finally {
    sessionStorage.removeItem("lab_csrf");
    location.reload();
  }
});
if (session.csrf) {
  $("#login-screen").hidden = true;
  $("#console").hidden = false;
  load().catch((e) => showError(e));
}
