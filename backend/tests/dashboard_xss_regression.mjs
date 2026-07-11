import fs from "node:fs";
import vm from "node:vm";

const appPath = process.argv[2];
if (!appPath) throw new Error("usage: node dashboard_xss_regression.mjs <app.js|->");
const source = appPath === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(appPath, "utf8");

class FakeNode {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
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

  insertAdjacentHTML() {
    throw new Error("unsafe insertAdjacentHTML sink used");
  }

  setAttribute(name, value) {
    if (/^on/i.test(name) || name.toLowerCase() === "srcdoc") {
      throw new Error(`unsafe attribute sink used: ${name}`);
    }
    this.attributes[name] = String(value);
  }
}

const ids = [
  "answer-text",
  "answer-badges",
  "answer-metadata",
  "safety-json",
  "temporal-json",
  "conflict-json",
  "citations-list",
  "support-list",
  "counter-list",
];
const nodes = new Map(ids.map((id) => [id, new FakeNode("div")]));
const document = {
  addEventListener() {},
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_namespace, tag) => new FakeNode(tag),
  getElementById: (id) => nodes.get(id) ?? null,
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
  console,
  document,
  window: {},
  Node: FakeNode,
  HTMLElement: FakeNode,
  DOMParser,
  URLSearchParams,
  setTimeout,
  clearTimeout,
};
vm.createContext(context);
vm.runInContext(source, context, {filename: appPath});

const payloads = [
  '<img src=x onerror="window.__trustragXss = true">',
  '<svg onload="window.__trustragXss = true"></svg>',
];

for (const payload of payloads) {
  context.renderQuery({
    answer: payload,
    question_type: "bookkeeping_sop",
    confidence: 0.9,
    needs_human_review: false,
    safety_analysis: {},
    temporal_analysis: {},
    conflict_analysis: {},
    citations: [{title: payload, doc_id: "xss-doc", snippet: payload}],
    support_evidence: [{title: payload, score: 0.9, content: payload}],
    counter_evidence: [],
    human_review: {required: false, status: null, review_queue_id: null, reasons: []},
    errors: [payload],
  });

  if (context.window.__trustragXss) throw new Error("event attribute executed");
  if (!nodes.get("answer-text").textContent.includes(payload)) {
    throw new Error("answer payload was not preserved as text");
  }
  for (const id of ["citations-list", "support-list"]) {
    if (!nodes.get(id).textContent.includes(payload)) {
      throw new Error(`${id} payload was not preserved as text`);
    }
  }
}

const renderedTags = [...nodes.values()]
  .flatMap(function collect(node) {
    return [node.tagName, ...node.children.flatMap(collect)];
  });
if (renderedTags.includes("IMG") || renderedTags.includes("SVG")) {
  throw new Error(`payload created executable elements: ${renderedTags.join(",")}`);
}

console.log("dashboard-xss-regression: OK");
