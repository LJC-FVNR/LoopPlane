#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");

function fail(message, details) {
  process.stderr.write(JSON.stringify({
    ok: false,
    status: "failed",
    error: message,
    details: details || null
  }, null, 2) + "\n");
  process.exit(1);
}

function assertContains(name, value, expected) {
  if (!String(value || "").includes(expected)) {
    fail(name + " did not contain " + JSON.stringify(expected), {value: String(value || "").slice(0, 1000)});
  }
}

function assertNotContains(name, value, forbidden) {
  if (String(value || "").includes(forbidden)) {
    fail(name + " unexpectedly contained " + JSON.stringify(forbidden), {value: String(value || "").slice(0, 1000)});
  }
}

function extractPayload(html) {
  const match = html.match(/<script id="loopplane-read-models" type="application\/json">([\s\S]*?)<\/script>/);
  if (!match) {
    fail("dashboard payload script tag was not found");
  }
  try {
    const payload = JSON.parse(match[1]);
    if (payload && payload.payload_encoding === "gzip+base64") {
      return JSON.parse(zlib.gunzipSync(Buffer.from(String(payload.payload_compressed || ""), "base64")).toString("utf8"));
    }
    return payload;
  } catch (error) {
    fail("dashboard payload JSON could not be parsed", {message: error.message});
  }
}

function decodeHtml(value) {
  return String(value || "")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

class ClassList {
  constructor() {
    this.values = new Set();
  }

  add(name) {
    this.values.add(String(name));
  }

  remove(name) {
    this.values.delete(String(name));
  }

  contains(name) {
    return this.values.has(String(name));
  }
}

class Element {
  constructor(ownerDocument, tagName, attributes) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName || "div";
    this.attributes = Object.assign({}, attributes || {});
    this.id = this.attributes.id || "";
    this.classList = new ClassList();
    String(this.attributes.class || "").split(/\s+/).filter(Boolean).forEach((name) => this.classList.add(name));
    this.listeners = {};
    this.children = [];
    this._innerHTML = "";
    this.textContent = "";
    this.value = this.attributes.value || "";
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(value) {
    this._innerHTML = String(value || "");
    if (this.id === "graph-panel-body") {
      this.ownerDocument.seedGraphNodesFromHtml(this._innerHTML);
    }
  }

  get disabled() {
    return this.attributes.disabled === true || this.attributes.disabled === "disabled";
  }

  set disabled(value) {
    if (value) {
      this.attributes.disabled = "disabled";
    } else {
      delete this.attributes.disabled;
    }
  }

  getAttribute(name) {
    const key = String(name);
    return Object.prototype.hasOwnProperty.call(this.attributes, key) ? String(this.attributes[key]) : null;
  }

  setAttribute(name, value) {
    const key = String(name);
    this.attributes[key] = String(value);
    if (key === "id") {
      this.id = String(value);
    }
    if (key === "class") {
      this.classList = new ClassList();
      String(value).split(/\s+/).filter(Boolean).forEach((item) => this.classList.add(item));
    }
  }

  removeAttribute(name) {
    delete this.attributes[String(name)];
  }

  addEventListener(type, handler) {
    const key = String(type);
    if (!this.listeners[key]) {
      this.listeners[key] = [];
    }
    this.listeners[key].push(handler);
  }

  dispatchEvent(event) {
    const type = typeof event === "string" ? event : event.type;
    const payload = typeof event === "string" ? {type: event} : event;
    (this.listeners[type] || []).forEach((handler) => handler(payload));
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }

  reset() {
    this.value = "";
  }
}

class Document {
  constructor(payload) {
    this.payload = payload;
    this.elements = new Map();
    this.domListeners = {};
    this.graphNodes = [];
    this.workflowRows = [];
    this.metricStrong = new Map();
    [
      "loopplane-read-models",
      "workflow-selector",
      "workflow-selector-status",
      "dashboard-workflow-title",
      "approval-alert-shell",
      "freshness-banner-shell",
      "plan-panel-body",
      "graph-panel-body",
      "node-detail-body",
      "activity-feed-body",
      "vc-panel-body",
      "approval-panel-body",
      "runner-panel-body",
      "inspector-console-body",
      "metrics-panel-body",
      "dashboard-refresh-button",
      "dashboard-refresh-status",
      "dashboard-last-refreshed",
      "file-preview-modal",
      "file-preview-title",
      "file-preview-path",
      "file-preview-status",
      "file-preview-content"
    ].forEach((id) => this.ensureElement(id));
    this.getElementById("loopplane-read-models").textContent = JSON.stringify(payload);
    this.getElementById("workflow-selector").value = payload.workflow_id || "";
    this.detailFileLinks = [
      new Element(this, "button", {
        class: "detail-file-link",
        "data-detail-file-link": "true",
        "data-detail-title": "Markdown Link Fixture",
        "data-detail-path": ".loopplane/results/T001/final.md",
        "data-detail-render": "markdown",
        "data-detail-content": "- [Report](" + String(payload.project_root || "") + "/sales_analysis/REPORT.md)"
      })
    ];
    this.seedGraphNodesFromPayload(payload);
    this.seedWorkflowRows(payload);
  }

  ensureElement(id) {
    if (!this.elements.has(id)) {
      this.elements.set(id, new Element(this, "div", {id: id}));
    }
    return this.elements.get(id);
  }

  getElementById(id) {
    return this.elements.get(String(id)) || null;
  }

  addEventListener(type, handler) {
    const key = String(type);
    if (!this.domListeners[key]) {
      this.domListeners[key] = [];
    }
    this.domListeners[key].push(handler);
  }

  dispatchEvent(type) {
    (this.domListeners[type] || []).forEach((handler) => handler({type: type}));
  }

  querySelector(selector) {
    const metric = String(selector).match(/^\[data-metric="([^"]+)"\] strong$/);
    if (metric) {
      const key = metric[1];
      if (!this.metricStrong.has(key)) {
        this.metricStrong.set(key, new Element(this, "strong", {}));
      }
      return this.metricStrong.get(key);
    }
    if (selector === ".graph-node.is-selected") {
      return this.graphNodes.find((node) => node.classList.contains("is-selected")) || null;
    }
    return null;
  }

  querySelectorAll(selector) {
    if (selector === ".graph-node") {
      return this.graphNodes;
    }
    if (selector === ".workflow-history-row") {
      return this.workflowRows;
    }
    if (selector === ".approval-response-form") {
      return [];
    }
    if (selector === "[data-detail-file-link]" || selector === "[data-detail-title]") {
      return this.detailFileLinks;
    }
    if (selector === "[data-log-stream-title]" || selector === "[data-file-preview-close]") {
      return [];
    }
    return [];
  }

  seedGraphNodesFromPayload(payload) {
    const graph = payload && payload.read_models && payload.read_models["workflow_graph.json"] || {};
    const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    this.graphNodes = nodes.map((node) => this.graphButton(node.node_id, node.status));
  }

  seedGraphNodesFromHtml(html) {
    const nodes = [];
    const pattern = /<button\b[^>]*class="[^"]*\bgraph-node\b[^"]*"[^>]*>/g;
    let match;
    while ((match = pattern.exec(html)) !== null) {
      const tag = match[0];
      const idMatch = tag.match(/data-node-id="([^"]+)"/);
      const statusMatch = tag.match(/data-status="([^"]+)"/);
      if (idMatch) {
        nodes.push(this.graphButton(decodeHtml(idMatch[1]), statusMatch ? decodeHtml(statusMatch[1]) : ""));
      }
    }
    this.graphNodes = nodes;
  }

  graphButton(nodeId, status) {
    return new Element(this, "button", {
      class: "graph-node",
      "data-node-id": nodeId || "",
      "data-status": status || ""
    });
  }

  seedWorkflowRows(payload) {
    const workflows = Array.isArray(payload.workflows) ? payload.workflows : [];
    this.workflowRows = workflows.map((workflow) => new Element(this, "li", {
      class: "workflow-history-row",
      "data-workflow-id": workflow.workflow_id || ""
    }));
  }
}

function run() {
  const indexPath = process.argv[2];
  const scriptPath = process.argv[3] || (indexPath ? path.join(path.dirname(indexPath), "static_dashboard.js") : "");
  if (!indexPath || !scriptPath) {
    fail("usage: node tests/dashboard_dom_smoke.js <index.html> <static_dashboard.js>");
  }
  const html = fs.readFileSync(indexPath, "utf8");
  const script = fs.readFileSync(scriptPath, "utf8");
  const payload = extractPayload(html);
  const document = new Document(payload);
  const window = {
    document: document,
    location: {search: ""},
    fetch: undefined
  };
  const context = {
    console: console,
    document: document,
    window: window,
    URLSearchParams: URLSearchParams
  };
  context.globalThis = context;
  vm.runInNewContext(script, context, {filename: scriptPath});
  document.dispatchEvent("DOMContentLoaded");

  const selector = document.getElementById("workflow-selector");
  selector.value = payload.workflow_id;
  selector.dispatchEvent({type: "change"});
  const runNode = document.graphNodes.find((node) => {
    return String(node.getAttribute("data-node-id") || "").includes("run_fixture");
  });
  if (runNode) {
    runNode.dispatchEvent({type: "click", preventDefault: function () {}});
  }

  const checks = [];
  function checkContains(name, id, expected) {
    assertContains(name, document.getElementById(id).innerHTML || document.getElementById(id).textContent, expected);
    checks.push(name);
  }

  checkContains("plan panel", "plan-panel-body", "Produce result artifact");
  checkContains("plan toggle", "plan-panel-body", "data-plan-view=\"markdown\"");
  checkContains("graph panel", "graph-panel-body", "node_run_run_fixture");
  checkContains("graph overview", "graph-panel-body", "graph-overview");
  checkContains("graph mode", "graph-panel-body", "Graph Mode");
  checkContains("graph pipeline", "graph-panel-body", "graph-pipeline-scroll");
  checkContains("node details prompt", "node-detail-body", "Worker Prompt");
  checkContains("node details final response title", "node-detail-body", "Final Response");
  checkContains("node details final", "node-detail-body", "Worker Final Output");
  checkContains("node details diff", "node-detail-body", "src/app.py");
  assertNotContains("node details", document.getElementById("node-detail-body").innerHTML, ".git/config");
  assertNotContains("node details", document.getElementById("node-detail-body").innerHTML, "super-secret-token");
  const approvalControls = payload.approval_controls && typeof payload.approval_controls === "object" ? payload.approval_controls : {};
  const pendingApprovals = Array.isArray(approvalControls.pending) ? approvalControls.pending : [];
  const expectedApproval = pendingApprovals.length ? pendingApprovals[0].approval_id : "";
  if (!expectedApproval) {
    fail("payload did not include a pending approval for the DOM smoke");
  }
  checkContains("approval alert", "approval-alert-shell", "pending approval");
  checkContains("approval panel", "approval-panel-body", expectedApproval);
  checkContains("version control", "vc-panel-body", "Repo Dirty");
  assertNotContains("version control", document.getElementById("vc-panel-body").innerHTML, ".git/");
  const inspectorConsole = payload.inspector_console && typeof payload.inspector_console === "object" ? payload.inspector_console : {};
  const recentChanges = Array.isArray(inspectorConsole.recent_change_requests) ? inspectorConsole.recent_change_requests : [];
  const expectedChangeRequest = recentChanges.length ? recentChanges[recentChanges.length - 1].user_request : "";
  if (!expectedChangeRequest) {
    fail("payload did not include a recent change request for the DOM smoke");
  }
  checkContains("inspector console", "inspector-console-body", "Full Agent Inspector");
  checkContains("change request console", "inspector-console-body", expectedChangeRequest);
  checkContains("controls", "inspector-console-body", "Planning Controls");
  checkContains("execution controls", "inspector-console-body", "Execution Requests");
  checkContains("static readonly controls", "inspector-console-body", "Static dashboard is read-only");
  checkContains("stale freshness", "freshness-banner-shell", "Read Models May Be Stale");
  checkContains("rebuild affordance", "freshness-banner-shell", "read-model-rebuild-form");
  checkContains("runner panel", "runner-panel-body", "Trusted Local");
  checkContains("snapshot panel", "metrics-panel-body", "workflow_status.json");
  const refreshButton = document.getElementById("dashboard-refresh-button");
  if (!refreshButton.disabled) {
    fail("static dashboard refresh button was not disabled");
  }
  assertContains("refresh status", document.getElementById("dashboard-refresh-status").textContent, "Static snapshot");
  assertContains("last refreshed", document.getElementById("dashboard-last-refreshed").textContent, "Last refreshed:");
  checks.push("refresh controls");

  window.location.search = "?token=smoke-token";
  const livePayload = JSON.parse(JSON.stringify(payload));
  livePayload.server_mode = true;
  document.getElementById("loopplane-read-models").textContent = JSON.stringify(livePayload);
  document.dispatchEvent("DOMContentLoaded");
  document.detailFileLinks[0].dispatchEvent({type: "click", preventDefault: function () {}});
  const previewHtml = document.getElementById("file-preview-content").innerHTML;
  assertContains(
    "markdown preview file link",
    previewHtml,
    "/api/workflows/" + encodeURIComponent(payload.workflow_id) + "/files?token=smoke-token&amp;path=sales_analysis%2FREPORT.md"
  );
  assertNotContains("markdown preview link label", previewHtml, "noneReportnone");
  assertNotContains(
    "markdown preview absolute project path",
    previewHtml,
    String(payload.project_root || "") + "/sales_analysis/REPORT.md"
  );
  checks.push("markdown preview file links");
  window.location.search = "";
  document.getElementById("loopplane-read-models").textContent = JSON.stringify(payload);
  document.dispatchEvent("DOMContentLoaded");

  const archivedWorkflow = (Array.isArray(payload.workflows) ? payload.workflows : []).find((workflow) => {
    return workflow && (workflow.archived === true || workflow.read_only === true);
  });
  if (archivedWorkflow) {
    selector.value = archivedWorkflow.workflow_id;
    selector.dispatchEvent({type: "change"});
    checkContains("archived control workflow", "inspector-console-body", archivedWorkflow.workflow_id);
    checkContains("archived control disabled", "inspector-console-body", "disabled");
    checkContains("archived approval disabled", "approval-panel-body", "Static dashboard is read-only");
    checkContains("archived selector status", "workflow-selector-status", "Selection does not update current_workflow.json");
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    status: "passed",
    workflow_id: payload.workflow_id,
    archived_workflow_id: archivedWorkflow ? archivedWorkflow.workflow_id : null,
    checks: checks
  }, null, 2) + "\n");
}

run();
