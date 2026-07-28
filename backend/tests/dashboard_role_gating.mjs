import fs from "node:fs";
import vm from "node:vm";

const appPath = process.argv[2];
if (!appPath) throw new Error("usage: node dashboard_role_gating.mjs <app.js|->");
const source = appPath === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(appPath, "utf8");

// A backend detail that must never reach the DOM: fetchJson only ever carries
// "<status> <statusText>", so an error body stays invisible to the user.
const LEAK = "Traceback (most recent call last): sqlalchemy.exc.IntegrityError";
const XSS = '<img src=x onerror="window.__trustragXss = true">';

function check(condition, message) {
  if (!condition) throw new Error(message);
}

class FakeNode {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this._text = "";
  }

  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set innerHTML(_value) {
    throw new Error("unsafe innerHTML sink used");
  }

  set outerHTML(_value) {
    throw new Error("unsafe outerHTML sink used");
  }

  set srcdoc(_value) {
    throw new Error("unsafe srcdoc sink used");
  }

  insertAdjacentHTML() {
    throw new Error("unsafe insertAdjacentHTML sink used");
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }

  replaceChildren(...children) {
    this._text = "";
    this.children = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    if (/^on/i.test(name) || name.toLowerCase() === "srcdoc") {
      throw new Error(`unsafe attribute sink used: ${name}`);
    }
    this.attributes[name] = String(value);
  }

  addEventListener() {}
}

function collectTags(node) {
  return [node.tagName, ...node.children.flatMap(collectTags)];
}

// Boots the REAL frontend/app.js with a URL-keyed fetch router. Nothing here
// supplies sessionStorage / location / history: the tenant console must work
// without them, exactly like the other DOM-less harnesses.
function boot({routes = {}} = {}) {
  const nodes = new Map();
  const fetches = [];

  const document = {
    addEventListener() {},
    createElement: (tag) => new FakeNode(tag),
    createElementNS: (_namespace, tag) => new FakeNode(tag),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new FakeNode("div"));
      return nodes.get(id);
    },
    querySelectorAll: () => [],
    write() {
      throw new Error("unsafe document.write sink used");
    },
    createRange() {
      return {
        createContextualFragment() {
          throw new Error("unsafe createContextualFragment sink used");
        },
      };
    },
  };

  class DOMParser {
    parseFromString() {
      throw new Error("unsafe DOMParser sink used");
    }
  }

  const context = {
    console: {log() {}, info() {}, warn() {}, error() {}, debug() {}},
    document,
    window: {},
    Node: FakeNode,
    HTMLElement: FakeNode,
    DOMParser,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    fetch: async (url, options = {}) => {
      const method = options.method || "GET";
      fetches.push({url, method, body: options.body});
      const reply = routes[`${method} ${url}`];
      if (!reply) {
        return {ok: false, status: 501, statusText: "Not Implemented", json: async () => ({})};
      }
      const status = reply.status ?? 200;
      return {
        ok: status < 400,
        status,
        statusText: reply.statusText ?? "OK",
        json: async () => reply.body ?? {},
      };
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context, {filename: appPath});

  // Mirrors `<section id="tenant-admin" ... hidden>` in index.html.
  const panel = document.getElementById("tenant-admin");
  panel.hidden = true;

  return {
    context,
    nodes,
    fetches,
    panel,
    node: (id) => document.getElementById(id),
    urls: () => fetches.map((call) => `${call.method} ${call.url}`),
    domText: () => [...nodes.values()].map((node) => node.textContent).join("\n"),
    tags: () => [...nodes.values()].flatMap(collectTags),
  };
}

function meRoute(roles) {
  return {body: {subject_id: "ops-1", tenant_id: "platform", roles}};
}

const TENANT_LIST = {
  body: {
    tenants: [
      {tenant_id: "alpha-firm", name: XSS, status: "active", created_at: "2026-07-27T00:00:00+00:00"},
    ],
  },
};

// --- platform_admin reveals the panel and lists tenants as text ---------------
{
  const env = boot({
    routes: {"GET /v1/me": meRoute(["platform_admin"]), "GET /v1/admin/tenants": TENANT_LIST},
  });

  await env.context.applyRoleGating();

  check(env.panel.hidden === false, "platform_admin: the tenant admin panel stayed hidden");
  check(
    env.urls().includes("GET /v1/admin/tenants"),
    `platform_admin: the tenant list was never fetched, got ${JSON.stringify(env.urls())}`,
  );
  const listed = env.node("tenant-list").textContent;
  check(listed.includes(XSS), `platform_admin: tenant name was not preserved as text, got "${listed}"`);
  check(listed.includes("alpha-firm"), "platform_admin: tenant_id was not rendered");
  check(!env.context.window.__trustragXss, "platform_admin: an event attribute executed");
  const tags = env.tags();
  check(
    !tags.includes("IMG") && !tags.includes("SVG"),
    `platform_admin: tenant payload created executable elements: ${tags.join(",")}`,
  );
}

// --- every other role keeps the panel hidden and never calls the admin API ----
for (const roles of [["viewer"], ["reviewer"], ["admin"], []]) {
  const env = boot({
    routes: {"GET /v1/me": meRoute(roles), "GET /v1/admin/tenants": TENANT_LIST},
  });

  await env.context.applyRoleGating();

  check(env.panel.hidden === true, `roles=${JSON.stringify(roles)}: the tenant admin panel was revealed`);
  check(
    !env.urls().includes("GET /v1/admin/tenants"),
    `roles=${JSON.stringify(roles)}: a non platform_admin called the tenant admin API`,
  );
}

// --- unauthenticated / rejected session keeps it hidden without throwing ------
for (const failure of [
  {status: 401, statusText: "Unauthorized"},
  {status: 403, statusText: "Forbidden"},
  {status: 501, statusText: "Not Implemented"},
]) {
  const env = boot({routes: {"GET /v1/me": {...failure, body: {detail: LEAK}}}});

  let thrown = null;
  try {
    await env.context.applyRoleGating();
  } catch (error) {
    thrown = error;
  }

  check(thrown === null, `me ${failure.status}: role gating threw and would abort dashboard init: ${thrown}`);
  check(env.panel.hidden === true, `me ${failure.status}: the tenant admin panel was revealed`);
  check(!env.domText().includes(LEAK), `me ${failure.status}: a backend detail reached the DOM`);
}

// --- create failures each surface a distinct, leak-free status ----------------
{
  const messages = new Map();
  const cases = [
    {status: 403, statusText: "Forbidden", expect: "平台管理员"},
    {status: 409, statusText: "Conflict", expect: "已存在"},
    {status: 400, statusText: "Bad Request", expect: "不能为空"},
    {status: 422, statusText: "Unprocessable Entity", expect: "不能为空"},
  ];
  for (const {status, statusText, expect} of cases) {
    const env = boot({
      routes: {
        "POST /v1/admin/tenants": {status, statusText, body: {detail: LEAK}},
        "GET /v1/admin/tenants": TENANT_LIST,
      },
    });
    env.node("new-tenant-id").value = "gamma";
    env.node("new-tenant-name").value = "Gamma";

    await env.context.createTenant();

    const text = env.node("tenant-admin-status").textContent;
    check(text.length > 0, `create ${status}: no user-visible status was rendered`);
    check(text.includes(expect), `create ${status}: status "${text}" does not explain the failure`);
    check(!text.includes(LEAK), `create ${status}: leaked a backend detail: "${text}"`);
    check(!text.includes(statusText), `create ${status}: echoed the raw status line: "${text}"`);
    check(!env.node("create-tenant").disabled, `create ${status}: the button stayed disabled`);
    messages.set(status, text);
  }
  check(messages.get(403) !== messages.get(409), "create: 403 and 409 render the same status");
  check(messages.get(409) !== messages.get(400), "create: 409 and 400 render the same status");
}

// --- a successful create refreshes the list through the same API --------------
{
  const env = boot({
    routes: {
      "POST /v1/admin/tenants": {
        status: 201,
        statusText: "Created",
        body: {tenant_id: "gamma", name: "Gamma", status: "active", created_at: "2026-07-27T00:00:00+00:00"},
      },
      "GET /v1/admin/tenants": TENANT_LIST,
    },
  });
  env.node("new-tenant-id").value = "  gamma  ";
  env.node("new-tenant-name").value = "Gamma";

  await env.context.createTenant();

  const posted = env.fetches.find((call) => call.method === "POST");
  check(posted !== undefined, "create ok: no POST was issued");
  check(
    JSON.parse(posted.body).tenant_id === "  gamma  " && JSON.parse(posted.body).name === "Gamma",
    `create ok: the typed values were not posted, got ${posted.body}`,
  );
  const order = env.urls();
  check(
    order.indexOf("GET /v1/admin/tenants") > order.indexOf("POST /v1/admin/tenants"),
    `create ok: the list was not refreshed after the create, got ${JSON.stringify(order)}`,
  );
  check(env.node("tenant-list").textContent.includes("alpha-firm"), "create ok: the list was not re-rendered");
  check(env.node("new-tenant-id").value === "", "create ok: the tenant_id input was not cleared");
  check(env.node("new-tenant-name").value === "", "create ok: the name input was not cleared");
}

// --- the [data-refresh="tenants"] control dispatches to the tenant list -------
{
  const env = boot({routes: {"GET /v1/admin/tenants": TENANT_LIST}});

  await env.context.refreshPanel("tenants");

  check(
    env.urls().includes("GET /v1/admin/tenants"),
    `refreshPanel("tenants"): no tenant list request, got ${JSON.stringify(env.urls())}`,
  );
}

// --- a broken tenant list degrades to a message, never to a thrown init -------
{
  const env = boot({
    routes: {
      "GET /v1/me": meRoute(["platform_admin"]),
      "GET /v1/admin/tenants": {status: 404, statusText: "Not Found", body: {detail: LEAK}},
    },
  });

  let thrown = null;
  try {
    await env.context.applyRoleGating();
  } catch (error) {
    thrown = error;
  }

  check(thrown === null, `list 404: role gating threw: ${thrown}`);
  check(env.node("tenant-admin-summary").textContent.length > 0, "list 404: no summary was rendered");
  check(!env.domText().includes(LEAK), "list 404: a backend detail reached the DOM");
}

console.log("dashboard-role-gating: OK");
