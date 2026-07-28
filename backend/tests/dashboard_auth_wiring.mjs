import fs from "node:fs";
import vm from "node:vm";

const appPath = process.argv[2];
if (!appPath) throw new Error("usage: node dashboard_auth_wiring.mjs <app.js|->");
const source = appPath === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(appPath, "utf8");

const TOKEN = "tok-secret-a1b2c3";

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
    this._text = "";
  }

  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
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
    this.attributes[name] = String(value);
  }

  addEventListener() {}
}

function domSurfaces(node) {
  return [
    node._text,
    node.className,
    String(node.value ?? ""),
    ...Object.values(node.attributes),
    ...Object.values(node.dataset),
    ...node.children.flatMap(domSurfaces),
  ].map(String);
}

function makeStorage(seed) {
  const data = new Map(Object.entries(seed));
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
}

const hostileStorage = {
  data: new Map(),
  getItem() {
    throw new Error("SecurityError: storage is blocked for opaque origins");
  },
  setItem() {
    throw new Error("SecurityError: storage is blocked for opaque origins");
  },
  removeItem() {
    throw new Error("SecurityError: storage is blocked for opaque origins");
  },
};

// Boots the REAL frontend/app.js in a sandbox: fires the DOMContentLoaded handler
// app.js registers, with only the three non-auth init steps stubbed out so the
// assertions isolate auth wiring. bootstrapAuth/fetchJson stay untouched.
function boot({hash = "", storage = null, history = null, location = null, responses = []} = {}) {
  const nodes = new Map([["auth-status", new FakeNode("span")]]);
  const logs = [];
  const fetches = [];
  const sessionStorage = storage ?? makeStorage({});
  const historyFake = history ?? {calls: [], replaceState(_state, _title, url) { this.calls.push(url); }};
  const locationFake = location ?? {pathname: "/dashboard", search: "?x=1", hash};
  const record = (...args) => logs.push(args.map(String).join(" "));
  let domReady = null;

  const document = {
    addEventListener(type, handler) {
      if (type === "DOMContentLoaded") domReady = handler;
    },
    createElement: (tag) => new FakeNode(tag),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new FakeNode("div"));
      return nodes.get(id);
    },
    querySelectorAll: () => [],
  };

  const context = {
    console: {log: record, info: record, warn: record, error: record, debug: record},
    document,
    window: {},
    Node: FakeNode,
    HTMLElement: FakeNode,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    sessionStorage,
    location: locationFake,
    history: historyFake,
    fetch: async (url, options) => {
      fetches.push({url, options});
      const next = responses.shift() ?? {ok: true, status: 200, statusText: "OK"};
      return {...next, json: async () => ({})};
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context, {filename: appPath});

  const initSteps = [];
  for (const step of ["renderExamples", "bindActions", "refreshAll"]) {
    context[step] = () => initSteps.push(step);
  }

  check(typeof domReady === "function", "app.js never registered a DOMContentLoaded handler");
  let initError = null;
  try {
    domReady();
  } catch (error) {
    initError = error;
  }

  return {
    context,
    initError,
    initSteps,
    logs,
    fetches,
    sessionStorage,
    history: historyFake,
    location: locationFake,
    authNode: nodes.get("auth-status"),
    authToken: () => vm.runInContext("state.authToken", context),
    leakSurfaces: () => [
      ...[...nodes.values()].flatMap(domSurfaces),
      ...fetches.map((call) => String(call.url)),
      ...historyFake.calls.map(String),
      ...logs,
    ],
  };
}

function assertHealthyInit(env, label) {
  check(env.initError === null, `${label}: bootstrapAuth aborted dashboard init: ${env.initError && env.initError.stack}`);
  check(
    env.initSteps.join(",") === "renderExamples,bindActions,refreshAll",
    `${label}: dashboard init steps did not all run, got [${env.initSteps.join(",")}]`,
  );
}

function assertNoTokenLeak(env, label) {
  const leaked = env.leakSurfaces().filter((surface) => surface.includes(TOKEN));
  check(leaked.length === 0, `${label}: token leaked to ${leaked.length} surface(s): ${JSON.stringify(leaked)}`);
}

function authHeaderOf(call) {
  const headers = call.options?.headers ?? {};
  const key = Object.keys(headers).find((name) => name.toLowerCase() === "authorization");
  return key ? headers[key] : null;
}

// --- (a) DOMContentLoaded really invokes bootstrapAuth, (c) no token leak ------
{
  const env = boot({hash: `#access_token=${TOKEN}&token_type=bearer&expires_in=3600`});
  assertHealthyInit(env, "fragment");
  check(env.authToken() === TOKEN, `fragment: bootstrapAuth was not invoked by DOMContentLoaded (state.authToken=${env.authToken()})`);
  check(env.authNode.textContent === "已登录", `fragment: auth status not rendered, got "${env.authNode.textContent}"`);
  check(env.authNode.dataset.authenticated === "true", "fragment: data-authenticated was not set to true");
  check(env.sessionStorage.getItem("trustrag_token") === TOKEN, "fragment: token was not persisted to sessionStorage");
  check(env.history.calls.join("|") === "/dashboard?x=1", `fragment: URL fragment not stripped, replaceState=${env.history.calls.join("|")}`);

  await env.context.fetchJson("/v1/query", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  const call = env.fetches.at(-1);
  check(authHeaderOf(call) === `Bearer ${TOKEN}`, `fragment: wrong Authorization header ${JSON.stringify(call.options.headers)}`);
  check(call.options.headers["Content-Type"] === "application/json", "fragment: caller headers were dropped by the merge");
  check(call.options.method === "POST", "fragment: caller method was dropped by the merge");
  assertNoTokenLeak(env, "fragment");
}

// --- (b) anonymous emits no Authorization key at all ---------------------------
{
  const env = boot({});
  assertHealthyInit(env, "anonymous");
  check(env.authToken() === null, `anonymous: expected no token, got ${env.authToken()}`);
  check(env.authNode.textContent === "未登录", `anonymous: rendered a logged-in state ("${env.authNode.textContent}") while unauthenticated`);
  check(env.authNode.dataset.authenticated === "false", "anonymous: data-authenticated was not set to false");

  await env.context.fetchJson("/healthz");
  const headers = env.fetches.at(-1).options.headers;
  const authKeys = Object.keys(headers).filter((name) => name.toLowerCase() === "authorization");
  check(authKeys.length === 0, `anonymous: request carried an Authorization key ${JSON.stringify(headers)}`);
}

// --- stored token is picked up, still without leaking --------------------------
{
  const env = boot({storage: makeStorage({trustrag_token: TOKEN})});
  assertHealthyInit(env, "stored");
  check(env.authToken() === TOKEN, "stored: sessionStorage token was not picked up");
  await env.context.fetchJson("/v1/documents");
  check(authHeaderOf(env.fetches.at(-1)) === `Bearer ${TOKEN}`, "stored: Authorization header missing");
  assertNoTokenLeak(env, "stored");
}

// --- a stale token is dropped on 401 instead of faking a session ---------------
{
  const env = boot({
    storage: makeStorage({trustrag_token: TOKEN}),
    responses: [{ok: false, status: 401, statusText: "Unauthorized"}],
  });
  assertHealthyInit(env, "stale");

  let thrown = null;
  try {
    await env.context.fetchJson("/v1/documents");
  } catch (error) {
    thrown = error;
  }
  check(thrown !== null, "stale: fetchJson swallowed the 401 instead of throwing");
  check(thrown.message === "401 Unauthorized", `stale: throw message changed, got "${thrown.message}"`);
  check(env.authToken() === null, "stale: rejected token was kept in memory");
  check(env.sessionStorage.getItem("trustrag_token") === null, "stale: rejected token survives in sessionStorage across reloads");
  check(env.authNode.textContent === "未登录", `stale: still renders "${env.authNode.textContent}" after the server rejected the token`);

  await env.context.fetchJson("/healthz");
  check(authHeaderOf(env.fetches.at(-1)) === null, "stale: rejected token was still sent on the next request");
}

// --- opaque origin (sandboxed iframe / file://) must not kill the dashboard ----
{
  const hostileHistory = {
    calls: [],
    replaceState() {
      throw new Error("SecurityError: history.replaceState is blocked for opaque origins");
    },
  };
  const env = boot({
    hash: `#access_token=${TOKEN}&token_type=bearer`,
    storage: hostileStorage,
    history: hostileHistory,
  });
  assertHealthyInit(env, "opaque-origin-fragment");
  check(env.authToken() === TOKEN, "opaque-origin-fragment: a blocked address-bar cleanup must not cost the captured session");
  check(env.authNode.textContent === "已登录", `opaque-origin-fragment: auth status rendered "${env.authNode.textContent}"`);
  assertNoTokenLeak(env, "opaque-origin-fragment");

  const anon = boot({storage: hostileStorage, history: hostileHistory});
  assertHealthyInit(anon, "opaque-origin-anonymous");
  check(anon.authToken() === null, "opaque-origin-anonymous: expected no token");
  check(anon.authNode.textContent === "未登录", `opaque-origin-anonymous: rendered "${anon.authNode.textContent}"`);

  const blindLocation = {
    pathname: "/dashboard",
    search: "?x=1",
    get hash() {
      throw new Error("SecurityError: location is blocked for opaque origins");
    },
  };
  const blind = boot({storage: hostileStorage, history: hostileHistory, location: blindLocation});
  assertHealthyInit(blind, "opaque-origin-location");
  check(blind.authNode.textContent === "未登录", `opaque-origin-location: rendered "${blind.authNode.textContent}"`);
}

console.log("dashboard-auth-wiring: OK");
