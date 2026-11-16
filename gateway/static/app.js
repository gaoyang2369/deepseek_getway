import { $, node, session, api, showError, notice } from "./common.js";
import { teamsView, membersView } from "./people.js";
import {
  overviewView,
  budgetsView,
  requestsView,
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
  events: "异常事件",
  audit: "管理审计",
};
const views = {
  overview: overviewView,
  teams: teamsView,
  members: membersView,
  budgets: budgetsView,
  requests: requestsView,
  prices: pricesView,
  events: eventsView,
  audit: auditView,
  students: studentAdminView,
};
async function load() {
  const loading = ++loadVersion,
    currentView = view;
  notice("");
  $("#view-title").textContent = names[view];
  document
    .querySelectorAll("nav button")
    .forEach((b) => b.classList.toggle("selected", b.dataset.view === view));
  const c = $("#content");
  c.replaceChildren(node("p", "加载中…", "muted"));
  const cache = {};
  [cache.teamHistory, cache.memberHistory, cache.config] = await Promise.all([
    api("/teams?include_deleted=true"),
    api("/memberships?include_deleted=true"),
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
$("#nav").addEventListener("click", (e) => {
  if (e.target.dataset.view) {
    view = e.target.dataset.view;
    load().catch((e) => showError(e));
  }
});
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
