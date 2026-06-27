(function () {
  "use strict";

  var REFRESH_INTERVAL_MS = 30000;
  var FILE_PREVIEW_MAX_LINES = 500;
  var activePayload = null;
  var dashboardPayloadCache = {};
  var dashboardEtagCache = {};
  var refreshTimer = null;
  var refreshInFlight = false;
  var initialLiveRefreshStarted = false;
  var logStreamTimer = null;
  var logStreamInFlight = false;
  var workflowHistoryShowAll = false;

  function readPayloadNode() {
    var node = document.getElementById("loopplane-read-models");
    if (!node || !node.textContent) {
      return null;
    }
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  function decodeBase64Bytes(value) {
    var binary = window.atob(String(value || ""));
    var bytes = new Uint8Array(binary.length);
    for (var index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  }

  function decompressPayload(wrapper) {
    if (!window.DecompressionStream || !window.Blob || !window.Response) {
      return Promise.reject(new Error("This browser cannot decompress the embedded dashboard snapshot."));
    }
    var stream = new window.Blob([decodeBase64Bytes(wrapper.payload_compressed)]).stream().pipeThrough(new window.DecompressionStream("gzip"));
    return new window.Response(stream).text().then(function (value) {
      return JSON.parse(value);
    });
  }

  function readPayload() {
    var payload = readPayloadNode();
    if (!payload) {
      return Promise.resolve(null);
    }
    if (payload.payload_encoding === "gzip+base64") {
      return decompressPayload(payload);
    }
    return Promise.resolve(payload);
  }

  function setDashboardLoading(title, message) {
    var overlay = document.getElementById("dashboard-loading-overlay");
    var titleNode = document.getElementById("dashboard-loading-title");
    var messageNode = document.getElementById("dashboard-loading-message");
    if (!overlay) {
      return;
    }
    overlay.removeAttribute("hidden");
    overlay.setAttribute("aria-busy", "true");
    if (titleNode) {
      titleNode.textContent = title || "Loading dashboard";
    }
    if (messageNode) {
      messageNode.textContent = message || "Preparing the workflow snapshot and rendering the dashboard.";
    }
  }

  function hideDashboardLoading() {
    var overlay = document.getElementById("dashboard-loading-overlay");
    if (!overlay) {
      return;
    }
    overlay.setAttribute("aria-busy", "false");
    overlay.setAttribute("hidden", "hidden");
  }

  function mountDashboardPayload(payload) {
    if (!payload) {
      return;
    }
    applyPayload(payload);
    loadRequestedWorkflowFromLocation(activePayload || payload);
  }

  function text(value) {
    if (value === null || value === undefined || value === "") {
      return "none";
    }
    if (typeof value === "boolean") {
      return value ? "true" : "false";
    }
    return String(value);
  }

  function cleanText(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value).trim();
  }

  function cleanTitle(value) {
    return cleanText(value).replace(/\s+/g, " ");
  }

  function workflowDisplayTitle(payload) {
    var workflowId = cleanTitle(payload && payload.workflow_id);
    var planIndex = readModel(payload, "plan_index.json");
    var workflowStatus = readModel(payload, "workflow_status.json");
    var workflow = payload && payload.workflow && typeof payload.workflow === "object" ? payload.workflow : {};
    var candidates = [
      payload && payload.workflow_title,
      planIndex.workflow_title,
      workflowStatus.workflow_title,
      workflow.workflow_title,
      workflow.name
    ];
    for (var index = 0; index < candidates.length; index += 1) {
      var title = cleanTitle(candidates[index]);
      if (title && title !== workflowId) {
        return title.slice(0, 120);
      }
    }
    return "Workflow";
  }

  function statusValue(value) {
    return text(value).trim().toLowerCase().replace(/\s+/g, "_").replace(/-/g, "_");
  }

  function statusTier(value) {
    var status = statusValue(value);
    if (!status || status === "none") {
      return "muted";
    }
    if (["fail", "failed", "failure", "error", "blocked", "rejected", "invalid", "conflict", "unsafe", "objective_unresolved"].indexOf(status) !== -1) {
      return "danger";
    }
    if (["stale", "pending", "pending_review", "requested", "queued", "waiting", "wait", "warning", "needs_attention", "requires_attention", "needs_user_approval", "needs_expansion", "needs_verification", "partial", "review", "expired"].indexOf(status) !== -1) {
      return "warning";
    }
    if (["starting", "running", "active", "serving", "started", "resumed", "in_progress", "processing"].indexOf(status) !== -1) {
      return "active";
    }
    if (["pass", "ok", "current", "completed", "complete", "done", "available", "approved", "applied", "created", "recorded", "submitted", "answered", "change_request_created", "enabled", "ready", "rendered", "closed"].indexOf(status) !== -1) {
      return "success";
    }
    if (["disabled", "read_only", "read_only_imported", "archived", "archived_view", "superseded", "skipped", "static", "events"].indexOf(status) !== -1) {
      return "muted";
    }
    if (status.indexOf("fail") !== -1 || status.indexOf("error") !== -1 || status.indexOf("blocked") !== -1) {
      return "danger";
    }
    if (status.indexOf("stale") !== -1 || status.indexOf("pending") !== -1 || status.indexOf("wait") !== -1 || status.indexOf("attention") !== -1) {
      return "warning";
    }
    if (status.indexOf("running") !== -1 || status.indexOf("active") !== -1) {
      return "active";
    }
    if (status.indexOf("pass") !== -1 || status.indexOf("complete") !== -1 || status.indexOf("ok") === 0) {
      return "success";
    }
    return "info";
  }

  function escapeHtml(value) {
    return htmlText(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function htmlText(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value);
  }

  function formValue(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value);
  }

  function statusAttributes(value) {
    return 'data-status="' + escapeHtml(statusValue(value)) + '" data-status-tier="' + escapeHtml(statusTier(value)) + '"';
  }

  function statusPill(value) {
    return '<span class="status-pill" ' + statusAttributes(value) + ">" + escapeHtml(humanStatusLabel(value)) + "</span>";
  }

  function statusFeedRow(title, status, detail) {
    var rowStatus = text(status || "unknown");
    return "<li " + statusAttributes(rowStatus) + "><strong>" + escapeHtml(title) + "</strong>" +
      statusPill(rowStatus) +
      "<small>" + escapeHtml(detail || "") + "</small></li>";
  }

  function row(label, value) {
    return "<dt>" + escapeHtml(label) + "</dt><dd>" + escapeHtml(value) + "</dd>";
  }

  function parseTimestamp(value) {
    if (!value || typeof value !== "string") {
      return null;
    }
    var normalized = value.replace(/Z$/, "+00:00");
    var ms = Date.parse(normalized);
    return Number.isFinite(ms) ? ms : null;
  }

  function firstTimestamp(record, keys) {
    if (!record || typeof record !== "object") {
      return null;
    }
    for (var index = 0; index < keys.length; index += 1) {
      var ms = parseTimestamp(record[keys[index]]);
      if (ms !== null) {
        return ms;
      }
    }
    return null;
  }

  function compactTimestamp(value) {
    var ms = typeof value === "number" ? value : parseTimestamp(value);
    if (ms === null) {
      return "pending";
    }
    var date = new Date(ms);
    var month = String(date.getMonth() + 1).padStart(2, "0");
    var day = String(date.getDate()).padStart(2, "0");
    var hour = String(date.getHours()).padStart(2, "0");
    var minute = String(date.getMinutes()).padStart(2, "0");
    return month + "-" + day + " " + hour + ":" + minute;
  }

  function durationLabel(ms) {
    if (!Number.isFinite(ms) || ms < 0) {
      return "pending";
    }
    var seconds = Math.floor(ms / 1000);
    var days = Math.floor(seconds / 86400);
    seconds %= 86400;
    var hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    var minutes = Math.floor(seconds / 60);
    seconds %= 60;
    if (days) {
      return days + "d " + hours + "h";
    }
    if (hours) {
      return hours + "h " + minutes + "m";
    }
    if (minutes) {
      return minutes + "m " + seconds + "s";
    }
    return seconds + "s";
  }

  function timestampIso(ms) {
    if (!Number.isFinite(ms)) {
      return "";
    }
    return new Date(ms).toISOString().replace(".000Z", "Z");
  }

  function isActiveStatus(value) {
    return statusTier(value) === "active" || ["queued", "pending", "waiting", "requested"].indexOf(statusValue(value)) !== -1;
  }

  function isTerminalStatus(value) {
    var normalized = statusValue(value);
    return ["pass", "ok", "current", "completed", "completed_with_warnings", "complete", "done", "succeeded", "success", "satisfied", "failed", "fail", "failure", "error", "blocked", "rejected", "invalid", "archived_view", "stopped", "cancelled", "aborted", "released"].indexOf(normalized) !== -1;
  }

  function nodeTiming(node) {
    var item = node && typeof node === "object" ? node : {};
    var started = firstTimestamp(item, ["started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"]);
    var ended = firstTimestamp(item, ["ended_at", "completed_at", "finished_at", "validated_at", "updated_at", "heartbeat_at", "ts", "timestamp"]);
    if (started === null && ended !== null) {
      started = ended;
    }
    var active = item.active === true || isActiveStatus(item.status);
    if (active) {
      ended = null;
    }
    if (ended === null && started !== null && isTerminalStatus(item.status)) {
      ended = started;
    }
    var elapsedSeconds = Number.isFinite(item.elapsed_seconds) ? item.elapsed_seconds : null;
    if (elapsedSeconds === null) {
      var elapsedEnd = ended !== null ? ended : (active && started !== null ? Date.now() : null);
      elapsedSeconds = started !== null && elapsedEnd !== null ? Math.max(0, Math.floor((elapsedEnd - started) / 1000)) : null;
    }
    return {
      started: cleanText(item.started_at) || (started !== null ? timestampIso(started) : "pending"),
      ended: active && started !== null ? "Present" : (cleanText(item.ended_at) || (ended !== null ? timestampIso(ended) : "pending")),
      elapsed: elapsedSeconds === null ? "pending" : durationLabel(elapsedSeconds * 1000)
    };
  }

  function list(title, values) {
    if (!Array.isArray(values) || values.length === 0) {
      return "";
    }
    var items = values.map(function (value) {
      return "<li>" + escapeHtml(value) + "</li>";
    }).join("");
    return '<div class="tag-section"><h4>' + escapeHtml(title) + "</h4><ul>" + items + "</ul></div>";
  }

  function workflowElapsedLabel(payload, workflowStatus, workflowGraph, feed) {
    var startCandidates = [];
    var endCandidates = [];
    var status = workflowStatus && typeof workflowStatus === "object" ? workflowStatus : {};
    var graph = workflowGraph && typeof workflowGraph === "object" ? workflowGraph : {};
    var nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    var feedRows = Array.isArray(feed) ? feed : [];
    [status.started_at, status.created_at, payload && payload.started_at].forEach(function (value) {
      var ms = parseTimestamp(value);
      if (ms !== null) {
        startCandidates.push(ms);
      }
    });
    [status.ended_at, status.completed_at, status.updated_at].forEach(function (value) {
      var ms = parseTimestamp(value);
      if (ms !== null) {
        endCandidates.push(ms);
      }
    });
    nodes.forEach(function (node) {
      var start = firstTimestamp(node, ["started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"]);
      var end = firstTimestamp(node, ["ended_at", "completed_at", "finished_at", "validated_at", "updated_at", "heartbeat_at", "ts", "timestamp", "started_at"]);
      if (start !== null) {
        startCandidates.push(start);
      }
      if (end !== null) {
        endCandidates.push(end);
      }
    });
    feedRows.forEach(function (record) {
      var ms = firstTimestamp(record, ["ts", "created_at", "generated_at"]);
      if (ms !== null) {
        startCandidates.push(ms);
        endCandidates.push(ms);
      }
    });
    if (!startCandidates.length) {
      return "unknown";
    }
    var started = Math.min.apply(null, startCandidates);
    var rendered = parseTimestamp(payload && (payload.rendered_at || payload.generated_at)) || Date.now();
    var ended = isTerminalStatus(status.status) && endCandidates.length ? Math.max.apply(null, endCandidates) : rendered;
    return durationLabel(Math.max(0, ended - started));
  }

  function readModel(payload, filename) {
    return payload && payload.read_models && payload.read_models[filename] ? payload.read_models[filename] : {};
  }

  function jsonlModel(payload, filename) {
    return payload && payload.jsonl_models && Array.isArray(payload.jsonl_models[filename]) ? payload.jsonl_models[filename] : [];
  }

  function renderNode(node, detail) {
    var summary = node.summary && typeof node.summary === "object" ? node.summary : {};
    var detailPayload = detail && typeof detail === "object" ? detail : {};
    var deliverables = node.deliverables === null || node.deliverables === undefined ? "" : String(node.deliverables);
    var timing = nodeTiming(node);
    var primary = '<div class="node-detail-primary">' +
      "<p>" + escapeHtml(summary.one_line || "No summary available.") + "</p>" +
      (deliverables ? '<dl class="detail-list compact-detail-list">' + row("Deliverables", deliverables) + "</dl>" : "") +
      "</div>";
    var sections = Array.isArray(detailPayload.sections) ? detailPayload.sections : [{
      key: "summary",
      title: "Summary",
      available: true,
      content: summary.one_line || "No summary available."
    }];
    return '<article class="node-detail">' +
      "<h3>" + escapeHtml(node.title || node.node_id || "Node") + "</h3>" +
      primary +
      '<dl class="detail-list">' +
      row("Node", node.node_id) +
      row("Type", node.type) +
      row("Status", node.status) +
      row("Task", node.task_id) +
      row("Run", node.run_id) +
      row("Started", timing.started) +
      row("Ended", timing.ended) +
      row("Elapsed", timing.elapsed) +
      "</dl>" +
      list("Highlights", summary.highlights) +
      list("Risks", summary.risks) +
      list("Output Refs", node.output_refs) +
      '<div class="node-detail-sections">' + sections.map(renderNodeDetailSection).join("") + "</div>" +
      "</article>";
  }

  function nodeDetailForNode(payload, node) {
    var nodeDetails = payload && payload.node_details && typeof payload.node_details === "object" ? payload.node_details : {};
    var nodes = nodeDetails.nodes && typeof nodeDetails.nodes === "object" ? nodeDetails.nodes : {};
    var runs = nodeDetails.runs && typeof nodeDetails.runs === "object" ? nodeDetails.runs : {};
    if (node && node.node_id && nodes[node.node_id]) {
      return nodes[node.node_id];
    }
    if (node && node.run_id && runs[node.run_id]) {
      return runs[node.run_id];
    }
    return {};
  }

  function renderNodeDetailSection(section) {
    var item = section && typeof section === "object" ? section : {};
    var key = text(item.key || "section");
    var title = nodeDetailSectionTitle(key, item.title || key.replace(/_/g, " "));
    if (item.available !== true) {
      return '<section class="node-detail-section" data-section="' + escapeHtml(key) + '" data-available="false">' +
        "<h4>" + escapeHtml(title) + "</h4>" +
        '<p class="empty-state">' + escapeHtml(nodeDetailSectionEmptyMessage(key, item.empty_message || "No evidence recorded.")) + "</p>" +
        "</section>";
    }
    var body = "";
    if (item.path) {
      body += renderDetailFileAction(item.path, title, item.content || "", item);
    }
    if (item.status || (item.summary && typeof item.summary !== "object")) {
      body += '<dl class="detail-list compact-detail-list">' +
        row("Status", item.status) +
        row("Summary", item.summary) +
        "</dl>";
    }
    if (item.content && !item.path) {
      body += renderDetailFileAction("", title, item.content, item);
    }
    if (Array.isArray(item.items) && item.items.length) {
      body += renderDetailItems(item.items);
    }
    if (Array.isArray(item.changed_files) && item.changed_files.length) {
      body += renderChangedFileItems(item.changed_files);
    }
    if (item.summary && typeof item.summary === "object") {
      body += renderDetailMapping("Summary", item.summary);
    }
    if (item.patch && typeof item.patch === "object") {
      body += renderArtifactDetail("Patch Artifact", item.patch);
    }
    if ((item.before && typeof item.before === "object") || (item.after && typeof item.after === "object")) {
      body += '<div class="node-checkpoint-grid">' +
        (item.before ? renderDetailMapping("Before", item.before) : '<p class="empty-state">No before checkpoint.</p>') +
        (item.after ? renderDetailMapping("After", item.after) : '<p class="empty-state">No after checkpoint.</p>') +
        "</div>";
    }
    if (item.truncated === true) {
      body += '<p class="selector-status">Additional records exist; this view is capped for safety.</p>';
    }
    if (!body) {
      body = '<p class="empty-state">Evidence metadata is available but has no displayable details.</p>';
    }
    return '<section class="node-detail-section" data-section="' + escapeHtml(key) + '" data-available="true">' +
      "<h4>" + escapeHtml(title) + "</h4>" +
      body +
      "</section>";
  }

  function nodeDetailSectionTitle(key, title) {
    var value = text(title || "");
    if (key === "final_output" && value === "Final Output") {
      return "Final Response";
    }
    return value;
  }

  function nodeDetailSectionEmptyMessage(key, message) {
    var value = text(message || "");
    if (key === "final_output" && value === "No final output file was recorded for this run.") {
      return "No final response file was recorded for this run.";
    }
    return value;
  }

  function renderDetailFileAction(path, title, content, metadata) {
    var pathValue = path ? String(path) : "";
    var contentValue = content ? String(content) : "";
    if (!pathValue && !contentValue) {
      return "";
    }
    var dataPath = pathValue ? ' data-detail-path="' + escapeHtml(pathValue) + '"' : "";
    var dataContent = contentValue ? ' data-detail-content="' + escapeHtml(contentValue) + '"' : "";
    var renderMode = detailRenderMode(pathValue, metadata && metadata.render_mode);
    var dataRender = renderMode ? ' data-detail-render="' + escapeHtml(renderMode) + '"' : "";
    var truncated = metadata && metadata.truncated === true;
    var truncatedAttr = truncated ? ' data-detail-truncated="true"' : "";
    var linkHtml = pathValue ? '<a class="detail-file-link" href="#" data-detail-file-link' + dataPath + ">Open file</a>" : "";
    var streamHtml = isLogPath(pathValue) ? '<button type="button" class="detail-file-button" data-log-stream-title="' +
      escapeHtml(title || "Log Stream") + '"' + dataPath + ">Follow log tail</button>" : "";
    var sizeRow = metadata && metadata.size_bytes !== undefined && metadata.size_bytes !== null ? row("Size", metadata.size_bytes + " bytes") : "";
    return '<div class="detail-file-card"' + truncatedAttr + ">" +
      '<dl class="detail-list compact-detail-list">' +
      (pathValue ? row("Path", pathValue) : "") +
      sizeRow +
      "</dl>" +
      '<div class="detail-file-actions">' +
      linkHtml +
      '<button type="button" class="detail-file-button" data-detail-title="' + escapeHtml(title || "File Preview") + '"' +
      dataPath + dataContent + dataRender + truncatedAttr + ">Preview</button>" +
      streamHtml +
      "</div>" +
      "</div>";
  }

  function detailRenderMode(path, explicit) {
    var mode = cleanText(explicit).toLowerCase();
    if (mode === "markdown" || mode === "text" || mode === "image") {
      return mode;
    }
    if (isImagePath(path)) {
      return "image";
    }
    return isMarkdownPath(path) ? "markdown" : "text";
  }

  function isImagePath(path) {
    var lowered = cleanText(path).toLowerCase();
    return !!lowered && (
      lowered.endsWith(".svg") ||
      lowered.endsWith(".png") ||
      lowered.endsWith(".jpg") ||
      lowered.endsWith(".jpeg") ||
      lowered.endsWith(".gif") ||
      lowered.endsWith(".webp")
    );
  }

  function isMarkdownPath(path) {
    var lowered = cleanText(path).toLowerCase();
    return !!lowered && (
      lowered.endsWith(".md") ||
      lowered.endsWith(".markdown") ||
      lowered.endsWith(".mdown") ||
      lowered.endsWith(".mkd")
    );
  }

  function isLogPath(path) {
    var lowered = cleanText(path).toLowerCase();
    return !!lowered && (
      lowered.endsWith(".log") ||
      lowered.endsWith(".out") ||
      lowered.endsWith(".err") ||
      lowered.indexOf("/logs/") !== -1 ||
      lowered.endsWith("_stdout") ||
      lowered.endsWith("_stderr")
    );
  }

  function renderArtifactDetail(title, item) {
    var metadata = humanDetailMetadata(item || {}, {content: true, path: true});
    return '<div class="detail-mapping"><h5>' + escapeHtml(title) + "</h5>" +
      renderDetailFileAction(item && item.path, title, item && item.content, item || {}) +
      renderDetailMapping("", metadata) +
      "</div>";
  }

  function renderDetailItems(items) {
    return '<ul class="node-detail-item-list">' + items.map(function (item) {
      if (item && typeof item === "object") {
        var label = detailItemLabel(item);
        var metadata = humanDetailMetadata(item, {content: true, path: true});
        return "<li><strong>" + escapeHtml(label) + "</strong>" +
          renderDetailMapping("", metadata) +
          renderDetailFileAction(item.path || "", label, item.content || "", item) +
          "</li>";
      }
      return "<li><span>" + escapeHtml(item) + "</span></li>";
    }).join("") + "</ul>";
  }

  function renderChangedFileItems(items) {
    return '<ul class="node-detail-item-list changed-file-list">' + items.map(function (item) {
      var stats = [];
      if (item.lines_added !== undefined && item.lines_added !== null) {
        stats.push("+" + item.lines_added);
      }
      if (item.lines_deleted !== undefined && item.lines_deleted !== null) {
        stats.push("-" + item.lines_deleted);
      }
      return "<li><strong>" + escapeHtml(item.path || "changed file") + "</strong>" +
        "<span>" + escapeHtml((item.change_type || "changed") + (stats.length ? " (" + stats.join(", ") + ")" : "")) + "</span></li>";
    }).join("") + "</ul>";
  }

  function renderDetailMapping(title, values) {
    if (!values || typeof values !== "object") {
      return "";
    }
    var visibleValues = humanDetailMetadata(values, {});
    var rows = Object.keys(visibleValues).map(function (key) {
      var value = visibleValues[key];
      if (value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) {
        return "";
      }
      var display = value;
      if (Array.isArray(value)) {
        display = value.join(", ");
      } else if (value && typeof value === "object") {
        display = JSON.stringify(value);
      }
      return row(key.replace(/_/g, " ").replace(/\b\w/g, function (chr) { return chr.toUpperCase(); }), display);
    }).join("");
    if (!rows) {
      return "";
    }
    return '<div class="detail-mapping">' + (title ? "<h5>" + escapeHtml(title) + "</h5>" : "") +
      '<dl class="detail-list compact-detail-list">' + rows + "</dl></div>";
  }

  function detailItemLabel(item) {
    var keys = ["path", "title", "name", "status", "type", "event_type", "reason", "change_request_id", "request_id", "run_id", "task_id"];
    for (var index = 0; index < keys.length; index += 1) {
      var value = text(item && item[keys[index]]);
      if (value) {
        return value;
      }
    }
    return "record";
  }

  function humanDetailMetadata(item, exclude) {
    var hidden = {
      sha: true,
      sha256: true,
      event_hash: true,
      events_sha256: true,
      events_segment_manifest: true,
      content_sha256: true,
      source_hashes: true,
      token: true,
      access_token: true,
      api_key: true,
      secret: true
    };
    var result = {};
    Object.keys(item || {}).forEach(function (key) {
      var value = item[key];
      if ((exclude && exclude[key]) || hidden[key] || /(_sha|_sha256|_hash|_token)$/.test(key)) {
        return;
      }
      if (value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) {
        return;
      }
      if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) {
        return;
      }
      result[key] = value;
    });
    return result;
  }

  function buildPhaseTiming(phase, tasks, workflowGraph, referenceTime) {
    var graph = workflowGraph && typeof workflowGraph === "object" ? workflowGraph : {};
    var nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    var taskIds = {};
    var starts = [];
    var ends = [];
    var active = isActiveStatus(phase && phase.status);
    tasks.forEach(function (task) {
      var taskId = task && task.task_id ? String(task.task_id) : "";
      if (taskId) {
        taskIds[taskId] = true;
      }
      var taskStart = firstTimestamp(task, ["started_at", "created_at", "assigned_at", "last_updated_at"]);
      var taskEnd = firstTimestamp(task, ["ended_at", "completed_at", "finished_at", "validated_at", "last_updated_at"]);
      if (taskStart !== null) {
        starts.push(taskStart);
      }
      if (taskEnd !== null) {
        ends.push(taskEnd);
      }
      active = active || isActiveStatus(task.status);
    });
    nodes.forEach(function (node) {
      var taskId = node && node.task_id ? String(node.task_id) : "";
      if (!taskId || !taskIds[taskId]) {
        return;
      }
      var nodeStart = firstTimestamp(node, ["started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"]);
      var nodeEnd = firstTimestamp(node, ["ended_at", "completed_at", "finished_at", "validated_at", "updated_at", "heartbeat_at", "ts", "timestamp", "started_at"]);
      if (nodeStart !== null) {
        starts.push(nodeStart);
      }
      if (nodeEnd !== null) {
        ends.push(nodeEnd);
      }
      active = active || isActiveStatus(node.status);
    });
    if (!starts.length && !ends.length) {
      return {available: false, start: null, end: null, duration: null, active: active};
    }
    var start = starts.length ? Math.min.apply(null, starts) : Math.min.apply(null, ends);
    var end = active ? null : (ends.length ? Math.max.apply(null, ends) : null);
    var durationEnd = end !== null ? end : (active ? referenceTime : null);
    return {
      available: true,
      start: start,
      end: end,
      duration: durationEnd !== null ? Math.max(0, durationEnd - start) : null,
      active: active
    };
  }

  function renderPhaseTiming(timing, maxDuration) {
    if (!timing || !timing.available) {
      return '<div class="phase-timing" data-phase-timing="pending">' +
        '<div class="phase-timing-facts"><span><small>Start</small><strong>pending</strong></span>' +
        '<span><small>End</small><strong>pending</strong></span>' +
        '<span><small>Duration</small><strong>pending</strong></span></div>' +
        '<div class="phase-duration-track"><span class="phase-duration-bar" style="--phase-duration-pct: 0%"></span></div>' +
        "</div>";
    }
    var percent = !timing.active && timing.end !== null ? 100 :
      (timing.duration && maxDuration ? Math.max(8, Math.min(100, Math.round((timing.duration / maxDuration) * 100))) : 8);
    return '<div class="phase-timing" data-phase-timing="' + escapeHtml(timing.active ? "running" : "recorded") + '">' +
      '<div class="phase-timing-facts">' +
      '<span><small>Start</small><strong>' + escapeHtml(compactTimestamp(timing.start)) + "</strong></span>" +
      '<span><small>End</small><strong>' + escapeHtml(timing.end === null && timing.active ? "Present" : compactTimestamp(timing.end)) + "</strong></span>" +
      '<span><small>Duration</small><strong>' + escapeHtml(durationLabel(timing.duration)) + "</strong></span>" +
      "</div>" +
      '<div class="phase-duration-track"><span class="phase-duration-bar" style="--phase-duration-pct: ' + escapeHtml(percent) + '%"></span></div>' +
      "</div>";
  }

  function phaseProgress(phase, tasks) {
    var status = statusValue(phase && phase.status);
    var progress = phase && phase.progress && typeof phase.progress === "object" ? phase.progress : {};
    var rawPercent = progress.progress_percent !== undefined ? progress.progress_percent :
      (phase && (phase.progress_percent !== undefined ? phase.progress_percent : phase.percent_complete));
    if (typeof rawPercent === "number" && isFinite(rawPercent)) {
      var explicitPercent = Math.max(0, Math.min(100, Math.round(rawPercent)));
      return {percent: explicitPercent, label: explicitPercent + "% complete"};
    }
    var completed = progress.completed_tasks !== undefined ? progress.completed_tasks :
      (progress.completed_count !== undefined ? progress.completed_count : phase && phase.completed_count);
    var total = progress.total_tasks !== undefined ? progress.total_tasks :
      (progress.task_count !== undefined ? progress.task_count : phase && phase.task_count);
    if (typeof completed === "number" && typeof total === "number" && total > 0) {
      var countPercent = Math.max(0, Math.min(100, Math.round((completed / total) * 100)));
      return {percent: countPercent, label: completed + "/" + total + " tasks"};
    }
    if (Array.isArray(tasks) && tasks.length) {
      var done = tasks.filter(function (task) {
        return statusTier(task && task.status) === "success";
      }).length;
      return {percent: Math.max(0, Math.min(100, Math.round((done / tasks.length) * 100))), label: done + "/" + tasks.length + " tasks"};
    }
    if (statusTier(status) === "success") {
      return {percent: 100, label: "phase complete"};
    }
    if (statusTier(status) === "active") {
      return {percent: 50, label: "running"};
    }
    return {percent: 0, label: "not started"};
  }

  function renderPhaseProgress(progress) {
    var percent = progress && typeof progress.percent === "number" ? Math.max(0, Math.min(100, Math.round(progress.percent))) : 0;
    return '<div class="phase-progress" aria-label="Phase progress">' +
      '<div class="phase-progress-label"><span>Progress</span><strong>' + escapeHtml(progress && progress.label ? progress.label : percent + "%") + "</strong></div>" +
      '<div class="phase-progress-track"><span class="phase-progress-bar" style="--phase-progress-pct: ' + escapeHtml(percent) + '%"></span></div>' +
      "</div>";
  }

  function renderObjectiveList(objectives, title) {
    var rows = Array.isArray(objectives) ? objectives : [];
    if (!rows.length) {
      return "";
    }
    return '<section class="objective-section">' +
      '<div class="objective-section-heading"><strong>' + escapeHtml(title || "Objectives") + '</strong><span>' + escapeHtml(rows.length) + '</span></div>' +
      '<ul class="objective-list">' +
      rows.map(function (objective) {
        var status = text(objective.status || objective.plan_status || "needs_verification");
        var result = objective.result && typeof objective.result === "object" ? objective.result : {};
        var followup = objective.followup_tasks || result.suggested_followup || "";
        if (Array.isArray(followup)) {
          followup = followup.join(", ");
        }
        return '<li class="objective-row" ' + statusAttributes(status) + '>' +
          statusPill(status) +
          '<div>' +
          '<strong>' + escapeHtml(objective.objective_id || "objective") + '</strong>' +
          '<span>' + escapeHtml(objective.text || "Objective") + '</span>' +
          '<small>' + escapeHtml(followup ? "follow-up: " + followup : (objective.report_status || result.verdict || "verification pending")) + '</small>' +
          '</div>' +
          '</li>';
      }).join("") +
      '</ul>' +
      '</section>';
  }

  function expansionNote(item, entityLabel) {
    if (!item || item.expanded !== true) {
      return "";
    }
    var label = "Added by self-expansion";
    if (entityLabel) {
      label = entityLabel + " added by self-expansion";
    }
    return '<small class="expansion-note" title="' + escapeHtml(label) + '" aria-label="' + escapeHtml(label) + '">Self-expansion</small>';
  }

  function expandedAttribute(item) {
    return item && item.expanded === true ? ' data-expanded="true"' : "";
  }

  function renderPlanPanel(planIndex, planMarkdown, workflowGraph, payload) {
    var phases = Array.isArray(planIndex.phases) ? planIndex.phases : [];
    var workflowObjectives = Array.isArray(planIndex.objectives) ? planIndex.objectives.filter(function (objective) {
      return objective && objective.scope === "workflow";
    }) : [];
    var referenceTime = parseTimestamp(payload && (payload.rendered_at || payload.generated_at)) || Date.now();
    var phaseTimings = phases.map(function (phase) {
      var tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
      return buildPhaseTiming(phase, tasks, workflowGraph, referenceTime);
    });
    var maxDuration = phaseTimings.reduce(function (max, timing) {
      return timing && timing.duration ? Math.max(max, timing.duration) : max;
    }, 0);
    var checklistBlocks = [];
    if (phases.length === 0) {
      checklistBlocks.push('<p class="empty-state">No checklist tasks are present.</p>');
    }
    phases.forEach(function (phase, phaseIndex) {
      var tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
      var taskRows = tasks.map(function (task) {
        var display = task.display && typeof task.display === "object" ? task.display : {};
        var status = text(task.status || "unknown");
        var taskTitle = renderHumanSummaryTrigger(task.title || "Untitled task", task.human_summary || {}, "task-summary-link");
        return '<li class="task-row" ' + statusAttributes(status) + expandedAttribute(task) + ">" +
          statusPill(status) +
          "<div>" +
          "<strong>" + escapeHtml(task.task_id || "task") + "</strong>" +
          expansionNote(task, "Task") +
          "<span>" + taskTitle + "</span>" +
          "<small>" + escapeHtml(task.validation_status || display.subtitle || "validation unknown") + "</small>" +
          "</div>" +
          "</li>";
      }).join("");
      var phaseStatus = text(phase.status || "unknown");
      var phaseTitle = renderHumanSummaryTrigger(phase.title || "Unphased", phase.human_summary || {}, "phase-summary-link");
      var objectiveRows = Array.isArray(phase.objectives) ? phase.objectives : [];
      checklistBlocks.push('<article class="phase-block" ' + statusAttributes(phaseStatus) + expandedAttribute(phase) + ">" +
        '<div class="phase-heading">' +
        '<div class="phase-title-block">' +
        "<h3>" + phaseTitle + "</h3>" +
        expansionNote(phase, "Phase") +
        "</div>" +
        statusPill(phaseStatus) +
        "</div>" +
        renderPhaseProgress(phaseProgress(phase, tasks)) +
        renderPhaseTiming(phaseTimings[phaseIndex], maxDuration) +
        '<ol class="task-list">' + taskRows + "</ol>" +
        renderObjectiveList(objectiveRows, "Phase objectives") +
        "</article>");
    });
    return '<div class="plan-view-toggle" role="tablist" aria-label="Plan view">' +
      '<button type="button" class="is-active" data-plan-view="checklist" aria-pressed="true">Checklist</button>' +
      '<button type="button" data-plan-view="markdown" aria-pressed="false">Full Markdown</button>' +
      "</div>" +
      '<div id="plan-checklist-view" class="plan-view plan-checklist-view is-active" data-plan-view-panel="checklist">' +
      checklistBlocks.join("") +
      renderObjectiveList(workflowObjectives, "Workflow objectives") +
      "</div>" +
      '<div id="plan-markdown-view" class="plan-view plan-markdown-view" data-plan-view-panel="markdown" hidden>' +
      renderPlanMarkdownView(planMarkdown) +
      "</div>";
  }

  function renderPlanMarkdownView(planMarkdown) {
    var payload = planMarkdown && typeof planMarkdown === "object" ? planMarkdown : {};
    var content = text(payload.content || "");
    var planPath = text(payload.path || "PLAN.md");
    if (!content) {
      return '<div class="plan-markdown-meta"><strong>Full plan markdown</strong><small>' +
        escapeHtml(planPath) + " unavailable</small></div>" +
        '<p class="empty-state">The plan markdown file could not be loaded.</p>' +
        "";
    }
    var sizeLabel = typeof payload.size_bytes === "number" ? payload.size_bytes + " bytes" : "markdown";
    return '<div class="plan-markdown-meta"><strong>Full PLAN.md</strong><small>' +
      escapeHtml(planPath) + " · " + escapeHtml(sizeLabel) + "</small></div>" +
      '<div class="markdown-document">' + renderMarkdownDocument(content, {path: planPath}) + "</div>";
  }

  function renderMarkdownDocument(content, options) {
    var context = options && typeof options === "object" ? options : {};
    var blocks = [];
    var listStack = [];
    var inCode = false;
    var codeLines = [];

    function closeLists() {
      while (listStack.length) {
        blocks.push("</" + listStack.pop() + ">");
      }
    }

    var lines = String(content || "").split(/\r?\n/);
    for (var lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      var rawLine = lines[lineIndex];
      var line = rawLine.replace(/\s+$/, "");
      var stripped = line.trim();
      var match;
      if (stripped.indexOf("```") === 0) {
        if (inCode) {
          blocks.push('<pre class="markdown-code"><code>' + escapeHtml(codeLines.join("\n")) + "</code></pre>");
          codeLines = [];
          inCode = false;
        } else {
          closeLists();
          inCode = true;
          codeLines = [];
        }
        continue;
      }
      if (inCode) {
        codeLines.push(rawLine);
        continue;
      }
      if (isMarkdownTableStart(lines, lineIndex)) {
        closeLists();
        var tableLines = [stripped, String(lines[lineIndex + 1] || "").trim()];
        lineIndex += 2;
        while (lineIndex < lines.length && cleanText(lines[lineIndex]) && cleanText(lines[lineIndex]).indexOf("|") !== -1) {
          tableLines.push(String(lines[lineIndex] || "").trim());
          lineIndex += 1;
        }
        lineIndex -= 1;
        blocks.push(renderMarkdownTable(tableLines, context));
        continue;
      }
      if (!stripped) {
        closeLists();
        continue;
      }
      match = stripped.match(/^(#{1,6})\s+(.+)$/);
      if (match) {
        closeLists();
        var level = Math.min(match[1].length + 2, 6);
        blocks.push("<h" + level + ">" + markdownInline(match[2], context) + "</h" + level + ">");
        continue;
      }
      var image = parseMarkdownImageLine(stripped);
      if (image) {
        closeLists();
        blocks.push(renderMarkdownFigure(image.alt, image.path, image.title, context));
        continue;
      }
      match = stripped.match(/^[-*]\s+\[([ xX])\]\s+(.+)$/);
      if (match) {
        if (listStack[listStack.length - 1] !== "ul") {
          closeLists();
          blocks.push('<ul class="markdown-task-list">');
          listStack.push("ul");
        }
        blocks.push('<li class="markdown-task"><input type="checkbox" disabled' +
          (match[1].toLowerCase() === "x" ? " checked" : "") +
          "><span>" + markdownInline(match[2], context) + "</span></li>");
        continue;
      }
      match = stripped.match(/^[-*]\s+(.+)$/);
      if (match) {
        if (listStack[listStack.length - 1] !== "ul") {
          closeLists();
          blocks.push("<ul>");
          listStack.push("ul");
        }
        blocks.push("<li>" + markdownInline(match[1], context) + "</li>");
        continue;
      }
      match = stripped.match(/^\d+[.)]\s+(.+)$/);
      if (match) {
        if (listStack[listStack.length - 1] !== "ol") {
          closeLists();
          blocks.push("<ol>");
          listStack.push("ol");
        }
        blocks.push("<li>" + markdownInline(match[1], context) + "</li>");
        continue;
      }
      closeLists();
      blocks.push("<p>" + markdownInline(stripped, context) + "</p>");
    }
    if (inCode) {
      blocks.push('<pre class="markdown-code"><code>' + escapeHtml(codeLines.join("\n")) + "</code></pre>");
    }
    closeLists();
    return blocks.join("");
  }

  function isMarkdownTableStart(lines, index) {
    var header = cleanText(lines[index]);
    var divider = cleanText(lines[index + 1]);
    return header.indexOf("|") !== -1 && isMarkdownTableDivider(divider);
  }

  function isMarkdownTableDivider(line) {
    if (!line || line.indexOf("|") === -1) {
      return false;
    }
    return splitMarkdownTableRow(line).every(function (cell) {
      return /^:?-{3,}:?$/.test(cleanText(cell));
    });
  }

  function splitMarkdownTableRow(line) {
    var trimmed = String(line || "").trim();
    if (trimmed.charAt(0) === "|") {
      trimmed = trimmed.slice(1);
    }
    if (trimmed.charAt(trimmed.length - 1) === "|") {
      trimmed = trimmed.slice(0, -1);
    }
    return trimmed.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function renderMarkdownTable(lines, context) {
    var header = splitMarkdownTableRow(lines[0] || "");
    var rows = lines.slice(2).map(splitMarkdownTableRow).filter(function (row) {
      return row.length && row.some(function (cell) { return cleanText(cell); });
    });
    var head = "<thead><tr>" + header.map(function (cell) {
      return "<th>" + markdownInline(cell, context) + "</th>";
    }).join("") + "</tr></thead>";
    var body = "<tbody>" + rows.map(function (row) {
      return "<tr>" + header.map(function (_cell, index) {
        return "<td>" + markdownInline(row[index] || "", context) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</tbody>";
    return '<div class="markdown-table-wrap"><table>' + head + body + "</table></div>";
  }

  function renderMarkdownFigure(alt, rawPath, title, context) {
    var src = markdownAssetUrl(rawPath, context);
    var caption = cleanText(title || alt);
    return '<figure class="markdown-figure">' +
      '<a class="markdown-figure-link" href="' + escapeHtml(src) + '" target="_blank" rel="noopener noreferrer">' +
      '<img src="' + escapeHtml(src) + '" alt="' + escapeHtml(alt || caption || "figure") + '" loading="lazy">' +
      "</a>" +
      (caption ? '<figcaption>' + markdownInline(caption, context) + "</figcaption>" : "") +
      "</figure>";
  }

  function parseMarkdownImageLine(line) {
    var match = String(line || "").match(/^!\[([^\]]*)\]\((.*)\)$/);
    if (!match) {
      return null;
    }
    var destination = parseMarkdownDestinationAndTitle(match[2]);
    if (!destination.path) {
      return null;
    }
    return {alt: match[1], path: destination.path, title: destination.title};
  }

  function parseMarkdownDestinationAndTitle(rawValue) {
    var body = decodeHtmlEntities(cleanText(rawValue));
    var title = "";
    var titleMatch = body.match(/^(.*?)\s+(["'])(.*?)\2\s*$/);
    if (titleMatch) {
      body = titleMatch[1];
      title = titleMatch[3];
    }
    return {path: body.trim().replace(/^<|>$/g, ""), title: title};
  }

  function markdownAssetUrl(rawPath, context) {
    var path = decodeHtmlEntities(cleanText(rawPath).replace(/^<|>$/g, ""));
    if (!path) {
      return "";
    }
    var payload = activePayload || {};
    var projectRelative = projectRelativeFromAbsolutePath(path, payload);
    if (projectRelative) {
      return projectFileMarkdownUrl(payload, projectRelative);
    }
    if (/^(?:https?:|data:|blob:)/i.test(path)) {
      return path;
    }
    if (/^\//.test(path)) {
      return tokenizedDashboardPath(path);
    }
    path = normalizeRelativePath(path);
    var resolved = resolveMarkdownAssetPath(path, context && context.path);
    return projectFileMarkdownUrl(payload, resolved);
  }

  function projectFileMarkdownUrl(payload, path) {
    if (payload.server_mode && payload.workflow_id) {
      return fileUrl(payload, path);
    }
    var base = cleanText(payload.static_project_root_href || "../..");
    return joinUrlPath(base, path);
  }

  function projectRelativeFromAbsolutePath(path, payload) {
    var source = normalizeAbsoluteFilePath(path);
    var projectRoot = normalizeAbsoluteFilePath(payload && payload.project_root);
    if (!source || !projectRoot || source.charAt(0) !== "/" || projectRoot.charAt(0) !== "/") {
      return "";
    }
    if (source === projectRoot) {
      return "";
    }
    if (source.indexOf(projectRoot + "/") !== 0) {
      return "";
    }
    return normalizeRelativePath(source.slice(projectRoot.length + 1));
  }

  function normalizeAbsoluteFilePath(path) {
    return cleanText(path).replace(/\\/g, "/").replace(/\/+/g, "/").replace(/\/+$/, "");
  }

  function tokenizedDashboardPath(path) {
    if (!/^\/(?:api|read_models)\//.test(path)) {
      return path;
    }
    return appendDashboardToken(path);
  }

  function appendDashboardToken(url) {
    var token = tokenValue();
    if (!token || /(?:^|[?&])token=/.test(url)) {
      return url;
    }
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "token=" + encodeURIComponent(token);
  }

  function resolveMarkdownAssetPath(path, markdownPath) {
    if (looksProjectRelative(path) || !markdownPath) {
      return normalizeRelativePath(path);
    }
    return normalizeRelativePath(dirnamePath(markdownPath) + "/" + path);
  }

  function looksProjectRelative(path) {
    if (/^\.loopplane\//.test(path)) {
      return true;
    }
    if (/^(?:\.\.?\/)/.test(path)) {
      return false;
    }
    return cleanText(path).indexOf("/") !== -1;
  }

  function dirnamePath(path) {
    var clean = cleanText(path);
    var index = clean.lastIndexOf("/");
    return index === -1 ? "" : clean.slice(0, index);
  }

  function normalizeRelativePath(path) {
    var parts = [];
    cleanText(path).split("/").forEach(function (part) {
      if (!part || part === ".") {
        return;
      }
      if (part === "..") {
        parts.pop();
        return;
      }
      parts.push(part);
    });
    return parts.join("/");
  }

  function joinUrlPath(base, path) {
    var prefix = cleanText(base).replace(/\/+$/, "");
    var suffix = cleanText(path).replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/");
    return (prefix ? prefix + "/" : "") + suffix;
  }

  function markdownInline(value, context) {
    return String(value || "").split(/(`[^`]+`)/g).map(function (part) {
      if (/^`[^`]+`$/.test(part)) {
        return "<code>" + escapeHtml(part.slice(1, -1)) + "</code>";
      }
      return markdownInlineSegment(part, context);
    }).join("");
  }

  function markdownInlineSegment(value, context) {
    var source = String(value || "");
    var parts = [];
    var cursor = 0;
    var token;
    while ((token = nextMarkdownInlineToken(source, cursor))) {
      parts.push(markdownPlainText(source.slice(cursor, token.start), context));
      if (token.kind === "image") {
        parts.push(markdownInlineImage(token.label, token.path, token.title, context));
      } else {
        parts.push(markdownAnchor(token.label, markdownLinkHref(token.path, context), "markdown-link"));
      }
      cursor = token.end + 1;
    }
    parts.push(markdownPlainText(source.slice(cursor), context));
    return parts.join("");
  }

  function nextMarkdownInlineToken(source, startIndex) {
    var image = nextMarkdownImage(source, startIndex);
    var link = nextMarkdownLink(source, startIndex);
    if (image && link) {
      return image.start <= link.start ? image : link;
    }
    return image || link;
  }

  function nextMarkdownImage(source, startIndex) {
    var start = source.indexOf("![", startIndex);
    while (start !== -1) {
      var closeLabel = source.indexOf("]", start + 2);
      if (closeLabel !== -1 && source.charAt(closeLabel + 1) === "(") {
        var closeDest = source.indexOf(")", closeLabel + 2);
        if (closeDest !== -1) {
          var destination = parseMarkdownDestinationAndTitle(source.slice(closeLabel + 2, closeDest));
          if (destination.path) {
            return {
              kind: "image",
              start: start,
              end: closeDest,
              label: source.slice(start + 2, closeLabel),
              path: destination.path,
              title: destination.title
            };
          }
        }
      }
      start = source.indexOf("![", start + 2);
    }
    return null;
  }

  function nextMarkdownLink(source, startIndex) {
    var start = source.indexOf("[", startIndex);
    while (start !== -1) {
      if (start > 0 && source.charAt(start - 1) === "!") {
        start = source.indexOf("[", start + 1);
        continue;
      }
      var closeLabel = source.indexOf("]", start + 1);
      if (closeLabel !== -1 && source.charAt(closeLabel + 1) === "(") {
        var closeDest = source.indexOf(")", closeLabel + 2);
        if (closeDest !== -1) {
          var destination = parseMarkdownDestinationAndTitle(source.slice(closeLabel + 2, closeDest));
          if (destination.path) {
            return {
              kind: "link",
              start: start,
              end: closeDest,
              label: source.slice(start + 1, closeLabel),
              path: destination.path,
              title: destination.title
            };
          }
        }
      }
      start = source.indexOf("[", start + 1);
    }
    return null;
  }

  function markdownPlainText(value, context) {
    return markdownEmphasis(String(value || ""));
  }

  function markdownEmphasis(value) {
    return escapeHtml(value).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function markdownAnchor(label, href, className) {
    var safeHref = cleanText(href);
    if (!safeHref) {
      return markdownEmphasis(label);
    }
    return '<a class="' + escapeHtml(className || "markdown-link") + '" href="' + escapeHtml(safeHref) +
      '" target="_blank" rel="noopener noreferrer">' + markdownEmphasis(label) + "</a>";
  }

  function markdownInlineImage(alt, rawPath, title, context) {
    var src = markdownAssetUrl(rawPath, context);
    var label = cleanText(title || alt || "figure");
    if (!src) {
      return markdownEmphasis(label);
    }
    return '<span class="markdown-inline-figure">' +
      '<a class="markdown-figure-link" href="' + escapeHtml(src) + '" target="_blank" rel="noopener noreferrer">' +
      '<img src="' + escapeHtml(src) + '" alt="' + escapeHtml(label) + '" loading="lazy">' +
      "</a></span>";
  }

  function markdownLinkHref(rawPath, context) {
    var path = decodeHtmlEntities(cleanText(rawPath).replace(/^<|>$/g, ""));
    if (!path) {
      return "";
    }
    if (/^(?:mailto:|tel:|#)/i.test(path)) {
      return path;
    }
    return markdownAssetUrl(path, context);
  }

  function decodeHtmlEntities(value) {
    var source = String(value || "");
    if (source.indexOf("&") === -1) {
      return source;
    }
    var textarea = document.createElement("textarea");
    textarea.innerHTML = source;
    return textarea.value;
  }

  function humanSummaryReady(summary) {
    if (!summary || typeof summary !== "object") {
      return false;
    }
    return cleanText(summary.status) === "ready" &&
      !!(cleanText(summary.content) || cleanText(summary.markdown_path) || cleanText(summary.excerpt));
  }

  function renderHumanSummaryTrigger(label, summary, className) {
    var display = cleanText(label) || "Untitled";
    if (!humanSummaryReady(summary)) {
      return escapeHtml(display);
    }
    var title = cleanText(summary.title) || display || "Human-readable summary";
    var path = cleanText(summary.markdown_path);
    var content = cleanText(summary.content) || cleanText(summary.excerpt);
    var attrs = ' data-detail-title="' + escapeHtml(title) + '" data-detail-render="markdown"';
    if (path) {
      attrs += ' data-detail-path="' + escapeHtml(path) + '"';
    }
    if (content) {
      attrs += ' data-detail-content="' + escapeHtml(content) + '"';
    }
    return '<button type="button" class="human-summary-trigger ' + escapeHtml(className || "") + '"' +
      attrs + ' aria-label="' + escapeHtml("Open summary for " + display) + '">' +
      escapeHtml(display) + "</button>";
  }

  function renderGraphPanel(workflowGraph, planIndex) {
    var nodes = Array.isArray(workflowGraph.nodes) ? workflowGraph.nodes : [];
    var edges = Array.isArray(workflowGraph.edges) ? workflowGraph.edges : [];
    if (nodes.length === 0) {
      return '<p class="empty-state">No graph nodes are present.</p>';
    }
    var groups = graphPhaseGroups(planIndex, nodes);
    var phaseHtml = groups.map(renderGraphPhaseLane).join("");
    var edgeRows = edges.slice(0, 12).map(function (edge) {
      return "<li>" + escapeHtml(edge.source) + " to " + escapeHtml(edge.target) +
        " <span>" + escapeHtml(edge.type || "edge") + "</span></li>";
    }).join("");
    if (edges.length > 12) {
      edgeRows += "<li>" + escapeHtml(edges.length - 12) + " more edges</li>";
    }
    return '<div class="graph-mode-toolbar">' +
      '<div><span class="eyebrow">Graph Mode</span><strong>Agent Pipeline</strong></div>' +
      '<div class="graph-mode-actions"><small>Agent runs, lifecycle events, and validation checks.</small>' +
      '<button type="button" class="graph-expand-toggle" data-graph-expand-toggle aria-pressed="false">Expand All</button></div>' +
      "</div>" +
      renderGraphOverview(groups, nodes, edges, workflowGraph) +
      '<div class="graph-pipeline-scroll" tabindex="0" aria-label="Scrollable phase pipeline">' +
      '<div class="graph-pipeline" data-graph-mode="phase_pipeline">' + phaseHtml + "</div>" +
      "</div>" +
      '<details class="graph-edge-summary"><summary><strong>Runtime Relations</strong><span>' + escapeHtml(edges.length) +
      '</span></summary><ul class="edge-list">' + edgeRows + "</ul></details>";
  }

  function renderGraphPhaseLane(group) {
    var groupStatus = text(group.status || "runtime");
    var groupTitle = renderHumanSummaryTrigger(group.title || "Workflow Events", group.human_summary || {}, "graph-summary-link");
    return '<section class="graph-phase-lane graph-phase-group" data-phase-key="' + escapeHtml(group.phase_key || "") + '" ' + statusAttributes(groupStatus) + expandedAttribute(group) + ">" +
      '<div class="graph-phase-heading"><div>' +
      "<strong>" + groupTitle + "</strong>" +
      expansionNote(group, "Phase") +
      "<small>" + escapeHtml(graphGroupSubtitle(group)) + "</small>" +
      "</div>" + statusPill(groupStatus) + "</div>" +
      '<div class="graph-task-rail">' + renderGraphTaskCards(group) + "</div>" +
      "</section>";
  }

  function renderGraphOverview(groups, nodes, edges, workflowGraph) {
    var taskCount = groups.reduce(function (total, group) {
      return total + (Array.isArray(group.tasks) ? group.tasks.length : 0);
    }, 0);
    var agentCount = nodes.filter(isGraphAgentNode).length;
    var eventCount = nodes.filter(isGraphEventNode).length;
    var eventWindow = workflowGraph && workflowGraph.event_window && typeof workflowGraph.event_window === "object" ? workflowGraph.event_window : {};
    var totalEvents = positiveInteger(eventWindow.total_events) ||
      positiveInteger(workflowGraph && workflowGraph.source_hashes && workflowGraph.source_hashes.events_count);
    var eventLabel = totalEvents && totalEvents > eventCount ? "recent events" : "events";
    var eventSuffix = totalEvents && totalEvents > eventCount ? '<small>of ' + escapeHtml(totalEvents) + "</small>" : "";
    var aggregation = workflowGraph && workflowGraph.self_expansion_aggregation && typeof workflowGraph.self_expansion_aggregation === "object" ? workflowGraph.self_expansion_aggregation : {};
    var aggregatedCount = positiveInteger(aggregation.aggregated_node_count) || 0;
    var aggregationHtml = aggregatedCount ?
      "<span><strong>" + escapeHtml(aggregatedCount) + "</strong> self-expansion aggregated</span>" :
      "";
    var checkCount = nodes.filter(isGraphCheckNode).length;
    var attentionCount = nodes.filter(function (node) {
      var tier = statusTier(node.status);
      return tier === "danger" || tier === "warning" || tier === "active";
    }).length;
    return '<div class="graph-overview" aria-label="Graph summary">' +
      "<span><strong>" + escapeHtml(groups.length) + "</strong> phases</span>" +
      "<span><strong>" + escapeHtml(taskCount) + "</strong> tasks</span>" +
      "<span><strong>" + escapeHtml(agentCount) + "</strong> agents</span>" +
      "<span><strong>" + escapeHtml(eventCount) + "</strong> " + escapeHtml(eventLabel) + eventSuffix + "</span>" +
      aggregationHtml +
      "<span><strong>" + escapeHtml(checkCount) + "</strong> checks</span>" +
      '<span data-status-tier="' + escapeHtml(attentionCount ? "warning" : "muted") + '"><strong>' + escapeHtml(attentionCount) + "</strong> attention</span>" +
      "</div>";
  }

  function graphGroupSubtitle(group) {
    var subtitle = text(group && group.subtitle || "");
    var latest = cleanText(group && group.last_activity_at);
    if (!latest) {
      return subtitle;
    }
    var latestLabel = "last " + compactTimestamp(latest);
    return subtitle ? (subtitle + " · " + latestLabel) : latestLabel;
  }

  function renderGraphTaskCards(group) {
    var nodes = Array.isArray(group.nodes) ? group.nodes : [];
    var tasks = Array.isArray(group.tasks) ? group.tasks : [];
    var usedNodeIds = {};
    var cards = [];
    var sortedTasks = tasks.slice().sort(graphTaskPriority(nodes));
    sortedTasks.forEach(function (task) {
      var taskId = text(task.task_id || "");
      var taskNodes = nodes.filter(function (node) {
        return text(node.task_id || "") === taskId;
      });
      taskNodes.forEach(function (node) {
        usedNodeIds[text(node.node_id || "")] = true;
      });
      cards.push(renderGraphTaskCard(task, taskNodes));
    });
    var unassigned = nodes.filter(function (node) {
      return !usedNodeIds[text(node.node_id || "")];
    });
    if (unassigned.length) {
      if (tasks.length) {
        cards.push(renderGraphTaskCard({
          task_id: "workflow_events",
          title: "Unassigned Runtime Events",
          status: group.status || "events"
        }, unassigned));
      } else {
        cards.push(renderGraphTaskCard({
          task_id: "workflow_events",
          title: text(group.title || "Workflow Events"),
          status: group.status || "events"
        }, unassigned));
      }
    }
    return cards.length ? cards.join("") : '<article class="graph-task-card empty"><p class="empty-state">No agent runs for this phase yet.</p></article>';
  }

  function renderGraphTaskCard(task, nodes) {
    var taskId = text(task.task_id || "task");
    var taskStatus = text(task.status || "planned");
    var timeline = renderGraphAgentTimeline(nodes, taskId);
    var timeLabel = graphTaskTimeLabel(nodes);
    var taskTitle = renderHumanSummaryTrigger(task.title || "Untitled task", task.human_summary || {}, "graph-summary-link");
    return '<article class="graph-task-card" ' + statusAttributes(taskStatus) + ' data-task-id="' + escapeHtml(taskId) + '"' + expandedAttribute(task) + ">" +
      '<div class="graph-task-heading"><div>' +
      "<strong>" + escapeHtml(taskId) + "</strong>" +
      expansionNote(task, "Task") +
      "<small>" + taskTitle + "</small>" +
      (timeLabel ? '<small class="graph-time-label">' + escapeHtml(timeLabel) + "</small>" : "") +
      "</div>" + statusPill(taskStatus) + "</div>" +
      '<div class="graph-node-stack agent-run-stack">' + timeline + "</div>" +
      "</article>";
  }

  function renderGraphAgentTimeline(nodes, taskId) {
    var records = Array.isArray(nodes) ? nodes.slice() : [];
    var agents = records.filter(isGraphAgentNode).sort(graphNodePriority);
    var secondary = records.filter(function (node) {
      return !isGraphAgentNode(node);
    });
    var used = {};
    var cards = agents.map(function (agent) {
      var related = secondary.filter(function (node) {
        return !used[cleanText(node && node.node_id)] && graphNodeRelatedToAgent(node, agent);
      });
      used[cleanText(agent && agent.node_id)] = true;
      related.forEach(function (node) {
        used[cleanText(node && node.node_id)] = true;
      });
      return renderGraphAgentCard(agent, related, taskId);
    });
    var remaining = records.filter(function (node) {
      return !used[cleanText(node && node.node_id)];
    }).sort(graphNodePriority);
    if (remaining.length) {
      cards.push(renderGraphRecordGroup(remaining, "Workflow Records", taskId));
    }
    return cards.length ? cards.join("") : '<p class="empty-state">No agent runs yet.</p>';
  }

  function renderGraphAgentCard(agent, related, taskId) {
    var checks = related.filter(isGraphCheckNode).sort(graphNodePriority);
    var events = related.filter(isGraphEventNode).sort(graphNodePriority);
    var other = related.filter(function (node) {
      return !isGraphEventNode(node) && !isGraphCheckNode(node);
    }).sort(graphNodePriority);
    var status = text(agent.status || "unknown");
    var displayAgent = graphNodeWithRelatedTime(agent, related);
    return '<section class="agent-run-card" ' + statusAttributes(status) + ">" +
      renderGraphNodeButton(displayAgent, taskId, false, "agent") +
      renderGraphRecordRow("Lifecycle", events, taskId, 6) +
      renderGraphRecordRow("Checks", checks, taskId, 6) +
      renderGraphRecordRow("Records", other, taskId, 6) +
      "</section>";
  }

  function renderGraphRecordGroup(nodes, title, taskId) {
    var status = text(nodes.length ? nodes[0].status : "events");
    return '<section class="agent-run-card graph-record-group" ' + statusAttributes(status) + ">" +
      '<div class="agent-run-group-heading"><strong>' + escapeHtml(title) + "</strong><small>" +
      escapeHtml(nodes.length + " record" + (nodes.length === 1 ? "" : "s")) + "</small></div>" +
      renderGraphRecordRow("Related", nodes, taskId, 8) +
      "</section>";
  }

  function renderGraphRecordRow(label, nodes, taskId, limit) {
    if (!Array.isArray(nodes) || nodes.length === 0) {
      return "";
    }
    var ordered = nodes.slice().sort(graphNodePriority);
    var visible = ordered.slice(0, limit);
    var hidden = ordered.slice(limit);
    var chips = visible.map(function (node) {
      return renderGraphNodeButton(node, taskId, false, graphNodeVariant(node));
    }).join("");
    if (hidden.length) {
      var moreLabel = "Show " + hidden.length + " more related record" + (hidden.length === 1 ? "" : "s");
      var lessLabel = "Hide " + hidden.length + " related record" + (hidden.length === 1 ? "" : "s");
      chips += '<button class="graph-node-more" type="button" data-graph-more="collapsed" aria-expanded="false" ' +
        'data-more-label="' + escapeHtml(moreLabel) + '" data-less-label="' + escapeHtml(lessLabel) + '">' +
        escapeHtml(moreLabel) + "</button>";
      chips += hidden.map(function (node) {
        return renderGraphNodeButton(node, taskId, true, graphNodeVariant(node));
      }).join("");
    }
    return '<div class="agent-flow-row" data-flow-kind="' + escapeHtml(label.toLowerCase()) + '">' +
      '<span class="agent-flow-label">' + escapeHtml(label) + "</span>" +
      '<div class="agent-flow-items">' + chips + "</div>" +
      "</div>";
  }

  function renderGraphNodeButton(node, taskId, hidden, variant) {
    var status = text(node.status || "unknown");
    var kind = variant || "record";
    var className = "graph-node";
    if (kind === "agent") {
      className += " agent-run-node";
    } else if (kind === "event") {
      className += " graph-event-chip";
    } else if (kind === "check") {
      className += " graph-check-chip";
    } else {
      className += " graph-record-chip";
    }
    var typeLabel = graphNodeTypeLabel(node);
    var title = graphNodeTitleLabel(node, status, kind);
    var strongLabel = kind === "agent" ? (typeLabel + " · " + humanStatusLabel(status)) : title;
    var smallLabel = graphNodeTimeLabel(node);
    return '<button class="' + className + '" type="button" data-node-id="' + escapeHtml(node.node_id) +
      '" ' + statusAttributes(status) + ' data-task-id="' + escapeHtml(node.task_id || "") + '"' +
      (hidden ? ' data-overflow-node="true" hidden' : "") + ">" +
      "<span>" + escapeHtml(typeLabel) + "</span>" +
      "<strong>" + escapeHtml(strongLabel) + "</strong>" +
      "<small>" + escapeHtml(smallLabel) + "</small>" +
      "</button>";
  }

  function graphNodeTypeLabel(node) {
    var typeValue = statusValue(node && node.type || "node");
    if (typeValue === "validation") {
      return "Validation";
    }
    if (typeValue === "event") {
      return truncateText(cleanText(node && (node.context_label || node.actor_label)) || "Event", 42);
    }
    return humanizeIdentifier(typeValue || "node");
  }

  function graphNodeTitleLabel(node, status, variant) {
    if (variant === "event") {
      var title = cleanText(node && node.title);
      if (title) {
        return truncateText(title, 72);
      }
      var sequence = node && node.event_sequence;
      var eventLabel = humanizeIdentifier(cleanText(node && node.event_type) || status || "event");
      return truncateText(sequence !== undefined && sequence !== null && sequence !== "" ? ("Event " + sequence + ": " + eventLabel) : eventLabel, 72);
    }
    if (variant === "check") {
      return "Validation · " + humanStatusLabel(status);
    }
    var title = cleanText(node && (node.title || node.task_title || node.task_id)) || status || "Node";
    if (variant === "agent") {
      return title;
    }
    return truncateText(humanizeIdentifier(title), 64);
  }

  function humanStatusLabel(value) {
    var status = statusValue(value);
    var labels = {
      needs_verification: "Verify",
      needs_expansion: "Expand",
      objective_unresolved: "Unresolved",
      pass_with_warnings: "Warnings"
    };
    return labels[status] || humanizeIdentifier(value || "unknown");
  }

  function humanizeIdentifier(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .split(/\s+/)
      .filter(Boolean)
      .map(function (part) {
        return part.length ? part.charAt(0).toUpperCase() + part.slice(1).toLowerCase() : part;
      })
      .join(" ") || "Node";
  }

  function graphNodeTimeLabel(node) {
    var started = cleanText(node && node.started_at);
    var ended = cleanText(node && node.ended_at);
    var heartbeat = cleanText(node && node.heartbeat_at);
    var active = node && (node.active === true || isActiveStatus(node.status));
    if (active && started) {
      return compactTimestamp(started) + " -> Present";
    }
    if (started && ended && started !== ended) {
      return compactTimestamp(started) + " -> " + compactTimestamp(ended);
    }
    if (started) {
      return "Started " + compactTimestamp(started);
    }
    if (ended) {
      return "Ended " + compactTimestamp(ended);
    }
    if (heartbeat) {
      return "Heartbeat " + compactTimestamp(heartbeat);
    }
    return "Time pending";
  }

  function graphNodeWithRelatedTime(agent, related) {
    if (agent && (cleanText(agent.started_at) || cleanText(agent.ended_at))) {
      return agent;
    }
    var timestamps = [];
    [agent].concat(Array.isArray(related) ? related : []).forEach(function (node) {
      ["started_at", "ended_at", "heartbeat_at", "ts", "timestamp", "created_at"].forEach(function (key) {
        var parsed = parseTimestamp(node && node[key]);
        if (parsed !== null) {
          timestamps.push(parsed);
        }
      });
    });
    if (!timestamps.length) {
      return agent;
    }
    var copy = Object.assign({}, agent || {});
    var active = copy.active === true || isActiveStatus(copy.status);
    copy.started_at = copy.started_at || new Date(Math.min.apply(Math, timestamps)).toISOString();
    if (!copy.ended_at && !active) {
      copy.ended_at = new Date(Math.max.apply(Math, timestamps)).toISOString();
    }
    return copy;
  }

  function graphTaskTimeLabel(nodes) {
    var timestamps = [];
    var active = false;
    (Array.isArray(nodes) ? nodes : []).forEach(function (node) {
      active = active || (node && (node.active === true || isActiveStatus(node.status)));
      ["started_at", "ended_at", "heartbeat_at"].forEach(function (key) {
        var ms = parseTimestamp(node && node[key]);
        if (ms !== null) {
          timestamps.push(ms);
        }
      });
    });
    if (!timestamps.length) {
      return "";
    }
    var start = Math.min.apply(null, timestamps);
    var end = Math.max.apply(null, timestamps);
    if (active) {
      return compactTimestamp(start) + " -> Present";
    }
    if (start === end) {
      return "At " + compactTimestamp(start);
    }
    return compactTimestamp(start) + " -> " + compactTimestamp(end);
  }

  function graphNodeVariant(node) {
    if (isGraphEventNode(node)) {
      return "event";
    }
    if (isGraphCheckNode(node)) {
      return "check";
    }
    return "record";
  }

  function isGraphEventNode(node) {
    return statusValue(node && node.type) === "event";
  }

  function isGraphCheckNode(node) {
    return statusValue(node && node.type) === "validation";
  }

  function isGraphAgentNode(node) {
    var typeValue = statusValue(node && node.type);
    return Boolean(node && node.run_id && typeValue !== "event" && typeValue !== "validation");
  }

  function graphNodeRelatedToAgent(node, agent) {
    var nodeRunId = cleanText(node && node.run_id);
    var agentRunId = cleanText(agent && agent.run_id);
    if (agentRunId) {
      return Boolean(nodeRunId && nodeRunId === agentRunId);
    }
    var nodeTaskId = cleanText(node && node.task_id);
    var agentTaskId = cleanText(agent && agent.task_id);
    return Boolean(nodeTaskId && agentTaskId && nodeTaskId === agentTaskId);
  }

  function truncateText(value, limit) {
    var textValue = String(value || "");
    if (textValue.length <= limit) {
      return textValue;
    }
    return textValue.slice(0, Math.max(0, limit - 3)).trim() + "...";
  }

  function graphNodePriority(left, right) {
    var bucketCompare = graphNodeBucket(left) - graphNodeBucket(right);
    if (bucketCompare) {
      return bucketCompare;
    }
    var timeCompare = compareGraphTimeDesc(latestGraphNodeTime(left), latestGraphNodeTime(right));
    if (timeCompare) {
      return timeCompare;
    }
    return text(left.node_id || "").localeCompare(text(right.node_id || ""));
  }

  function latestGraphNodeTime(node) {
    return latestGraphMappingTime(node);
  }

  function latestGraphMappingTime(value) {
    var timestamps = [];
    [
      "ended_at",
      "completed_at",
      "finished_at",
      "validated_at",
      "last_updated_at",
      "updated_at",
      "heartbeat_at",
      "started_at",
      "prepared_at",
      "ts",
      "timestamp",
      "generated_at",
      "created_at"
    ].forEach(function (key) {
      var raw = value && value[key];
      var parsed = parseTimestamp(raw);
      if (parsed !== null) {
        timestamps.push(parsed);
      }
    });
    return timestamps.length ? timestampIso(Math.max.apply(null, timestamps)) : "";
  }

  function compareGraphTimeDesc(leftTime, rightTime) {
    if (leftTime && !rightTime) {
      return -1;
    }
    if (!leftTime && rightTime) {
      return 1;
    }
    if (leftTime !== rightTime) {
      return rightTime.localeCompare(leftTime);
    }
    return 0;
  }

  function graphTaskPriority(nodes) {
    return function (left, right) {
      var bucketCompare = graphTaskBucket(left, nodes) - graphTaskBucket(right, nodes);
      if (bucketCompare) {
        return bucketCompare;
      }
      var timeCompare = compareGraphTimeDesc(graphTaskLatestTime(left, nodes), graphTaskLatestTime(right, nodes));
      if (timeCompare) {
        return timeCompare;
      }
      var leftOrder = Number.isFinite(Number(left && left.order_index)) ? Number(left.order_index) : 0;
      var rightOrder = Number.isFinite(Number(right && right.order_index)) ? Number(right.order_index) : 0;
      if (leftOrder !== rightOrder) {
        return leftOrder - rightOrder;
      }
      return text(left && left.task_id || "").localeCompare(text(right && right.task_id || ""));
    };
  }

  function graphTaskLatestTime(task, nodes) {
    var taskId = text(task && task.task_id || "");
    var timestamps = [];
    (Array.isArray(nodes) ? nodes : []).forEach(function (node) {
      if (text(node && node.task_id || "") === taskId) {
        var nodeTime = latestGraphNodeTime(node);
        if (nodeTime) {
          timestamps.push(nodeTime);
        }
      }
    });
    var ownTime = latestGraphMappingTime(task || {});
    if (ownTime) {
      timestamps.push(ownTime);
    }
    return timestamps.length ? timestamps.sort().pop() : "";
  }

  function graphGroupPriority(left, right) {
    var leftBucket = graphGroupBucket(left);
    var rightBucket = graphGroupBucket(right);
    if (leftBucket !== rightBucket) {
      return leftBucket - rightBucket;
    }
    var leftOrder = Number.isFinite(Number(left && left.order_index)) ? Number(left.order_index) : 0;
    var rightOrder = Number.isFinite(Number(right && right.order_index)) ? Number(right.order_index) : 0;
    if (leftBucket === 1) {
      if (leftOrder !== rightOrder) {
        return leftOrder - rightOrder;
      }
      var pendingNatural = naturalCompare(graphGroupLabel(left), graphGroupLabel(right));
      if (pendingNatural) {
        return pendingNatural;
      }
      return text(left && left.phase_key || "").localeCompare(text(right && right.phase_key || ""));
    }
    var timeCompare = compareGraphTimeDesc(graphGroupActivityTime(left), graphGroupActivityTime(right));
    if (timeCompare) {
      return timeCompare;
    }
    var natural = naturalCompare(graphGroupLabel(left), graphGroupLabel(right));
    if (natural) {
      return natural;
    }
    if (leftOrder !== rightOrder) {
      return leftOrder - rightOrder;
    }
    return text(left && left.phase_key || "").localeCompare(text(right && right.phase_key || ""));
  }

  function graphGroupBucket(group) {
    if (graphGroupHasActiveWork(group)) {
      return 0;
    }
    if (statusTier(group && group.status) === "success") {
      return 2;
    }
    if (graphGroupAllWorkSuccess(group)) {
      return 2;
    }
    return 1;
  }

  function graphGroupHasActiveWork(group) {
    if (statusBucket(group && group.status) === 0) {
      return true;
    }
    var tasks = Array.isArray(group && group.tasks) ? group.tasks : [];
    for (var taskIndex = 0; taskIndex < tasks.length; taskIndex += 1) {
      if (statusBucket(tasks[taskIndex] && tasks[taskIndex].status) === 0) {
        return true;
      }
    }
    var nodes = Array.isArray(group && group.nodes) ? group.nodes : [];
    for (var nodeIndex = 0; nodeIndex < nodes.length; nodeIndex += 1) {
      if (graphNodeBucket(nodes[nodeIndex]) === 0) {
        return true;
      }
    }
    return false;
  }

  function graphGroupAllWorkSuccess(group) {
    var tasks = Array.isArray(group && group.tasks) ? group.tasks : [];
    if (tasks.length) {
      return tasks.every(function (task) {
        return statusTier(task && task.status) === "success";
      });
    }
    var nodes = (Array.isArray(group && group.nodes) ? group.nodes : []).filter(isGraphAgentNode);
    return nodes.length > 0 && nodes.every(function (node) {
      return statusTier(node && node.status) === "success";
    });
  }

  function graphTaskBucket(task, nodes) {
    var taskId = text(task && task.task_id || "");
    var bucket = statusBucket(task && task.status);
    (Array.isArray(nodes) ? nodes : []).forEach(function (node) {
      if (text(node && node.task_id || "") === taskId) {
        bucket = Math.min(bucket, graphNodeBucket(node));
      }
    });
    return bucket;
  }

  function graphNodeBucket(node) {
    return statusBucket(node && node.status);
  }

  function statusBucket(status) {
    var tier = statusTier(status);
    if (tier === "active") {
      return 0;
    }
    if (tier === "success") {
      return 2;
    }
    return 1;
  }

  function graphGroupActivityTime(group) {
    return cleanText(group && group.last_activity_at) || graphGroupLatestTime(group);
  }

  function graphGroupLabel(group) {
    return text(group && (group.phase_key || group.title) || "");
  }

  function naturalCompare(left, right) {
    var leftParts = String(left || "").toLowerCase().split(/(\d+)/).filter(Boolean);
    var rightParts = String(right || "").toLowerCase().split(/(\d+)/).filter(Boolean);
    var length = Math.max(leftParts.length, rightParts.length);
    for (var index = 0; index < length; index += 1) {
      if (index >= leftParts.length) {
        return -1;
      }
      if (index >= rightParts.length) {
        return 1;
      }
      var leftPart = leftParts[index];
      var rightPart = rightParts[index];
      var leftNumber = /^\d+$/.test(leftPart) ? Number(leftPart) : null;
      var rightNumber = /^\d+$/.test(rightPart) ? Number(rightPart) : null;
      if (leftNumber !== null && rightNumber !== null && leftNumber !== rightNumber) {
        return leftNumber - rightNumber;
      }
      if (leftNumber !== null && rightNumber === null) {
        return -1;
      }
      if (leftNumber === null && rightNumber !== null) {
        return 1;
      }
      var textCompare = leftPart.localeCompare(rightPart);
      if (textCompare) {
        return textCompare;
      }
    }
    return 0;
  }

  function graphGroupLatestTime(group) {
    var timestamps = [];
    (Array.isArray(group && group.nodes) ? group.nodes : []).forEach(function (node) {
      var nodeTime = latestGraphNodeTime(node);
      if (nodeTime) {
        timestamps.push(nodeTime);
      }
    });
    (Array.isArray(group && group.tasks) ? group.tasks : []).forEach(function (task) {
      var taskTime = latestGraphMappingTime(task);
      if (taskTime) {
        timestamps.push(taskTime);
      }
    });
    return timestamps.length ? timestamps.sort().pop() : "";
  }

  function graphPhaseGroups(planIndex, nodes) {
    var phases = planIndex && Array.isArray(planIndex.phases) ? planIndex.phases : [];
    var groups = [];
    var taskToGroup = {};
    var phaseToGroup = {};
    phases.forEach(function (phase, index) {
      var tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
      var taskIds = [];
      var group = {
        phase_key: text(phase.phase_id || phase.id || phase.title || ("phase_" + (index + 1))),
        title: text(phase.title || ("Phase " + (index + 1))),
        subtitle: "",
        status: text(phase.status || "planned"),
        human_summary: phase.human_summary && typeof phase.human_summary === "object" ? phase.human_summary : {},
        expanded: phase.expanded === true,
        expansion: phase.expansion && typeof phase.expansion === "object" ? phase.expansion : null,
        expansion_marker: phase.expansion_marker || "",
        order_index: index,
        task_ids: taskIds,
        tasks: [],
        nodes: []
      };
      tasks.forEach(function (task, taskIndex) {
        var taskId = text(task.task_id || "");
        if (!taskId) {
          return;
        }
        taskIds.push(taskId);
        group.tasks.push({
          task_id: taskId,
          title: text(task.title || "Untitled task"),
          status: text(task.status || "planned"),
          deliverables: task.deliverables || "",
          last_updated_at: text(task.last_updated_at || ""),
          started_at: text(task.started_at || ""),
          ended_at: text(task.ended_at || ""),
          completed_at: text(task.completed_at || ""),
          validated_at: text(task.validated_at || ""),
          human_summary: task.human_summary && typeof task.human_summary === "object" ? task.human_summary : {},
          expanded: task.expanded === true,
          expansion: task.expansion && typeof task.expansion === "object" ? task.expansion : null,
          expansion_marker: task.expansion_marker || "",
          order_index: taskIndex
        });
        taskToGroup[taskId] = group;
      });
      group.subtitle = taskIds.length + " task" + (taskIds.length === 1 ? "" : "s");
      groups.push(group);
      [group.phase_key, phase.id, phase.title].forEach(function (key) {
        var value = cleanText(key || "");
        if (value) {
          phaseToGroup[value] = group;
        }
      });
    });
    var eventGroup = {
      phase_key: "workflow_events",
      title: "Workflow Events",
      subtitle: "Unassigned runtime and lifecycle nodes",
      status: "events",
      order_index: groups.length,
      task_ids: [],
      nodes: []
    };
    nodes.forEach(function (node) {
      var taskId = text(node.task_id || "");
      var group = taskId ? taskToGroup[taskId] : null;
      if (!group) {
        [node.phase_id, node.objective_phase_id, node.phase].some(function (key) {
          var value = cleanText(key || "");
          if (value && phaseToGroup[value]) {
            group = phaseToGroup[value];
            return true;
          }
          return false;
        });
      }
      if (!group) {
        group = eventGroup;
      }
      group.nodes.push(node);
    });
    groups.concat([eventGroup]).forEach(function (group) {
      group.last_activity_at = graphGroupLatestTime(group);
    });
    var visible = groups.filter(function (group) {
      return group.nodes.length || group.task_ids.length;
    });
    if (eventGroup.nodes.length) {
      visible.push(eventGroup);
    }
    return visible.length ? visible.sort(graphGroupPriority) : [eventGroup];
  }

  function renderActivityFeed(feed, payload) {
    var scopeNote = activityFeedScopeNote(feed, payload);
    if (!Array.isArray(feed) || feed.length === 0) {
      return scopeNote + '<p class="empty-state">No activity records are present.</p>';
    }
    return scopeNote + '<ol class="feed-list">' + feed.slice(-25).map(function (item) {
      var severity = text(item.severity || "info");
      return '<li class="feed-row" data-severity="' + escapeHtml(severity) + '">' +
        "<time>" + escapeHtml(item.ts || item.generated_at || "") + "</time>" +
        "<strong>" + escapeHtml(item.event || "event") + "</strong>" +
        "<span>" + escapeHtml(item.message || "") + "</span>" +
        "</li>";
    }).join("") + "</ol>";
  }

  function activityFeedScopeNote(feed, payload) {
    var rows = Array.isArray(feed) ? feed.length : 0;
    var visibleRows = Math.min(25, rows);
    var maxEvents = positiveInteger(payload && payload.max_dashboard_events) || rows;
    var freshness = payload && payload.read_model_freshness && typeof payload.read_model_freshness === "object" ? payload.read_model_freshness : {};
    var eventLog = freshness.event_log && typeof freshness.event_log === "object" ? freshness.event_log : {};
    var totalEvents = positiveInteger(eventLog.events_count);
    var message = totalEvents && totalEvents > maxEvents ?
      ("Read model contains the most recent " + rows + " of " + totalEvents + " events (configured max " + maxEvents + "); this panel shows the newest " + visibleRows + " rows.") :
      ("Read model contains " + rows + " event records; this panel shows the newest " + visibleRows + " rows.");
    return '<p class="selector-status">' + escapeHtml(message) + "</p>";
  }

  function positiveInteger(value) {
    var numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 1) {
      return null;
    }
    return Math.floor(numeric);
  }

  function renderGitStatus(versionControl) {
    var repository = versionControl.repository && typeof versionControl.repository === "object" ? versionControl.repository : {};
    var checkpoint = versionControl.latest_checkpoint && typeof versionControl.latest_checkpoint === "object" ? versionControl.latest_checkpoint : null;
    var checkpointHtml = checkpoint ? '<dl class="detail-list">' +
      row("Checkpoint", checkpoint.checkpoint_id || checkpoint.id) +
      row("Reason", checkpoint.reason) +
      row("Created", checkpoint.created_at) +
      "</dl>" : '<p class="empty-state">No checkpoint has been recorded in the read model.</p>';
    return '<div class="git-grid">' +
      '<dl class="detail-list">' +
      row("Status", versionControl.status) +
      row("Provider", versionControl.provider) +
      row("Git Available", versionControl.git_available) +
      row("Repo Dirty", repository.dirty) +
      row("Dirty Files", repository.dirty_files_count) +
      row("Head Commit", repository.head_commit) +
      row("Problem", versionControl.problem) +
      "</dl>" +
      checkpointHtml +
      "</div>";
  }

  function renderFreshnessBanner(payload) {
    var freshness = payload && payload.read_model_freshness && typeof payload.read_model_freshness === "object" ? payload.read_model_freshness : {};
    var rebuild = payload && payload.read_model_rebuild && typeof payload.read_model_rebuild === "object" ? payload.read_model_rebuild : {};
    var status = text(freshness.status || "");
    if (status !== "stale" && status !== "unknown") {
      return "";
    }
    var warnings = Array.isArray(freshness.warnings) ? freshness.warnings : [];
    var files = warnings.filter(function (warning) {
      return warning && (warning.code === "read_model_stale" || warning.code === "read_model_metadata_missing") && warning.file;
    }).map(function (warning) {
      return warning.file;
    });
    var fileLabel = files.slice(0, 5).join(", ");
    if (files.length > 5) {
      fileLabel += ", and " + String(files.length - 5) + " more";
    }
    var rebuildInProgress = readModelRebuildInProgress(rebuild);
    var liveRefreshExpected = readModelLiveRefreshExpected(payload, rebuild);
    var eyebrow = "Freshness Warning";
    var title = status === "stale" ? "Read Models May Be Stale" : "Read Model Freshness Needs Rebuild";
    var summary = freshness.summary || "Read models require a rebuild before dashboard status can be trusted.";
    var ariaLabel = "Read model freshness warning";
    if (rebuildInProgress) {
      eyebrow = "Rebuild In Progress";
      title = "Read Models Are Rebuilding";
      summary = rebuild.in_progress_summary || "A read-model rebuild is already queued or running. Dashboard data may lag until it finishes; refresh shortly.";
      ariaLabel = "Read model rebuild in progress";
    } else if (liveRefreshExpected) {
      eyebrow = "Live Refresh In Progress";
      title = "Read Models Are Refreshing";
      summary = "The live dashboard is rebuilding its derived read models now. This page will update automatically when the refresh finishes.";
      ariaLabel = "Read model live refresh in progress";
    }
    return '<section class="freshness-banner" data-freshness-status="' + escapeHtml(status) +
      '" data-rebuild-in-progress="' + String(rebuildInProgress) + '" role="status" aria-label="' + escapeHtml(ariaLabel) + '">' +
      "<div>" +
      '<p class="eyebrow">' + escapeHtml(eyebrow) + "</p>" +
      "<h2>" + escapeHtml(title) + "</h2>" +
      "<p>" + escapeHtml(summary) + "</p>" +
      '<dl class="freshness-facts">' +
      row("Read Model Event", eventRefLabel(freshness.read_model || {})) +
      row("Event Log Head", eventRefLabel(freshness.event_log || {})) +
      row("Affected Files", fileLabel || "unknown") +
      "</dl>" +
      "</div>" +
      '<div class="rebuild-hint">' +
      "<span>Rebuild command</span>" +
      "<code>" + escapeHtml(freshness.rebuild_command || "loopplane rebuild-read-models") + "</code>" +
      renderReadModelRebuildForm(payload, rebuild) +
      "</div>" +
      "</section>";
  }

  function renderReadModelRebuildForm(payload, rebuild) {
    var blockers = Array.isArray(rebuild.mutation_blockers) ? rebuild.mutation_blockers : [];
    var mutationAllowed = rebuild.mutation_allowed === true;
    var rebuildInProgress = readModelRebuildInProgress(rebuild);
    var liveRefreshExpected = readModelLiveRefreshExpected(payload, rebuild);
    var requestAllowed = rebuild.request_allowed !== false;
    var disabled = !payload.server_mode || !mutationAllowed || !requestAllowed || rebuildInProgress || liveRefreshExpected;
    var disabledAttr = disabled ? " disabled" : "";
    var buttonLabel = "Create Rebuild Request";
    if (rebuildInProgress) {
      buttonLabel = "Rebuild Request Pending";
    } else if (liveRefreshExpected) {
      buttonLabel = "Live Refresh Running";
    }
    var commands = Array.isArray(rebuild.commands) ? rebuild.commands : [];
    var recent = Array.isArray(rebuild.recent) ? rebuild.recent : [];
    var rows = recent.slice(-4).map(function (record) {
      return statusFeedRow(record.type || "rebuild_read_models", record.status || "pending", record.request_id || "");
    }).join("");
    if (!rows) {
      rows = "<li><span>No rebuild requests recorded.</span></li>";
    }
    return '<form id="read-model-rebuild-form" class="rebuild-request-form" data-endpoint="' + escapeHtml(rebuild.endpoint || "") + '">' +
      '<label>Reason<input name="reason" value=""' + disabledAttr + "></label>" +
      '<button type="submit" data-rebuild-action="rebuild_read_models"' + disabledAttr + ">" + escapeHtml(buttonLabel) + "</button>" +
      '<p id="read-model-rebuild-status" class="selector-status" role="status">' +
      escapeHtml(readModelRebuildStatusMessage(payload.server_mode, mutationAllowed, blockers, rebuildInProgress, liveRefreshExpected)) + "</p>" +
      '<dl class="detail-list compact-detail-list">' +
      row("Pending", rebuild.pending_count || 0) +
      row("Latest", latestRebuildRequestLabel(rebuild)) +
      row("Request Path", rebuild.requests_path) +
      "</dl>" +
      '<div class="attention-commands">' + commands.slice(1, 2).map(function (command) {
        return "<code>" + escapeHtml(command) + "</code>";
      }).join("") + "</div>" +
      '<ol class="feed-list compact-feed">' + rows + "</ol>" +
      "</form>";
  }

  function readModelRebuildStatusMessage(serverMode, mutationAllowed, blockers, rebuildInProgress, liveRefreshExpected) {
    if (rebuildInProgress) {
      return "A read-model rebuild is already queued or running. Wait for the runtime to finish, then refresh the dashboard.";
    }
    if (liveRefreshExpected) {
      return "The live dashboard is refreshing read models now. Wait for the page to update before requesting another rebuild.";
    }
    if (!serverMode) {
      return "Static dashboard is read-only; open server mode to create a rebuild request record.";
    }
    if (!mutationAllowed) {
      return "Read-model rebuild requests are disabled for this workflow: " + (blockers.length ? blockers.join(", ") : "not mutable") + ".";
    }
    return "This records a read-model rebuild request. Dashboard data will update after the runtime processes it.";
  }

  function readModelRebuildInProgress(rebuild) {
    if (!rebuild || typeof rebuild !== "object") {
      return false;
    }
    if (Number(rebuild.pending_count || 0) > 0) {
      return true;
    }
    if (readModelRebuildStatusActive(rebuild.latest_status)) {
      return true;
    }
    var recent = Array.isArray(rebuild.recent) ? rebuild.recent : [];
    return recent.some(function (record) {
      return readModelRebuildStatusActive(record && record.status, "pending");
    });
  }

  function readModelRebuildStatusActive(value, fallback) {
    var status = text(value || fallback || "").trim().toLowerCase().replace(/-/g, "_");
    return ["accepted", "in_progress", "pending", "processing", "queued", "requested", "running", "started"].indexOf(status) !== -1;
  }

  function readModelLiveRefreshExpected(payload, rebuild) {
    return Boolean(
      payload &&
      payload.server_mode &&
      (
        payload.read_model_live_refresh_expected === true ||
        (rebuild && rebuild.live_refresh_expected === true)
      )
    );
  }

  function latestRebuildRequestLabel(rebuild) {
    if (!rebuild.latest_request_id) {
      return "none";
    }
    return ["rebuild_read_models", rebuild.latest_status, rebuild.latest_request_id].filter(Boolean).join(" ");
  }

  function renderControlPanel(payload, workflowStatus) {
    var planningControls = payload && payload.planning_controls && typeof payload.planning_controls === "object" ? payload.planning_controls : {};
    var executionControls = payload && payload.execution_controls && typeof payload.execution_controls === "object" ? payload.execution_controls : {};
    var control = Object.keys(executionControls).length ? executionControls : (workflowStatus.control && typeof workflowStatus.control === "object" ? workflowStatus.control : {});
    var recent = Array.isArray(control.recent) ? control.recent : [];
    var rows = recent.slice(-6).map(function (record) {
      return statusFeedRow(record.type || "control", record.status || "unknown", record.request_id || "");
    }).join("");
    if (!rows) {
      rows = "<li><span>No control requests recorded.</span></li>";
    }
    return '<div class="control-stack">' +
      renderPlanningControls(payload, planningControls) +
      renderExecutionControls(payload, control, rows) +
      "</div>";
  }

  function renderExecutionControls(payload, executionControls, rows) {
    var endpoints = executionControls.endpoints && typeof executionControls.endpoints === "object" ? executionControls.endpoints : {};
    var blockers = Array.isArray(executionControls.mutation_blockers) ? executionControls.mutation_blockers : [];
    var mutationAllowed = executionControls.mutation_allowed === true;
    var disabled = !payload.server_mode || !mutationAllowed;
    var disabledAttr = disabled ? " disabled" : "";
    var commands = Array.isArray(executionControls.commands) ? executionControls.commands : [];
    function actionButton(action, label) {
      return '<button type="submit" data-control-action="' + escapeHtml(action) + '" data-endpoint="' + escapeHtml(endpoints[action] || "") + '"' + disabledAttr + ">" + escapeHtml(label) + "</button>";
    }
    return '<div class="execution-controls" data-mutation-allowed="' + escapeHtml(String(mutationAllowed)) + '">' +
      "<h3>Execution Requests</h3>" +
      '<p class="selector-status" id="execution-control-status" role="status">' +
      escapeHtml(executionControlStatusMessage(payload.server_mode, mutationAllowed, blockers)) + "</p>" +
      '<form id="execution-control-form" class="execution-control-form">' +
      '<div class="form-grid">' +
      '<label>Reason<input name="reason" value=""' + disabledAttr + "></label>" +
      "</div>" +
      '<div class="execution-action-grid">' +
      actionButton("start", "Start") +
      actionButton("pause", "Pause") +
      actionButton("resume", "Resume") +
      actionButton("stop", "Stop") +
      "</div>" +
      "</form>" +
      '<dl class="detail-list">' +
      row("Runtime", executionControls.runtime_status || executionControls.status) +
      row("Pending", executionControls.pending_count || 0) +
      row("Applied", executionControls.applied_count || 0) +
      row("Rejected", executionControls.rejected_count || 0) +
      row("Latest", latestControlLabel(executionControls)) +
      row("Request Path", executionControls.requests_path) +
      "</dl>" +
      '<div class="attention-commands">' + commands.slice(0, 8).map(function (command) {
        return "<code>" + escapeHtml(command) + "</code>";
      }).join("") + "</div>" +
      '<ol class="feed-list compact-feed">' + rows + "</ol>" +
      "</div>";
  }

  function executionControlStatusMessage(serverMode, mutationAllowed, blockers) {
    if (!serverMode) {
      return "Static dashboard is read-only; open server mode to create start, pause, resume, or stop request records.";
    }
    if (!mutationAllowed) {
      return "Execution controls are disabled for this workflow: " + (blockers.length ? blockers.join(", ") : "not mutable") + ".";
    }
    return "Creates start, pause, resume, and stop request records only; the scheduler or detached supervisor applies them at safe points.";
  }

  function renderPlanningControls(payload, planningControls) {
    var endpoints = planningControls.endpoints && typeof planningControls.endpoints === "object" ? planningControls.endpoints : {};
    var recent = Array.isArray(planningControls.recent) ? planningControls.recent : [];
    var blockers = Array.isArray(planningControls.mutation_blockers) ? planningControls.mutation_blockers : [];
    var mutationAllowed = planningControls.mutation_allowed === true;
    var disabled = !payload.server_mode || !mutationAllowed;
    var disabledAttr = disabled ? " disabled" : "";
    var rows = recent.slice(-6).map(function (record) {
      return statusFeedRow(planningRequestLabel(record.type), record.status || "pending", record.request_id || "");
    }).join("");
    if (!rows) {
      rows = "<li><span>No planning requests recorded.</span></li>";
    }
    return '<div class="planning-controls" data-mutation-allowed="' + escapeHtml(String(mutationAllowed)) + '">' +
      "<h3>Planning Controls</h3>" +
      '<p class="selector-status" id="planning-control-status" role="status">' +
      escapeHtml(planningControlStatusMessage(payload.server_mode, mutationAllowed, blockers)) + "</p>" +
      '<form id="planning-control-form" class="planning-control-form">' +
      '<div class="form-grid">' +
      '<label>Planner Runner<input name="planner_runner_id" value="planner"' + disabledAttr + "></label>" +
      '<label>Auditor Runner<input name="auditor_runner_id" value="auditor"' + disabledAttr + "></label>" +
      '<label>Activation Source<input name="activation_source" value="PLAN_DRAFT.md"' + disabledAttr + "></label>" +
      '<label>Reason<input name="reason" value=""' + disabledAttr + "></label>" +
      "</div>" +
      '<div class="planning-action-grid">' +
      '<button type="submit" data-planning-action="plan" data-endpoint="' + escapeHtml(endpoints.plan || "") + '"' + disabledAttr + ">Run Planner</button>" +
      '<button type="submit" data-planning-action="audit" data-endpoint="' + escapeHtml(endpoints.audit || "") + '"' + disabledAttr + ">Run Auditor</button>" +
      '<button type="submit" data-planning-action="activate_plan" data-endpoint="' + escapeHtml(endpoints.activate_plan || "") + '"' + disabledAttr + ">Activate Plan</button>" +
      "</div>" +
      "</form>" +
      '<dl class="detail-list">' +
      row("Pending", planningControls.pending_count || 0) +
      row("Latest", latestPlanningLabel(planningControls)) +
      row("Request Path", planningControls.requests_path) +
      "</dl>" +
      '<ol class="feed-list compact-feed">' + rows + "</ol>" +
      "</div>";
  }

  function planningControlStatusMessage(serverMode, mutationAllowed, blockers) {
    if (!serverMode) {
      return "Static dashboard is read-only; open server mode to create planner, auditor, or activation request records.";
    }
    if (!mutationAllowed) {
      return "Planning controls are disabled for this workflow: " + (blockers.length ? blockers.join(", ") : "not mutable") + ".";
    }
    return "Creates dashboard request records only; planner, auditor, and activation work is applied by LoopPlane runtime commands.";
  }

  function planningRequestLabel(value) {
    if (value === "plan") {
      return "planner";
    }
    if (value === "audit") {
      return "auditor";
    }
    if (value === "activate_plan") {
      return "activate plan";
    }
    return text(value || "planning");
  }

  function latestPlanningLabel(planningControls) {
    if (!planningControls.latest_request_id) {
      return "none";
    }
    return [planningRequestLabel(planningControls.latest_type), planningControls.latest_status, planningControls.latest_request_id].filter(Boolean).join(" ");
  }

  function renderRunnerPanel(payload) {
    var runnerConfiguration = payload.runner_configuration && typeof payload.runner_configuration === "object" ? payload.runner_configuration : {};
    var runners = Array.isArray(runnerConfiguration.runners) ? runnerConfiguration.runners : [];
    var trustedLocal = runnerConfiguration.trusted_local_mode === true;
    if (runnerConfiguration.ok === false) {
      var errors = Array.isArray(runnerConfiguration.errors) ? runnerConfiguration.errors : [];
      return '<div class="runner-summary" data-trusted-local="false"><p class="empty-state">' +
        escapeHtml(errors.join("; ") || "Runner configuration is unavailable.") + "</p></div>";
    }
    var rows = runners.map(renderRunnerCard).join("");
    if (!rows) {
      rows = '<p class="empty-state">No configured runners were found.</p>';
    }
    var note = "";
    if (trustedLocal && !payload.server_mode) {
      note = '<p class="selector-status">Trusted local mode is enabled; open the server dashboard to apply runner settings.</p>';
    } else if (!trustedLocal) {
      note = '<p class="selector-status">Trusted local mode is disabled; commands are hidden and browser configuration changes are unavailable.</p>';
    }
    return '<div class="runner-summary" data-trusted-local="' + escapeHtml(String(trustedLocal)) + '">' +
      '<dl class="detail-list">' +
      row("Trusted Local", trustedLocal ? "enabled" : "disabled") +
      row("Default", runnerConfiguration.default_runner) +
      row("Runners", runnerConfiguration.runner_count || runners.length) +
      row("Config", runnerConfiguration.config_path) +
      "</dl>" +
      note +
      '<div class="runner-list">' + rows + "</div>" +
      (trustedLocal && payload.server_mode ? renderRunnerForm(runners, runnerConfiguration.default_runner) : "") +
      "</div>";
  }

  function renderRunnerCard(runner) {
    var doctor = runner.doctor && typeof runner.doctor === "object" ? runner.doctor : {};
    var diagnostics = Array.isArray(doctor.diagnostics) ? doctor.diagnostics : [];
    var diagnosticHtml = diagnostics.slice(0, 4).map(function (item) {
      return "<li>" + escapeHtml(item) + "</li>";
    }).join("");
    var runnerStatus = runner.enabled === true ? "enabled" : "disabled";
    return '<article class="runner-card" data-runner-id="' + escapeHtml(runner.runner_id) + '" ' + statusAttributes(runnerStatus) + ">" +
      '<div class="runner-card-heading">' +
      "<strong>" + escapeHtml(runner.runner_id || "runner") + "</strong>" +
      statusPill(runnerStatus) +
      "</div>" +
      '<dl class="detail-list">' +
      row("Role", runner.role) +
      row("Adapter", runner.adapter) +
      row("Command", runner.command) +
      row("Model", runner.model || "default") +
      row("Effort", runner.reasoning_effort || "default") +
      row("Prompt", runner.prompt_delivery_mode) +
      row("Timeout", runnerTimeoutLabel(runner.timeout_seconds)) +
      row("Doctor", doctor.status) +
      "</dl>" +
      '<ul class="runner-diagnostics">' + diagnosticHtml + "</ul>" +
      "</article>";
  }

  function renderRunnerForm(runners, defaultRunner) {
    if (!Array.isArray(runners) || runners.length === 0) {
      return "";
    }
    var selected = runners.find(function (runner) {
      return text(runner && runner.runner_id) === text(defaultRunner || "");
    }) || runners[0] || {};
    var selectedRunnerId = text(selected.runner_id || "");
    var options = runners.map(function (runner) {
      var runnerId = text(runner && runner.runner_id || "");
      var selectedAttr = runnerId === selectedRunnerId ? " selected" : "";
      return '<option value="' + escapeHtml(runnerId) + '"' + selectedAttr + ">" + escapeHtml(runnerId) + "</option>";
    }).join("");
    return '<form id="runner-config-request-form" class="runner-config-form">' +
      '<div class="form-grid">' +
      '<label>Runner<select name="runner_id">' + options + "</select></label>" +
      '<label>Role<input name="role" value="' + escapeHtml(selected.role || "") + '"></label>' +
      '<label>Adapter<input name="adapter" value="' + escapeHtml(selected.adapter || "") + '"></label>' +
      '<label>Command<input name="command" value=""></label>' +
      '<label>Model<input name="model" value="' + escapeHtml(selected.model || "") + '" placeholder="default"></label>' +
      '<label>Effort<select name="reasoning_effort">' + reasoningEffortOptions(selected.reasoning_effort) + "</select></label>" +
      '<label>Prompt<select name="prompt_delivery_mode">' + promptDeliveryOptions(selected.prompt_delivery_mode) + "</select></label>" +
      '<label>Timeout<input name="timeout_seconds" type="number" min="1" value="' + escapeHtml(selected.timeout_seconds || "") + '"></label>' +
      "</div>" +
      '<button type="submit">Apply Runner Settings</button>' +
      '<p id="runner-config-request-status" class="selector-status" role="status"></p>' +
      "</form>";
  }

  function promptDeliveryOptions(selected) {
    return ["custom_adapter", "file_argument", "interactive_terminal", "stdin", "stdin_or_prompt_flag"].map(function (mode) {
      return '<option value="' + escapeHtml(mode) + '"' + (mode === selected ? " selected" : "") + ">" + escapeHtml(mode) + "</option>";
    }).join("");
  }

  function reasoningEffortOptions(selected) {
    return [
      ["", "default"],
      ["low", "low"],
      ["medium", "medium"],
      ["high", "high"],
      ["xhigh", "xhigh"]
    ].map(function (item) {
      return '<option value="' + escapeHtml(item[0]) + '"' + (item[0] === selected ? " selected" : "") + ">" + escapeHtml(item[1]) + "</option>";
    }).join("");
  }

  function runnerTimeoutLabel(value) {
    var seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      return value;
    }
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) {
      return String(seconds) + "s (" + hours + "h" + (minutes ? " " + minutes + "m" : "") + " safety ceiling)";
    }
    return String(seconds) + "s safety ceiling";
  }

  function renderChangePanel(workflowStatus) {
    var changeRequests = workflowStatus.change_requests && typeof workflowStatus.change_requests === "object" ? workflowStatus.change_requests : {};
    var recent = Array.isArray(changeRequests.recent) ? changeRequests.recent : [];
    var commands = Array.isArray(changeRequests.commands) ? changeRequests.commands : [];
    var rows = recent.slice(-6).map(function (record) {
      return statusFeedRow(record.change_request_id || "change_request", record.status || "unknown", record.user_request || "");
    }).join("");
    if (!rows) {
      rows = "<li><span>No change requests recorded.</span></li>";
    }
    return '<div class="control-summary">' +
      '<dl class="detail-list">' +
      row("Pending Review", changeRequests.pending_review_count || 0) +
      row("Needs Approval", changeRequests.needs_user_approval_count || 0) +
      row("Approved", changeRequests.approved_count || 0) +
      row("Applied", changeRequests.applied_count || 0) +
      row("Latest", changeRequests.latest_change_request_id || "none") +
      "</dl>" +
      '<div class="attention-commands">' + commands.slice(0, 6).map(function (command) {
        return "<code>" + escapeHtml(command) + "</code>";
      }).join("") + "</div>" +
      '<ol class="feed-list compact-feed">' + rows + "</ol>" +
      "</div>";
  }

  function renderInspectorConsole(payload, workflowStatus) {
    var consolePayload = payload && payload.inspector_console && typeof payload.inspector_console === "object" ? payload.inspector_console : {};
    var fallbackChangeRequests = workflowStatus.change_requests && typeof workflowStatus.change_requests === "object" ? workflowStatus.change_requests : {};
    var endpoints = consolePayload.endpoints && typeof consolePayload.endpoints === "object" ? consolePayload.endpoints : {};
    var blockers = Array.isArray(consolePayload.mutation_blockers) ? consolePayload.mutation_blockers : [];
    var chatBlockers = Array.isArray(consolePayload.chat_blockers) ? consolePayload.chat_blockers : [];
    var mutationAllowed = consolePayload.mutation_allowed === true;
    var chatAllowed = consolePayload.chat_allowed === true;
    var changeAllowed = consolePayload.change_request_allowed === true;
    var chatDisabled = !payload.server_mode || !chatAllowed;
    var changeDisabled = !payload.server_mode || !changeAllowed;
    var chatDisabledAttr = chatDisabled ? " disabled" : "";
    var changeDisabledAttr = changeDisabled ? " disabled" : "";
    var recentChat = Array.isArray(consolePayload.recent_chat) ? consolePayload.recent_chat : [];
    var recentChanges = Array.isArray(consolePayload.recent_change_requests) && consolePayload.recent_change_requests.length ?
      consolePayload.recent_change_requests :
      (Array.isArray(fallbackChangeRequests.recent) ? fallbackChangeRequests.recent : []);
    var commands = Array.isArray(consolePayload.commands) && consolePayload.commands.length ?
      consolePayload.commands :
      (Array.isArray(fallbackChangeRequests.commands) ? fallbackChangeRequests.commands : []);
    var contextPaths = Array.isArray(consolePayload.context_paths) ? consolePayload.context_paths :
      (Array.isArray(consolePayload.allowed_paths) ? consolePayload.allowed_paths : []);
    var latestChat = consolePayload.latest_chat && typeof consolePayload.latest_chat === "object" ? consolePayload.latest_chat : {};
    var contextPathRows = contextPaths.length ? contextPaths.map(function (path) {
      return "<li>" + escapeHtml(path) + "</li>";
    }).join("") : "<li>none</li>";
    return '<div class="inspector-console" data-mutation-allowed="' + escapeHtml(String(mutationAllowed)) + '">' +
      '<p class="selector-status" id="inspector-console-status" role="status">' +
      escapeHtml(inspectorConsoleStatusMessage(payload.server_mode, mutationAllowed, chatAllowed, blockers, chatBlockers)) + "</p>" +
      renderLatestInspectorChat(latestChat) +
      '<div class="inspector-action-grid">' +
      '<form id="inspector-chat-form" class="inspector-chat-form" data-endpoint="' + escapeHtml(endpoints.chat || "") + '">' +
      "<h3>Full Agent Inspector</h3>" +
      '<input type="hidden" name="runner_id" value="' + escapeHtml(consolePayload.runner_id || "inspector") + '">' +
      '<label>Question<textarea name="message" rows="4"' + chatDisabledAttr + "></textarea></label>" +
      '<button type="submit"' + chatDisabledAttr + ">Ask Inspector</button>" +
      '<p id="inspector-chat-status" class="selector-status" role="status"></p>' +
      "</form>" +
      '<form id="change-request-form" class="change-request-form" data-endpoint="' + escapeHtml(endpoints.change_request || "") + '">' +
      "<h3>Change Request</h3>" +
      '<label>Request<textarea name="user_request" rows="4"' + changeDisabledAttr + "></textarea></label>" +
      '<button type="submit"' + changeDisabledAttr + ">Create Change Request</button>" +
      '<p id="change-request-status" class="selector-status" role="status"></p>' +
      "</form>" +
      "</div>" +
      '<div class="inspector-meta-grid">' +
      '<dl class="detail-list">' +
      row("Mode", consolePayload.mode || "agent_inspection") +
      row("Chat Requests", consolePayload.chat_count || 0) +
      row("Chat Pending", consolePayload.chat_pending_count || 0) +
      row("Change Requests", consolePayload.change_request_count || fallbackChangeRequests.total_count || 0) +
      row("Pending Review", consolePayload.pending_review_count || fallbackChangeRequests.pending_review_count || 0) +
      row("Latest Chat", latestInspectorChatLabel(consolePayload)) +
      row("Latest Change", latestInspectorChangeLabel(consolePayload)) +
      "</dl>" +
      '<div class="inspector-allowed-paths"><h3>Context Paths</h3><ul>' + contextPathRows + "</ul></div>" +
      '<div class="attention-commands">' + commands.slice(0, 6).map(function (command) {
        return "<code>" + escapeHtml(command) + "</code>";
      }).join("") + "</div>" +
      "</div>" +
      '<div class="inspector-history-grid">' +
      "<div><h3>Recent Chat</h3>" +
      '<ol class="feed-list compact-feed">' + renderInspectorChatRows(recentChat) + "</ol></div>" +
      "<div><h3>Recent Change Requests</h3>" +
      '<ol class="feed-list compact-feed">' + renderInspectorChangeRows(recentChanges) + "</ol></div>" +
      "</div>" +
      '<section class="inspector-embedded-controls"><h3>Execution Controls</h3>' + renderControlPanel(payload, workflowStatus) + "</section>" +
      "</div>";
  }

  function renderLatestInspectorChat(record) {
    if (!record || !record.request_id) {
      return "";
    }
    var answer = cleanText(record.answer || record.summary) || "No response recorded.";
    var question = cleanText(record.user_message || "");
    var refs = Array.isArray(record.refs) && record.refs.length ? record.refs.slice(0, 5).map(function (ref) {
      return "<li>" + escapeHtml(ref) + "</li>";
    }).join("") : "<li>none</li>";
    return '<section class="inspector-latest-answer">' +
      "<h3>Latest Inspector Answer</h3>" +
      (question ? '<p class="inspector-question-text">' + escapeHtml(question) + "</p>" : "") +
      '<p class="inspector-answer-text">' + escapeHtml(answer) + "</p>" +
      '<details class="technical-detail"><summary>Sources and request details</summary>' +
      '<dl class="detail-list compact-detail-list">' +
      row("Status", record.status) +
      row("Request", record.request_id) +
      row("Response", record.response_id) +
      "</dl>" +
      "<ul>" + refs + "</ul></details>" +
      "</section>";
  }

  function renderInspectorChatRows(records) {
    var rows = records.slice(-6).map(function (record) {
      return statusFeedRow(record.request_id || "chat_request", record.status || "pending", record.answer || record.summary || record.user_message || "");
    }).join("");
    return rows || "<li><span>No inspector chat records.</span></li>";
  }

  function renderInspectorChangeRows(records) {
    var rows = records.slice(-6).map(function (record) {
      return statusFeedRow(record.change_request_id || "change_request", record.status || "pending_review", record.user_request || "");
    }).join("");
    return rows || "<li><span>No change requests recorded.</span></li>";
  }

  function inspectorConsoleStatusMessage(serverMode, mutationAllowed, chatAllowed, blockers, chatBlockers) {
    if (!serverMode) {
      return "Static dashboard is read-only; open server mode to create inspector chat or change request records.";
    }
    if (!mutationAllowed) {
      return "Inspector console is disabled for this workflow: " + (blockers.length ? blockers.join(", ") : "not mutable") + ".";
    }
    if (!chatAllowed) {
      return "Inspector chat is disabled: " + (chatBlockers.length ? chatBlockers.join(", ") : "inspector unavailable") + ". Change requests still append request records.";
    }
    return "Runs the configured inspector agent with full local access and shows the agent response here.";
  }

  function latestInspectorChatLabel(consolePayload) {
    if (!consolePayload.latest_chat_request_id) {
      return "none";
    }
    return [consolePayload.latest_chat_status, consolePayload.latest_chat_request_id].filter(Boolean).join(" ");
  }

  function latestInspectorChangeLabel(consolePayload) {
    if (!consolePayload.latest_change_request_id) {
      return "none";
    }
    return [consolePayload.latest_change_request_status, consolePayload.latest_change_request_id].filter(Boolean).join(" ");
  }

  function approvalMetricLabel(approvalControls) {
    var pending = Number(approvalControls.pending_count || 0);
    var closed = Number(approvalControls.approved_count || 0) +
      Number(approvalControls.rejected_count || 0) +
      Number(approvalControls.expired_count || 0) +
      Number(approvalControls.superseded_count || 0);
    if (pending > 0) {
      return String(pending) + " pending";
    }
    return closed > 0 ? "0 pending, " + String(closed) + " closed" : "none pending";
  }

  function renderApprovalTopAlert(approvalControls) {
    var pending = Number(approvalControls.pending_count || 0);
    if (pending <= 0) {
      return "";
    }
    return '<div class="approval-top-alert" data-pending-approvals="' + escapeHtml(pending) + '" role="status">' +
      "<strong>" + escapeHtml(String(pending) + " pending approval request" + (pending === 1 ? "" : "s")) + "</strong>" +
      "<span>Review the Approval Panel before continuing workflow execution.</span>" +
      "</div>";
  }

  function renderApprovalPanel(payload) {
    var approvalControls = payload && payload.approval_controls && typeof payload.approval_controls === "object" ? payload.approval_controls : {};
    var pending = Array.isArray(approvalControls.pending) ? approvalControls.pending : [];
    var recent = Array.isArray(approvalControls.recent) ? approvalControls.recent : [];
    var blockers = Array.isArray(approvalControls.mutation_blockers) ? approvalControls.mutation_blockers : [];
    var commands = Array.isArray(approvalControls.commands) ? approvalControls.commands : [];
    var mutationAllowed = approvalControls.mutation_allowed === true;
    var disabled = !payload.server_mode || !mutationAllowed;
    var pendingHtml = pending.map(function (record) {
      return renderApprovalCard(record, disabled, true);
    }).join("");
    if (!pendingHtml) {
      pendingHtml = '<p class="empty-state">No pending approvals.</p>';
    }
    var recentHtml = recent.slice(-8).map(function (record) {
      return renderApprovalCard(record, true, false);
    }).join("");
    if (!recentHtml) {
      recentHtml = '<p class="empty-state">No approval history recorded.</p>';
    }
    return '<div class="approval-summary" data-mutation-allowed="' + escapeHtml(String(mutationAllowed)) + '">' +
      '<p class="selector-status" id="approval-panel-status" role="status">' +
      escapeHtml(approvalPanelStatusMessage(payload.server_mode, mutationAllowed, blockers)) + "</p>" +
      '<dl class="detail-list">' +
      row("Pending", approvalControls.pending_count || 0) +
      row("Approved", approvalControls.approved_count || 0) +
      row("Rejected", approvalControls.rejected_count || 0) +
      row("Expired", approvalControls.expired_count || 0) +
      row("Requests", approvalControls.requests_path) +
      row("Responses", approvalControls.responses_path) +
      "</dl>" +
      '<div class="attention-commands">' + commands.slice(0, 8).map(function (command) {
        return "<code>" + escapeHtml(command) + "</code>";
      }).join("") + "</div>" +
      '<div class="approval-list" id="approval-pending-list"><h3>Pending Approvals</h3>' + pendingHtml + "</div>" +
      '<div class="approval-list" id="approval-recent-list"><h3>Recent Approval History</h3>' + recentHtml + "</div>" +
      "</div>";
  }

  function approvalPanelStatusMessage(serverMode, mutationAllowed, blockers) {
    if (!serverMode) {
      return "Static dashboard is read-only; open server mode to approve or reject pending requests.";
    }
    if (!mutationAllowed) {
      return "Approval responses are disabled for this workflow: " + (blockers.length ? blockers.join(", ") : "not mutable") + ".";
    }
    return "Approve or reject pending human approval requests. Responses append records only; the scheduler observes them on its next tick.";
  }

  function renderApprovalCard(record, disabled, pending) {
    var response = record.response && typeof record.response === "object" ? record.response : null;
    var refs = Array.isArray(record.evidence_refs) ? record.evidence_refs : [];
    var refHtml = refs.slice(0, 8).map(function (ref) {
      return "<li>" + escapeHtml(ref) + "</li>";
    }).join("");
    if (!refHtml) {
      refHtml = '<p class="empty-state">No evidence or source paths recorded.</p>';
    } else {
      refHtml = '<ul class="approval-ref-list">' + refHtml + "</ul>";
    }
    var responseDetail = "";
    if (response) {
      responseDetail = '<dl class="detail-list approval-response-detail">' +
        row("Decision", response.decision) +
        row("Responder", response.approved_by) +
        row("Responded", response.responded_at) +
        row("Notes", response.notes) +
        "</dl>";
    }
    var cardStatus = text(record.status || "unknown");
    return '<article class="approval-card" ' + statusAttributes(cardStatus) + ' data-approval-id="' + escapeHtml(record.approval_id || "approval") + '">' +
      '<div class="approval-card-heading">' +
      "<strong>" + escapeHtml(record.approval_id || "approval") + "</strong>" +
      statusPill(cardStatus) +
      "</div>" +
      "<p>" + escapeHtml(record.message || "Approval requested.") + "</p>" +
      '<dl class="detail-list">' +
      row("Type", record.type) +
      row("Task", record.task_id) +
      row("Run", record.run_id) +
      row("Scope", record.scope) +
      row("Created", record.created_at || record.requested_at) +
      row("Expires", record.expires_at) +
      row("Source", record.source) +
      "</dl>" +
      '<div class="approval-refs"><h4>Evidence And Source Paths</h4>' + refHtml + "</div>" +
      responseDetail +
      (pending ? renderApprovalResponseForm(record, disabled) : "") +
      "</article>";
  }

  function renderApprovalResponseForm(record, disabled) {
    var disabledAttr = disabled ? " disabled" : "";
    return '<form class="approval-response-form" data-approval-id="' + escapeHtml(record.approval_id || "") + '" data-endpoint="' + escapeHtml(record.respond_endpoint || "") + '">' +
      '<div class="form-grid">' +
      '<label>Scope<input name="scope" value="' + escapeHtml(record.scope || "") + '"' + disabledAttr + "></label>" +
      '<label>Notes<textarea name="notes" rows="3"' + disabledAttr + "></textarea></label>" +
      "</div>" +
      '<div class="approval-action-grid">' +
      '<button type="submit" data-approval-decision="approved"' + disabledAttr + ">Approve</button>" +
      '<button type="submit" data-approval-decision="rejected"' + disabledAttr + ">Reject</button>" +
      "</div>" +
      '<p class="selector-status approval-response-status" role="status"></p>' +
      "</form>";
  }

  function renderSnapshot(payload, metrics) {
    var counts = metrics.counts && typeof metrics.counts === "object" ? metrics.counts : {};
    var freshness = payload.read_model_freshness && typeof payload.read_model_freshness === "object" ? payload.read_model_freshness : {};
    var files = Array.isArray(payload.read_model_files) ? payload.read_model_files : [];
    var fileLinks = files.map(function (filename) {
      return '<li><a href="' + escapeHtml(readModelUrl(filename)) + '" data-read-model-file="' + escapeHtml(filename) + '">' + escapeHtml(filename) + "</a></li>";
    }).join("");
    return '<dl class="detail-list">' +
      row("Rendered", payload.rendered_at) +
      row("Read Models", payload.read_models_dir) +
      row("Freshness", freshness.status) +
      row("Read Model Event", eventRefLabel(freshness.read_model || {})) +
      row("Event Log Head", eventRefLabel(freshness.event_log || {})) +
      row("Tasks", counts.tasks_total) +
      row("Runs", counts.runs_total) +
      row("Failed Validations", counts.validations_failed) +
      "</dl>" +
      '<ul class="model-link-list">' + fileLinks + "</ul>";
  }

  function latestControlLabel(control) {
    if (!control.latest_request_id) {
      return "none";
    }
    return [control.latest_type, control.latest_status, control.latest_request_id].filter(Boolean).join(" ");
  }

  function eventRefLabel(value) {
    var eventId = value.source_event_id || value.events_head || value.event_id;
    var sequence = value.last_event_seq !== undefined && value.last_event_seq !== null ? value.last_event_seq : value.seq;
    if (eventId && sequence !== undefined && sequence !== null) {
      return eventId + " (seq " + sequence + ")";
    }
    if (eventId) {
      return eventId;
    }
    if (sequence !== undefined && sequence !== null) {
      return "seq " + sequence;
    }
    return "none";
  }

  function progressLabel(workflowStatus) {
    var progress = workflowStatus.progress && typeof workflowStatus.progress === "object" ? workflowStatus.progress : {};
    if (progress.progress_percent === undefined || progress.progress_percent === null) {
      return text(progress.completed_tasks || 0) + "/" + text(progress.total_tasks || 0);
    }
    return text(progress.progress_percent) + "% (" + text(progress.completed_tasks || 0) + "/" + text(progress.total_tasks || 0) + ")";
  }

  function checkpointLabel(versionControl) {
    var checkpoint = versionControl.latest_checkpoint && typeof versionControl.latest_checkpoint === "object" ? versionControl.latest_checkpoint : null;
    if (!checkpoint) {
      return "none";
    }
    return checkpoint.checkpoint_id || checkpoint.id || "recorded";
  }

  function setMetric(key, value) {
    var metric = document.querySelector('[data-metric="' + key + '"] strong');
    if (metric) {
      metric.textContent = text(value || "unknown");
      var shell = metric.parentNode;
      if (shell && shell.setAttribute) {
        shell.setAttribute("data-status", statusValue(value));
        shell.setAttribute("data-status-tier", statusTier(value));
      }
    }
  }

  function setHtml(id, html) {
    var node = document.getElementById(id);
    if (node) {
      node.innerHTML = html;
    }
  }

  function mountPanelCollapses() {
    if (!document.querySelectorAll) {
      return;
    }
    var panels = Array.prototype.slice.call(document.querySelectorAll('[data-collapsible="true"]'));
    panels.forEach(function (panel) {
      var key = panel.getAttribute("data-panel-key") || "";
      var button = panel.querySelector ? panel.querySelector("[data-panel-toggle]") : null;
      var heading = panel.querySelector ? panel.querySelector("[data-panel-heading]") : null;
      if (!button) {
        return;
      }
      if (button.getAttribute("data-mounted") !== "true") {
        button.setAttribute("data-mounted", "true");
        button.addEventListener("click", function (event) {
          if (event && event.stopPropagation) {
            event.stopPropagation();
          }
          setPanelCollapsed(panel, !panel.classList.contains("is-collapsed"), true);
        });
      }
      if (heading && heading.getAttribute("data-mounted") !== "true") {
        heading.setAttribute("data-mounted", "true");
        heading.addEventListener("click", function (event) {
          var target = event && event.target;
          if (target && target.tagName && String(target.tagName).toLowerCase() === "button") {
            return;
          }
          setPanelCollapsed(panel, !panel.classList.contains("is-collapsed"), true);
        });
      }
      setPanelCollapsed(panel, preferredPanelCollapsed(key, panel), false);
    });
  }

  function preferredPanelCollapsed(key, panel) {
    try {
      var stored = window.localStorage && window.localStorage.getItem("loopplane-panel-collapsed:" + key);
      if (stored === "true") {
        return true;
      }
      if (stored === "false") {
        return false;
      }
    } catch (error) {
      return panel.getAttribute("data-default-collapsed") === "true";
    }
    return panel.getAttribute("data-default-collapsed") === "true";
  }

  function setPanelCollapsed(panel, collapsed, persist) {
    var button = panel.querySelector ? panel.querySelector("[data-panel-toggle]") : null;
    var body = null;
    if (button) {
      body = document.getElementById(button.getAttribute("aria-controls") || "");
    }
    panel.classList[collapsed ? "add" : "remove"]("is-collapsed");
    if (body) {
      if (collapsed) {
        body.setAttribute("hidden", "hidden");
      } else {
        body.removeAttribute("hidden");
      }
    }
    if (button) {
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      var textNode = button.querySelector ? button.querySelector(".panel-toggle-text") : null;
      if (textNode) {
        textNode.textContent = collapsed ? "Expand" : "Collapse";
      }
    }
    if (persist) {
      try {
        if (window.localStorage) {
          window.localStorage.setItem("loopplane-panel-collapsed:" + (panel.getAttribute("data-panel-key") || ""), collapsed ? "true" : "false");
        }
      } catch (error) {
        return;
      }
    }
  }

  function mountGraphOverflow() {
    if (!document.querySelectorAll) {
      return;
    }
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-graph-more]"));
    buttons.forEach(function (button) {
      if (button.getAttribute("data-mounted") === "true") {
        return;
      }
      button.setAttribute("data-mounted", "true");
      button.addEventListener("click", function () {
        var stack = button.parentNode;
        var expanded = button.getAttribute("aria-expanded") === "true";
        if (!stack || !stack.querySelectorAll) {
          return;
        }
        Array.prototype.slice.call(stack.querySelectorAll('[data-overflow-node="true"]')).forEach(function (node) {
          if (expanded) {
            node.setAttribute("hidden", "hidden");
          } else {
            node.removeAttribute("hidden");
          }
        });
        button.setAttribute("aria-expanded", expanded ? "false" : "true");
        button.setAttribute("data-graph-more", expanded ? "collapsed" : "expanded");
        button.textContent = expanded ? (button.getAttribute("data-more-label") || "Show more related records") : (button.getAttribute("data-less-label") || "Hide related records");
      });
    });
  }

  function mountGraphExpandToggle() {
    var button = document.querySelector ? document.querySelector("[data-graph-expand-toggle]") : null;
    var panel = document.querySelector ? document.querySelector(".graph-panel") : null;
    if (!button || !panel) {
      return;
    }
    if (button.getAttribute("data-mounted") !== "true") {
      button.setAttribute("data-mounted", "true");
      button.addEventListener("click", function () {
        setGraphExpanded(panel, !panel.classList.contains("is-graph-expanded"), true);
      });
    }
    setGraphExpanded(panel, preferredGraphExpanded(), false);
  }

  function preferredGraphExpanded() {
    try {
      return window.localStorage && window.localStorage.getItem("loopplane-graph-expanded") === "true";
    } catch (error) {
      return false;
    }
  }

  function setGraphExpanded(panel, expanded, persist) {
    var button = panel.querySelector ? panel.querySelector("[data-graph-expand-toggle]") : null;
    var grid = document.querySelector ? document.querySelector(".dashboard-grid") : null;
    panel.classList[expanded ? "add" : "remove"]("is-graph-expanded");
    if (grid) {
      grid.classList[expanded ? "add" : "remove"]("is-graph-expanded");
    }
    if (button) {
      button.setAttribute("aria-pressed", expanded ? "true" : "false");
      button.textContent = expanded ? "Scrollable" : "Expand All";
    }
    if (panel.querySelectorAll) {
      Array.prototype.slice.call(panel.querySelectorAll("[data-graph-more]")).forEach(function (moreButton) {
        var moreExpanded = moreButton.getAttribute("aria-expanded") === "true";
        if (expanded && !moreExpanded) {
          moreButton.click();
        } else if (!expanded && moreExpanded) {
          moreButton.click();
        }
      });
    }
    if (persist) {
      try {
        if (window.localStorage) {
          window.localStorage.setItem("loopplane-graph-expanded", expanded ? "true" : "false");
        }
      } catch (error) {
        return;
      }
    }
  }

  function mountNodeDetails(payload, preferredNodeId) {
    var graph = readModel(payload, "workflow_graph.json");
    var nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
    var byId = {};
    nodes.forEach(function (node) {
      byId[node.node_id] = node;
    });
    var detail = document.getElementById("node-detail-body");
    var buttons = Array.prototype.slice.call(document.querySelectorAll(".graph-node"));
    if (!detail || buttons.length === 0) {
      return;
    }
    function select(button) {
      buttons.forEach(function (item) {
        item.classList.remove("is-selected");
      });
      button.classList.add("is-selected");
      var node = byId[button.getAttribute("data-node-id")];
      if (node) {
        detail.innerHTML = renderNode(node, nodeDetailForNode(payload, node));
        mountFilePreviewActions(payload);
        fetchSelectedRunDetail(payload, node, detail);
      }
    }
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        select(button);
      });
    });
    var selected = buttons[0];
    if (preferredNodeId) {
      selected = buttons.filter(function (button) {
        return button.getAttribute("data-node-id") === preferredNodeId;
      })[0] || selected;
    }
    select(selected);
  }

  function fetchSelectedRunDetail(payload, node, detail) {
    if (!payload || !payload.server_mode || !node || !node.run_id || !payload.workflow_id || !window.fetch) {
      return;
    }
    var selectedNodeId = node.node_id;
    fetchRunDetail(payload.workflow_id, node.run_id).then(function (result) {
      var selectedButton = document.querySelector(".graph-node.is-selected");
      if (!selectedButton || selectedButton.getAttribute("data-node-id") !== selectedNodeId) {
        return;
      }
      var apiDetail = result.node_detail && typeof result.node_detail === "object" ? result.node_detail : {};
      if (!apiDetail.sections && result.details && typeof result.details === "object") {
        apiDetail = {
          sections: Array.isArray(result.details.sections) ? result.details.sections : [],
          run: result.run || {}
        };
      }
      detail.innerHTML = renderNode(node, apiDetail);
      mountFilePreviewActions(payload);
    }).catch(function () {
      return null;
    });
  }

  function fetchRunDetail(workflowId, runId) {
    var url = "/api/workflows/" + encodeURIComponent(workflowId) + "/runs/" + encodeURIComponent(runId) + tokenQuery();
    return window.fetch(url, {
      credentials: "same-origin",
      headers: {"Accept": "application/json"}
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok || !payload.ok) {
          throw new Error((payload.errors && payload.errors.join("; ")) || payload.status || "run detail unavailable");
        }
        return payload;
      });
    });
  }

  function selectorStatus(message) {
    var node = document.getElementById("workflow-selector-status");
    if (node) {
      node.textContent = message;
    }
  }

  function setWorkflowHistoryMode(showAll) {
    workflowHistoryShowAll = Boolean(showAll);
    var shell = document.querySelector ? document.querySelector(".workflow-selector-shell") : null;
    var toggle = document.getElementById("workflow-history-toggle");
    if (shell) {
      shell.setAttribute("data-workflow-history-mode", workflowHistoryShowAll ? "all" : "selected");
    }
    if (toggle) {
      toggle.setAttribute("aria-pressed", workflowHistoryShowAll ? "true" : "false");
      toggle.textContent = workflowHistoryShowAll ? "Show selected only" : "Show all workflows";
    }
  }

  function mountWorkflowHistoryToggle() {
    var toggle = document.getElementById("workflow-history-toggle");
    if (!toggle) {
      setWorkflowHistoryMode(workflowHistoryShowAll);
      return;
    }
    if (toggle.getAttribute("data-mounted") !== "true") {
      toggle.setAttribute("data-mounted", "true");
      toggle.addEventListener("click", function () {
        setWorkflowHistoryMode(!workflowHistoryShowAll);
      });
    }
    setWorkflowHistoryMode(workflowHistoryShowAll);
  }

  function updateHistorySelection(workflowId) {
    Array.prototype.slice.call(document.querySelectorAll(".workflow-history-row")).forEach(function (rowNode) {
      if (rowNode.getAttribute("data-workflow-id") === workflowId) {
        rowNode.classList.add("selected");
      } else {
        rowNode.classList.remove("selected");
      }
    });
  }

  function normalizePayload(payload, workflowId) {
    var base = activePayload || {};
    var normalized = {};
    Object.keys(base).forEach(function (key) {
      normalized[key] = base[key];
    });
    Object.keys(payload || {}).forEach(function (key) {
      normalized[key] = payload[key];
    });
    normalized.workflow_id = workflowId || payload.workflow_id || base.workflow_id;
    normalized.workflow_title = payload.workflow_title || base.workflow_title;
    normalized.server_mode = Boolean((payload && payload.server_mode) || base.server_mode);
    normalized.workflows = payload.workflows || base.workflows || [];
    normalized.workspace = payload.workspace || base.workspace || {};
    normalized.workflow_snapshots = base.workflow_snapshots || {};
    normalized.read_model_files = payload.read_model_files || base.read_model_files || [];
    normalized.plan_markdown = payload.plan_markdown || base.plan_markdown || {};
    normalized.planning_controls = payload.planning_controls || base.planning_controls || {};
    normalized.execution_controls = payload.execution_controls || base.execution_controls || {};
    normalized.approval_controls = payload.approval_controls || base.approval_controls || {};
    normalized.inspector_console = payload.inspector_console || base.inspector_console || {};
    normalized.read_model_rebuild = payload.read_model_rebuild || base.read_model_rebuild || {};
    normalized.node_details = payload.node_details || base.node_details || {};
    normalized.initial_dashboard_load = payload.initial_dashboard_load === true;
    return normalized;
  }

  function dashboardCacheKey(workflowId) {
    return String(workflowId || "").trim();
  }

  function rememberDashboardPayload(payload, etag) {
    var workflowId = dashboardCacheKey(payload && payload.workflow_id);
    if (!workflowId) {
      return;
    }
    dashboardPayloadCache[workflowId] = payload;
    var nextEtag = etag || (payload && payload.dashboard_etag) || "";
    if (nextEtag) {
      dashboardEtagCache[workflowId] = nextEtag;
    }
  }

  function captureDashboardState() {
    var selected = document.querySelector ? document.querySelector(".graph-node.is-selected") : null;
    var state = {
      selectedNodeId: selected ? selected.getAttribute("data-node-id") || "" : "",
      scrolls: {},
      drafts: {}
    };
    [
      "#plan-panel-body",
      "#graph-panel-body",
      "#node-detail-body",
      "#activity-feed-body",
      "#runner-panel-body",
      "#inspector-console-body",
      "#metrics-panel-body",
      ".graph-pipeline-scroll"
    ].forEach(function (selector) {
      var node = document.querySelector ? document.querySelector(selector) : null;
      if (node) {
        state.scrolls[selector] = {top: node.scrollTop || 0, left: node.scrollLeft || 0};
      }
    });
    [
      "#inspector-chat-form textarea[name='message']",
      "#change-request-form textarea[name='user_request']",
      "#execution-control-form input[name='reason']",
      "#planning-control-form input[name='reason']",
      "#planning-control-form input[name='planner_runner_id']",
      "#planning-control-form input[name='auditor_runner_id']",
      "#planning-control-form input[name='activation_source']"
    ].forEach(function (selector) {
      var input = document.querySelector ? document.querySelector(selector) : null;
      if (input) {
        state.drafts[selector] = input.value;
      }
    });
    return state;
  }

  function restoreDashboardState(state) {
    if (!state) {
      return;
    }
    Object.keys(state.drafts || {}).forEach(function (selector) {
      var input = document.querySelector ? document.querySelector(selector) : null;
      if (input) {
        input.value = state.drafts[selector];
      }
    });
    var restoreScrolls = function () {
      Object.keys(state.scrolls || {}).forEach(function (selector) {
        var node = document.querySelector ? document.querySelector(selector) : null;
        var scroll = state.scrolls[selector];
        if (node && scroll) {
          node.scrollTop = scroll.top || 0;
          node.scrollLeft = scroll.left || 0;
        }
      });
    };
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(restoreScrolls);
    } else {
      restoreScrolls();
    }
  }

  function applyPayload(payload, message) {
    var uiState = captureDashboardState();
    var workflowStatus = readModel(payload, "workflow_status.json");
    var planIndex = readModel(payload, "plan_index.json");
    var workflowGraph = readModel(payload, "workflow_graph.json");
    var versionControl = readModel(payload, "version_control_status.json");
    var metrics = readModel(payload, "metrics.json");
    var feed = jsonlModel(payload, "dashboard_feed.jsonl");
    var approvalControls = payload.approval_controls && typeof payload.approval_controls === "object" ? payload.approval_controls : {};
    activePayload = payload;
    rememberDashboardPayload(payload);
    var title = document.getElementById("dashboard-workflow-title");
    if (title) {
      title.textContent = workflowDisplayTitle(payload);
    }
    var selector = document.getElementById("workflow-selector");
    if (selector && payload.workflow_id) {
      selector.value = text(payload.workflow_id);
    }
    setMetric("status", workflowStatus.status);
    setMetric("phase", workflowStatus.phase);
    setMetric("progress", progressLabel(workflowStatus));
    setMetric("checkpoint", checkpointLabel(versionControl));
    setMetric("approvals", approvalMetricLabel(approvalControls));
    setHtml("approval-alert-shell", renderApprovalTopAlert(approvalControls));
    setHtml("freshness-banner-shell", renderFreshnessBanner(payload));
    setMetric("elapsed", workflowElapsedLabel(payload, workflowStatus, workflowGraph, feed));
    mountWorkspaceSelector(payload);
    setHtml("plan-panel-body", renderPlanPanel(planIndex, payload.plan_markdown || {}, workflowGraph, payload));
    setHtml("graph-panel-body", renderGraphPanel(workflowGraph, planIndex));
    setHtml("node-detail-body", '<p class="empty-state">No node selected.</p>');
    setHtml("activity-feed-body", renderActivityFeed(feed, payload));
    setHtml("vc-panel-body", renderGitStatus(versionControl));
    setHtml("approval-panel-body", renderApprovalPanel(payload));
    setHtml("runner-panel-body", renderRunnerPanel(payload));
    setHtml("inspector-console-body", renderInspectorConsole(payload, workflowStatus));
    setHtml("metrics-panel-body", renderSnapshot(payload, metrics));
    updateHistorySelection(text(payload.workflow_id));
    setWorkflowHistoryMode(workflowHistoryShowAll);
    selectorStatus(message || ("Viewing " + text(payload.workflow_id) + ". Selection does not update current_workflow.json."));
    updateRefreshDisplay(payload, message);
    mountRefreshControls(payload);
    mountPanelCollapses();
    mountGraphOverflow();
    mountGraphExpandToggle();
    mountNodeDetails(payload, uiState.selectedNodeId);
    mountRunnerConfigForm(payload);
    mountPlanningControlForm(payload);
    mountExecutionControlForm(payload);
    mountReadModelRebuildForm(payload);
    mountApprovalResponseForms(payload);
    mountInspectorConsoleForms(payload);
    mountPlanViewToggle();
    mountReadModelLinks();
    mountFilePreviewActions(payload);
    restoreDashboardState(uiState);
  }

  function mountPlanViewToggle() {
    var buttons = document.querySelectorAll ? document.querySelectorAll("[data-plan-view]") : [];
    if (!buttons || !buttons.length) {
      return;
    }
    var selected = preferredPlanView();
    Array.prototype.forEach.call(buttons, function (button) {
      if (button.getAttribute("data-mounted") !== "true") {
        button.setAttribute("data-mounted", "true");
        button.addEventListener("click", function () {
          setPlanView(button.getAttribute("data-plan-view") || "checklist");
        });
      }
    });
    setPlanView(selected);
  }

  function preferredPlanView() {
    try {
      var stored = window.localStorage && window.localStorage.getItem("loopplane-plan-view");
      if (stored === "checklist" || stored === "markdown") {
        return stored;
      }
    } catch (error) {
      return "checklist";
    }
    return "checklist";
  }

  function setPlanView(view) {
    var next = view === "markdown" ? "markdown" : "checklist";
    var buttons = document.querySelectorAll ? document.querySelectorAll("[data-plan-view]") : [];
    var panels = document.querySelectorAll ? document.querySelectorAll("[data-plan-view-panel]") : [];
    Array.prototype.forEach.call(buttons, function (button) {
      var active = button.getAttribute("data-plan-view") === next;
      button.classList[active ? "add" : "remove"]("is-active");
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    Array.prototype.forEach.call(panels, function (panel) {
      var active = panel.getAttribute("data-plan-view-panel") === next;
      panel.classList[active ? "add" : "remove"]("is-active");
      if (active) {
        panel.removeAttribute("hidden");
      } else {
        panel.setAttribute("hidden", "hidden");
      }
    });
    try {
      if (window.localStorage) {
        window.localStorage.setItem("loopplane-plan-view", next);
      }
    } catch (error) {
      return;
    }
  }

  function mountRefreshControls(payload) {
    var button = document.getElementById("dashboard-refresh-button");
    var live = Boolean(payload && payload.server_mode && window.fetch);
    if (button) {
      button.disabled = !live;
      if (button.getAttribute("data-mounted") !== "true") {
        button.setAttribute("data-mounted", "true");
        button.addEventListener("click", function () {
          refreshDashboard("manual");
        });
      }
    }
    if (!live) {
      setRefreshStatus("Static snapshot; live refresh unavailable.", "muted");
      updateRefreshDisplay(payload, "");
      stopAutoRefresh();
      return;
    }
    var rebuild = payload && payload.read_model_rebuild && typeof payload.read_model_rebuild === "object" ? payload.read_model_rebuild : {};
    var needsInitialDashboardLoad = payload && payload.initial_dashboard_load === true;
    var needsLiveRefresh = readModelLiveRefreshExpected(payload, rebuild);
    setRefreshStatus(
      needsInitialDashboardLoad ? "Loading workflow data..." : (needsLiveRefresh ? "Refreshing read models now..." : "Auto-refresh every 30s."),
      (needsInitialDashboardLoad || needsLiveRefresh) ? "active" : "success"
    );
    updateRefreshDisplay(payload, "");
    startAutoRefresh();
    if ((needsInitialDashboardLoad || needsLiveRefresh) && !initialLiveRefreshStarted) {
      initialLiveRefreshStarted = true;
      refreshDashboard(needsInitialDashboardLoad ? "initial-load" : "initial");
    }
  }

  function startAutoRefresh() {
    if (refreshTimer || !window.setInterval) {
      return;
    }
    refreshTimer = window.setInterval(function () {
      refreshDashboard("auto");
    }, REFRESH_INTERVAL_MS);
  }

  function stopAutoRefresh() {
    if (!refreshTimer || !window.clearInterval) {
      refreshTimer = null;
      return;
    }
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }

  function refreshDashboard(source) {
    var payload = activePayload || {};
    if (!payload.server_mode || !payload.workflow_id || !window.fetch || refreshInFlight) {
      return;
    }
    refreshInFlight = true;
    setRefreshStatus(source === "manual" ? "Refreshing now..." : (source === "initial-load" ? "Loading workflow data..." : (source === "initial" ? "Refreshing read models now..." : "Auto-refreshing...")), "active");
    fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
      refreshInFlight = false;
      if (nextPayload.dashboard_not_modified) {
        setRefreshStatus(source === "manual" ? "No dashboard changes." : "Dashboard unchanged. Auto-refresh every 30s.", "success");
        return;
      }
      applyPayload(nextPayload, source === "manual" ? "Manual refresh complete." : (source === "initial-load" ? "Workflow data loaded." : (source === "initial" ? "Live read-model refresh complete." : "Auto refresh complete.")));
    }).catch(function (error) {
      refreshInFlight = false;
      setRefreshStatus("Refresh failed: " + error.message, "danger");
    });
  }

  function updateRefreshDisplay(payload, message) {
    var last = document.getElementById("dashboard-last-refreshed");
    var value = payload && (payload.rendered_at || payload.generated_at || payload.started_at);
    var display = value || new Date().toISOString();
    if (last) {
      last.setAttribute("datetime", display);
      last.textContent = "Last refreshed: " + display;
    }
    if (message && (payload && payload.server_mode)) {
      setRefreshStatus(message + " Auto-refresh every 30s.", "success");
    }
  }

  function setRefreshStatus(message, tier) {
    var status = document.getElementById("dashboard-refresh-status");
    if (!status) {
      return;
    }
    status.textContent = message;
    status.setAttribute("data-status-tier", tier || "info");
  }

  function staticSnapshot(payload, workflowId) {
    var snapshots = payload && payload.workflow_snapshots && typeof payload.workflow_snapshots === "object" ? payload.workflow_snapshots : {};
    var snapshot = snapshots[workflowId];
    if (!snapshot || !snapshot.ok) {
      return null;
    }
    return normalizePayload(snapshot, workflowId);
  }

  function tokenQuery() {
    try {
      var params = new URLSearchParams(window.location.search || "");
      var token = params.get("token");
      return token ? "?token=" + encodeURIComponent(token) : "";
    } catch (error) {
      return "";
    }
  }

  function readModelUrl(filename) {
    return "read_models/" + encodeURIComponent(filename || "") + tokenQuery();
  }

  function fileUrl(payload, path, options) {
    if (!payload || !payload.workflow_id || !path) {
      return "";
    }
    var filePath = projectRelativeFromAbsolutePath(path, payload) || cleanText(path);
    if (/^\//.test(filePath)) {
      return "";
    }
    filePath = normalizeRelativePath(filePath);
    if (!filePath) {
      return "";
    }
    if (!payload.server_mode) {
      var base = cleanText(payload.static_project_root_href || "../..");
      return joinUrlPath(base, filePath);
    }
    var token = tokenQuery();
    var url = "/api/workflows/" + encodeURIComponent(payload.workflow_id) + "/files" +
      token + (token ? "&" : "?") + "path=" + encodeURIComponent(filePath);
    var opts = options && typeof options === "object" ? options : {};
    if (opts.preview) {
      url += "&preview=1&max_lines=" + encodeURIComponent(String(opts.maxLines || FILE_PREVIEW_MAX_LINES));
    }
    if (opts.tail) {
      url += "&tail=1&max_lines=" + encodeURIComponent(String(opts.maxLines || FILE_PREVIEW_MAX_LINES));
    }
    return url;
  }

  function mountReadModelLinks() {
    if (!document.querySelectorAll) {
      return;
    }
    Array.prototype.slice.call(document.querySelectorAll("[data-read-model-file]")).forEach(function (link) {
      link.setAttribute("href", readModelUrl(link.getAttribute("data-read-model-file") || ""));
    });
  }

  function mountFilePreviewActions(payload) {
    if (!document.querySelectorAll) {
      return;
    }
    Array.prototype.slice.call(document.querySelectorAll("[data-detail-file-link]")).forEach(function (link) {
      var path = link.getAttribute("data-detail-path") || "";
      var href = fileUrl(payload, path);
      if (href) {
        link.setAttribute("href", href);
        link.setAttribute("target", "_blank");
        link.setAttribute("rel", "noopener noreferrer");
      } else {
        link.setAttribute("href", "#");
      }
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-detail-title]")).forEach(function (button) {
      if (button.getAttribute("data-mounted") === "true") {
        return;
      }
      button.setAttribute("data-mounted", "true");
      button.addEventListener("click", function () {
        previewDetailFile(payload, button);
      });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-log-stream-title]")).forEach(function (button) {
      if (button.getAttribute("data-mounted") === "true") {
        return;
      }
      button.setAttribute("data-mounted", "true");
      button.addEventListener("click", function () {
        streamLogTail(payload, button);
      });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-file-preview-close]")).forEach(function (button) {
      if (button.getAttribute("data-mounted") === "true") {
        return;
      }
      button.setAttribute("data-mounted", "true");
      button.addEventListener("click", closeFilePreview);
    });
  }

  function previewDetailFile(payload, button) {
    var title = button.getAttribute("data-detail-title") || "File Preview";
    var path = button.getAttribute("data-detail-path") || "";
    var fallback = button.getAttribute("data-detail-content") || "";
    var renderMode = button.getAttribute("data-detail-render") || detailRenderMode(path);
    stopLogStream();
    if (renderMode === "image" && path) {
      var imageHref = fileUrl(payload, path);
      openFilePreview(
        title,
        path,
        imageHref || fallback,
        imageHref ? "Image preview." : (fallback ? fallbackPreviewStatus(button) : "Image preview is unavailable."),
        "image"
      );
      return;
    }
    openFilePreview(
      title,
      path,
      path ? "Loading preview..." : (fallback || "Loading preview..."),
      path ? "Loading preview..." : (fallback ? fallbackPreviewStatus(button) : "Loading preview..."),
      renderMode
    );
    var href = fileUrl(payload, path, {preview: true, maxLines: FILE_PREVIEW_MAX_LINES});
    if (!href || !window.fetch) {
      if (!fallback) {
        openFilePreview(title, path, "File preview is only available in live dashboard server mode.");
      } else {
        openFilePreview(title, path, fallback, fallbackPreviewStatus(button), renderMode);
      }
      return;
    }
    window.fetch(href, {credentials: "same-origin", headers: {"Accept": "text/plain"}}).then(function (response) {
      return response.text().then(function (content) {
        if (!response.ok) {
          throw new Error(content || response.statusText || "file unavailable");
        }
        openFilePreview(title, path, content, "Preview shows at most the first " + FILE_PREVIEW_MAX_LINES + " lines. Use Open file to view the full file.", renderMode);
      });
    }).catch(function (error) {
      openFilePreview(title, path, fallback || ("Unable to load file: " + error.message), fallback ? fallbackPreviewStatus(button) : "", renderMode);
    });
  }

  function fallbackPreviewStatus(button) {
    if (button && button.getAttribute("data-detail-truncated") === "true") {
      return "Embedded preview content is truncated. Use Open file to view the full file.";
    }
    return "Embedded preview content.";
  }

  function streamLogTail(payload, button) {
    var title = button.getAttribute("data-log-stream-title") || "Log Stream";
    var path = button.getAttribute("data-detail-path") || "";
    stopLogStream();
    openFilePreview(title, path, "Loading log tail...", "Following the last " + FILE_PREVIEW_MAX_LINES + " lines. Updates every 2s.", "text", {scrollToBottom: true});
    function refreshTail() {
      if (logStreamInFlight) {
        return;
      }
      var href = fileUrl(payload, path, {tail: true, maxLines: FILE_PREVIEW_MAX_LINES});
      if (!href || !window.fetch) {
        openFilePreview(title, path, "Log streaming is only available in live dashboard server mode.", "", "text");
        stopLogStream();
        return;
      }
      logStreamInFlight = true;
      window.fetch(href, {credentials: "same-origin", headers: {"Accept": "text/plain"}}).then(function (response) {
        return response.text().then(function (content) {
          if (!response.ok) {
            throw new Error(content || response.statusText || "log unavailable");
          }
          openFilePreview(title, path, content || "[log file is empty]", "Following the last " + FILE_PREVIEW_MAX_LINES + " lines. Updates every 2s.", "text", {scrollToBottom: true});
        });
      }).catch(function (error) {
        openFilePreview(title, path, "Waiting for log tail: " + error.message, "Retrying every 2s.", "text");
      }).then(function () {
        logStreamInFlight = false;
      });
    }
    refreshTail();
    logStreamTimer = window.setInterval(refreshTail, 2000);
  }

  function stopLogStream() {
    if (logStreamTimer !== null) {
      window.clearInterval(logStreamTimer);
      logStreamTimer = null;
    }
    logStreamInFlight = false;
  }

  function openFilePreview(title, path, content, status, renderMode, options) {
    var modal = document.getElementById("file-preview-modal");
    var titleNode = document.getElementById("file-preview-title");
    var pathNode = document.getElementById("file-preview-path");
    var statusNode = document.getElementById("file-preview-status");
    var contentNode = document.getElementById("file-preview-content");
    if (!modal || !contentNode) {
      return;
    }
    if (titleNode) {
      titleNode.textContent = title || "File Preview";
    }
    if (pathNode) {
      pathNode.textContent = path || "";
    }
    if (statusNode) {
      statusNode.textContent = status || "";
    }
    renderPreviewContent(contentNode, content || "", renderMode || "text", {path: path});
    if (options && options.scrollToBottom) {
      scrollPreviewToBottom(contentNode);
    }
    modal.removeAttribute("hidden");
  }

  function scrollPreviewToBottom(contentNode) {
    function applyScroll() {
      contentNode.scrollTop = contentNode.scrollHeight;
    }
    applyScroll();
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(applyScroll);
    } else {
      window.setTimeout(applyScroll, 0);
    }
  }

  function renderPreviewContent(contentNode, content, renderMode, options) {
    if (renderMode === "markdown") {
      contentNode.className = "file-preview-content markdown-document markdown-preview-content";
      contentNode.innerHTML = renderMarkdownDocument(content || "", options || {});
      return;
    }
    if (renderMode === "image") {
      var src = cleanText(content);
      contentNode.className = "file-preview-content image-preview-content";
      contentNode.innerHTML = src ?
        '<figure class="markdown-figure image-preview-figure"><img src="' + escapeHtml(src) + '" alt="' + escapeHtml((options && options.path) || "artifact preview") + '" loading="lazy"></figure>' :
        '<p class="empty-state">Image preview is unavailable.</p>';
      return;
    }
    contentNode.className = "detail-pre file-preview-content";
    contentNode.textContent = content || "";
  }

  function closeFilePreview() {
    stopLogStream();
    var modal = document.getElementById("file-preview-modal");
    if (modal) {
      modal.setAttribute("hidden", "hidden");
    }
  }

  function tokenValue() {
    try {
      var params = new URLSearchParams(window.location.search || "");
      return params.get("token") || "";
    } catch (error) {
      return "";
    }
  }

  function requestedWorkflowValue() {
    try {
      var params = new URLSearchParams(window.location.search || "");
      return cleanText(params.get("workflow") || params.get("workflow_id") || "");
    } catch (error) {
      return "";
    }
  }

  function loadRequestedWorkflowFromLocation(payload) {
    var workflowId = requestedWorkflowValue();
    if (!workflowId || workflowId === cleanText(payload && payload.workflow_id)) {
      return;
    }
    var snapshot = staticSnapshot(payload, workflowId);
    if (snapshot) {
      applyPayload(snapshot, "Viewing " + workflowId + " from the URL workflow selection. Selection does not update current_workflow.json.");
      return;
    }
    if (payload && payload.server_mode && window.fetch) {
      selectorStatus("Loading " + workflowId + " from the workspace API.");
      fetchDashboardData(workflowId).then(function (nextPayload) {
        applyPayload(nextPayload, "Viewing " + workflowId + " from the URL workflow selection. Selection does not update current_workflow.json.");
      }).catch(function (error) {
        selectorStatus("Unable to load " + workflowId + ": " + error.message);
      });
      return;
    }
    selectorStatus(workflowId + " is listed in the URL, but its read models are not available in this static dashboard.");
  }

  function fetchDashboardData(workflowId) {
    var url = "/api/workflows/" + encodeURIComponent(workflowId) + "/dashboard-data" + tokenQuery();
    var cacheKey = dashboardCacheKey(workflowId);
    var headers = {"Accept": "application/json"};
    if (cacheKey && dashboardPayloadCache[cacheKey] && dashboardEtagCache[cacheKey]) {
      headers["If-None-Match"] = dashboardEtagCache[cacheKey];
    }
    return window.fetch(url, {
      credentials: "same-origin",
      headers: headers
    }).then(function (response) {
      if (response.status === 304) {
        var cached = dashboardPayloadCache[cacheKey];
        if (!cached) {
          throw new Error("dashboard data unchanged but no cached payload is available");
        }
        var unchanged = normalizePayload(cached, workflowId);
        unchanged.dashboard_not_modified = true;
        return unchanged;
      }
      return response.json().then(function (payload) {
        if (!response.ok || !payload.ok) {
          throw new Error((payload.errors && payload.errors.join("; ")) || payload.status || "dashboard data unavailable");
        }
        var etag = response.headers && response.headers.get ? response.headers.get("ETag") : "";
        if (etag) {
          payload.dashboard_etag = etag;
        }
        var normalized = normalizePayload(payload, workflowId);
        rememberDashboardPayload(normalized, etag);
        return normalized;
      });
    });
  }

  function refreshDashboardAfterMutation(payload, statusElement, message) {
    if (!payload || !payload.server_mode || !payload.workflow_id || !window.fetch) {
      if (statusElement) {
        statusElement.textContent = message || "Settings applied.";
      }
      return Promise.resolve();
    }
    return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
      applyPayload(nextPayload, message || "Dashboard refreshed.");
    });
  }

  function fetchApprovalStatus(workflowId) {
    var url = "/api/workflows/" + encodeURIComponent(workflowId) + "/approvals" + tokenQuery();
    return window.fetch(url, {
      credentials: "same-origin",
      headers: {"Accept": "application/json"}
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok || !payload.ok) {
          throw new Error((payload.errors && payload.errors.join("; ")) || payload.status || "approval list unavailable");
        }
        return payload;
      });
    });
  }

  function approvalControlsFromStatus(statusPayload, previousControls, workflowId) {
    var previous = previousControls && typeof previousControls === "object" ? previousControls : {};
    var approvals = Array.isArray(statusPayload.approvals) ? statusPayload.approvals : [];
    approvals.forEach(function (approval) {
      if (!approval.respond_endpoint && approval.approval_id) {
        approval.respond_endpoint = "/api/workflows/" + encodeURIComponent(workflowId) + "/approvals/" + encodeURIComponent(approval.approval_id) + "/respond";
      }
    });
    var pending = approvals.filter(function (approval) {
      return approval.status === "pending";
    });
    function countStatus(status) {
      return approvals.filter(function (approval) {
        return approval.status === status;
      }).length;
    }
    return {
      schema_version: statusPayload.schema_version || previous.schema_version,
      ok: statusPayload.ok !== false,
      status: previous.status || statusPayload.status || "available",
      workflow_id: statusPayload.workflow_id || workflowId,
      mutation_allowed: previous.mutation_allowed === true,
      mutation_blockers: Array.isArray(previous.mutation_blockers) ? previous.mutation_blockers : [],
      response_record_only: true,
      approval_policy: statusPayload.approval_policy || previous.approval_policy || {},
      requests_path: statusPayload.requests_path || previous.requests_path,
      responses_path: statusPayload.responses_path || previous.responses_path,
      list_endpoint: previous.list_endpoint || ("/api/workflows/" + encodeURIComponent(workflowId) + "/approvals"),
      pending_count: pending.length,
      approved_count: countStatus("approved"),
      rejected_count: countStatus("rejected"),
      expired_count: countStatus("expired"),
      superseded_count: countStatus("superseded"),
      pending: pending,
      recent: approvals.slice(-8),
      approvals: approvals,
      commands: Array.isArray(previous.commands) ? previous.commands : ["loopplane approvals --project <project>"],
      warnings: Array.isArray(statusPayload.warnings) ? statusPayload.warnings : []
    };
  }

  function mountWorkspaceSelector(payload) {
    var selector = document.getElementById("workflow-selector");
    mountWorkflowHistoryToggle();
    if (!selector) {
      return;
    }
    updateHistorySelection(selector.value || text(payload && payload.workflow_id));
    if (selector.getAttribute("data-mounted") === "true") {
      return;
    }
    selector.setAttribute("data-mounted", "true");
    selector.addEventListener("change", function () {
      var workflowId = selector.value;
      updateHistorySelection(workflowId);
      setWorkflowHistoryMode(workflowHistoryShowAll);
      var snapshot = staticSnapshot(activePayload || payload, workflowId);
      if (snapshot) {
        applyPayload(snapshot, "Viewing " + workflowId + " from the embedded static read-model snapshot. Selection does not update current_workflow.json.");
        return;
      }
      if (activePayload && activePayload.server_mode && window.fetch) {
        selectorStatus("Loading " + workflowId + " from the workspace API.");
        fetchDashboardData(workflowId).then(function (nextPayload) {
          applyPayload(nextPayload, "Viewing " + workflowId + " from the workspace API. Selection does not update current_workflow.json.");
        }).catch(function (error) {
          selectorStatus("Unable to load " + workflowId + ": " + error.message);
        });
        return;
      }
      selectorStatus(workflowId + " is listed, but its read models are not available in this static dashboard.");
    });
  }

  function mountRunnerConfigForm(payload) {
    var form = document.getElementById("runner-config-request-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("runner-config-request-status");
    var runnerSelect = form.querySelector('[name="runner_id"]');
    if (runnerSelect) {
      runnerSelect.addEventListener("change", function () {
        fillRunnerConfigForm(form, runnerFromPayload(payload, runnerSelect.value));
      });
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var endpoint = payload && payload.runner_configuration ? payload.runner_configuration.request_endpoint : "";
      if (!endpoint || !window.fetch) {
        if (status) {
          status.textContent = "Request endpoint unavailable.";
        }
        return;
      }
      var data = new FormData(form);
      var timeout = data.get("timeout_seconds");
      var model = formValue(data.get("model")).trim();
      var reasoningEffort = formValue(data.get("reasoning_effort")).trim();
      var body = {
        runner_id: formValue(data.get("runner_id")).trim(),
        role: formValue(data.get("role")).trim(),
        adapter: formValue(data.get("adapter")).trim(),
        command: formValue(data.get("command")).trim(),
        prompt_delivery_mode: formValue(data.get("prompt_delivery_mode")).trim()
      };
      if (model) {
        body.model = model;
      }
      if (reasoningEffort) {
        body.reasoning_effort = reasoningEffort;
      }
      if (timeout !== null && String(timeout).trim() !== "") {
        body.timeout_seconds = Number(timeout);
      }
      var token = tokenValue();
      var headers = {"Accept": "application/json", "Content-Type": "application/json"};
      if (token) {
        headers["X-LoopPlane-Token"] = token;
      }
      if (status) {
        status.textContent = "Applying runner settings.";
      }
      window.fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify(body)
      }).then(function (response) {
        return response.json().then(function (result) {
          if (!response.ok || !result.ok) {
            throw new Error((result.errors && result.errors.join("; ")) || result.status || "request failed");
          }
          if (status) {
            status.textContent = "Applied runner settings. Audit request " + text(result.request && result.request.request_id) + " recorded.";
          }
          return refreshDashboardAfterMutation(payload, status, "Runner settings applied.");
        });
      }).catch(function (error) {
        if (status) {
          status.textContent = error.message;
        }
      });
    });
  }

  function runnerFromPayload(payload, runnerId) {
    var runners = payload && payload.runner_configuration && Array.isArray(payload.runner_configuration.runners) ?
      payload.runner_configuration.runners : [];
    for (var index = 0; index < runners.length; index += 1) {
      if (String(runners[index].runner_id || "") === String(runnerId || "")) {
        return runners[index];
      }
    }
    return runners[0] || {};
  }

  function fillRunnerConfigForm(form, runner) {
    if (!form || !runner) {
      return;
    }
    setFormValue(form, "role", runner.role || "");
    setFormValue(form, "adapter", runner.adapter || "");
    setFormValue(form, "command", "");
    setFormValue(form, "model", runner.model || "");
    setFormValue(form, "reasoning_effort", runner.reasoning_effort || "");
    setFormValue(form, "prompt_delivery_mode", runner.prompt_delivery_mode || "");
    setFormValue(form, "timeout_seconds", runner.timeout_seconds || "");
  }

  function setFormValue(form, name, value) {
    var field = form.querySelector('[name="' + name + '"]');
    if (field) {
      field.value = value;
    }
  }

  function mountPlanningControlForm(payload) {
    var form = document.getElementById("planning-control-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("planning-control-status");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
    });
    Array.prototype.slice.call(form.querySelectorAll("[data-planning-action]")).forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        if (button.disabled) {
          return;
        }
        submitPlanningRequest(payload, form, button, status);
      });
    });
  }

  function submitPlanningRequest(payload, form, button, status) {
    var endpoint = button.getAttribute("data-endpoint") || "";
    var action = button.getAttribute("data-planning-action") || "";
    if (!endpoint || !window.fetch) {
      if (status) {
        status.textContent = "Request endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var body = {
      source: "dashboard_ui",
      request_channel: "dashboard_requests"
    };
    var reason = formValue(data.get("reason")).trim();
    if (reason) {
      body.reason = reason;
    }
    if (action === "plan") {
      body.runner_id = formValue(data.get("planner_runner_id")).trim() || "planner";
    } else if (action === "audit") {
      body.runner_id = formValue(data.get("auditor_runner_id")).trim() || "auditor";
    } else if (action === "activate_plan") {
      body.plan = formValue(data.get("activation_source")).trim() || "PLAN_DRAFT.md";
    }
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Creating " + planningRequestLabel(action) + " request.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "request failed");
        }
        if (status) {
          status.textContent = "Request " + text(result.request && result.request.request_id) + " recorded.";
        }
        if (payload && payload.server_mode && payload.workflow_id) {
          return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
            applyPayload(nextPayload, "Request " + text(result.request && result.request.request_id) + " recorded.");
          });
        }
        return null;
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function mountExecutionControlForm(payload) {
    var form = document.getElementById("execution-control-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("execution-control-status");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
    });
    Array.prototype.slice.call(form.querySelectorAll("[data-control-action]")).forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        if (button.disabled) {
          return;
        }
        submitExecutionControlRequest(payload, form, button, status);
      });
    });
  }

  function submitExecutionControlRequest(payload, form, button, status) {
    var endpoint = button.getAttribute("data-endpoint") || "";
    var action = button.getAttribute("data-control-action") || "";
    if (!endpoint || !window.fetch) {
      if (status) {
        status.textContent = "Request endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var body = {
      type: action,
      source: "dashboard_ui",
      request_channel: "control_requests"
    };
    var reason = formValue(data.get("reason")).trim();
    if (reason) {
      body.reason = reason;
    }
    if (action === "start") {
      body.detach = true;
    }
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Creating " + action + " request.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "request failed");
        }
        if (status) {
          status.textContent = "Request " + text(result.request && result.request.request_id) + " recorded.";
        }
        if (payload && payload.server_mode && payload.workflow_id) {
          return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
            applyPayload(nextPayload, "Request " + text(result.request && result.request.request_id) + " recorded.");
          });
        }
        return null;
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function mountReadModelRebuildForm(payload) {
    var form = document.getElementById("read-model-rebuild-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("read-model-rebuild-status");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitReadModelRebuildRequest(payload, form, status);
    });
  }

  function submitReadModelRebuildRequest(payload, form, status) {
    var rebuild = payload && payload.read_model_rebuild && typeof payload.read_model_rebuild === "object" ? payload.read_model_rebuild : {};
    var freshness = payload && payload.read_model_freshness && typeof payload.read_model_freshness === "object" ? payload.read_model_freshness : {};
    var endpoint = form.getAttribute("data-endpoint") || rebuild.endpoint || "";
    var rebuildInProgress = readModelRebuildInProgress(rebuild);
    var liveRefreshExpected = readModelLiveRefreshExpected(payload, rebuild);
    if (rebuildInProgress || liveRefreshExpected || rebuild.request_allowed === false) {
      if (status) {
        if (liveRefreshExpected) {
          status.textContent = "The live dashboard is refreshing read models now. Wait for the page to update before requesting another rebuild.";
        } else if (rebuildInProgress) {
          status.textContent = "A read-model rebuild is already queued or running. Wait for the runtime to finish, then refresh the dashboard.";
        } else {
          status.textContent = "Read-model rebuild requests are disabled for this workflow.";
        }
      }
      return;
    }
    if (!endpoint || !window.fetch) {
      if (status) {
        status.textContent = "Read-model rebuild request endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var body = {
      type: "rebuild_read_models",
      source: "dashboard_ui",
      request_channel: "control_requests",
      freshness_status: freshness.status || rebuild.freshness_status || "unknown"
    };
    var reason = formValue(data.get("reason")).trim();
    if (reason) {
      body.reason = reason;
    }
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Creating read-model rebuild request.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "read-model rebuild request failed");
        }
        if (status) {
          status.textContent = "Request " + text(result.request && result.request.request_id) + " recorded.";
        }
        form.reset();
        if (payload && payload.server_mode && payload.workflow_id) {
          return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
            applyPayload(nextPayload, "Read-model rebuild request " + text(result.request && result.request.request_id) + " recorded.");
          });
        }
        return null;
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function mountApprovalResponseForms(payload) {
    Array.prototype.slice.call(document.querySelectorAll(".approval-response-form")).forEach(function (form) {
      if (form.getAttribute("data-mounted") === "true") {
        return;
      }
      form.setAttribute("data-mounted", "true");
      var status = form.querySelector(".approval-response-status");
      form.addEventListener("submit", function (event) {
        event.preventDefault();
      });
      Array.prototype.slice.call(form.querySelectorAll("[data-approval-decision]")).forEach(function (button) {
        button.addEventListener("click", function (event) {
          event.preventDefault();
          if (button.disabled) {
            return;
          }
          submitApprovalResponse(payload, form, button, status);
        });
      });
    });
  }

  function submitApprovalResponse(payload, form, button, status) {
    var endpoint = form.getAttribute("data-endpoint") || "";
    var decision = button.getAttribute("data-approval-decision") || "";
    var workflowId = payload && payload.workflow_id ? payload.workflow_id : "";
    if (!endpoint || !window.fetch || !workflowId) {
      if (status) {
        status.textContent = "Approval response endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var body = {
      decision: decision,
      source: "dashboard_ui"
    };
    var scope = formValue(data.get("scope")).trim();
    var notes = formValue(data.get("notes")).trim();
    if (scope) {
      body.scope = scope;
    }
    if (notes) {
      body.notes = notes;
    }
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Submitting " + decision + " response.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "approval response failed");
        }
        if (status) {
          status.textContent = "Approval " + text(result.approval && result.approval.approval_id) + " recorded as " + text(result.status) + ".";
        }
        return fetchApprovalStatus(workflowId).then(function (statusPayload) {
          var nextPayload = normalizePayload({
            approval_controls: approvalControlsFromStatus(
              statusPayload,
              payload.approval_controls || {},
              workflowId
            )
          }, workflowId);
          applyPayload(nextPayload, "Approval response recorded. Approval list refreshed.");
        });
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function mountInspectorConsoleForms(payload) {
    mountInspectorChatForm(payload);
    mountChangeRequestForm(payload);
  }

  function mountInspectorChatForm(payload) {
    var form = document.getElementById("inspector-chat-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("inspector-chat-status");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitInspectorChat(payload, form, status);
    });
  }

  function submitInspectorChat(payload, form, status) {
    var endpoint = form.getAttribute("data-endpoint") || "";
    if (!endpoint || !window.fetch) {
      if (status) {
        status.textContent = "Inspector chat endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var message = formValue(data.get("message")).trim();
    if (!message) {
      if (status) {
        status.textContent = "Question is required.";
      }
      return;
    }
    var body = {
      message: message,
      runner_id: formValue(data.get("runner_id")).trim() || "inspector",
      source: "dashboard_ui"
    };
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Running inspector agent.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "inspector chat failed");
        }
        if (status) {
          status.textContent = result.response && (result.response.answer || result.response.summary) ?
            "Inspector answered." :
            "Inspector agent finished for " + text(result.request && result.request.request_id) + ".";
        }
        form.reset();
        if (payload && payload.server_mode && payload.workflow_id) {
          return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
            applyPayload(nextPayload, "Inspector answered.");
          });
        }
        return null;
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function mountChangeRequestForm(payload) {
    var form = document.getElementById("change-request-form");
    if (!form || form.getAttribute("data-mounted") === "true") {
      return;
    }
    form.setAttribute("data-mounted", "true");
    var status = document.getElementById("change-request-status");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitChangeRequest(payload, form, status);
    });
  }

  function submitChangeRequest(payload, form, status) {
    var endpoint = form.getAttribute("data-endpoint") || "";
    if (!endpoint || !window.fetch) {
      if (status) {
        status.textContent = "Change request endpoint unavailable.";
      }
      return;
    }
    var data = new FormData(form);
    var userRequest = formValue(data.get("user_request")).trim();
    if (!userRequest) {
      if (status) {
        status.textContent = "Change request text is required.";
      }
      return;
    }
    var body = {
      user_request: userRequest,
      source: "dashboard_ui"
    };
    var token = tokenValue();
    var headers = {"Accept": "application/json", "Content-Type": "application/json"};
    if (token) {
      headers["X-LoopPlane-Token"] = token;
    }
    if (status) {
      status.textContent = "Creating change request.";
    }
    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok || !result.ok) {
          throw new Error((result.errors && result.errors.join("; ")) || result.status || "change request failed");
        }
        if (status) {
          status.textContent = "Change request " + text(result.change_request_id) + " recorded.";
        }
        form.reset();
        if (payload && payload.server_mode && payload.workflow_id) {
          return fetchDashboardData(payload.workflow_id).then(function (nextPayload) {
            applyPayload(nextPayload, "Change request " + text(result.change_request_id) + " recorded.");
          });
        }
        return null;
      });
    }).catch(function (error) {
      if (status) {
        status.textContent = error.message;
      }
    });
  }

  function preferredTheme() {
    try {
      var stored = window.localStorage && window.localStorage.getItem("loopplane-dashboard-theme");
      if (stored === "light" || stored === "dark") {
        return stored;
      }
    } catch (error) {
      return "dark";
    }
    return "dark";
  }

  function setTheme(theme) {
    var next = theme === "light" ? "light" : "dark";
    var root = document.documentElement || document.body;
    if (root && root.setAttribute) {
      root.setAttribute("data-theme", next);
    }
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
      button.textContent = next === "dark" ? "Light mode" : "Dark mode";
    }
    var logos = document.querySelectorAll ? document.querySelectorAll("[data-logo-dark][data-logo-light]") : [];
    for (var index = 0; index < logos.length; index += 1) {
      var logo = logos[index];
      var src = logo.getAttribute(next === "light" ? "data-logo-light" : "data-logo-dark");
      if (src && logo.getAttribute("src") !== src) {
        logo.setAttribute("src", src);
      }
    }
    try {
      if (window.localStorage) {
        window.localStorage.setItem("loopplane-dashboard-theme", next);
      }
    } catch (error) {
      return;
    }
  }

  function mountThemeToggle() {
    setTheme(preferredTheme());
    var button = document.getElementById("theme-toggle");
    if (!button || button.getAttribute("data-mounted") === "true") {
      return;
    }
    button.setAttribute("data-mounted", "true");
    button.addEventListener("click", function () {
      var root = document.documentElement || document.body;
      var current = root && root.getAttribute ? root.getAttribute("data-theme") : "dark";
      setTheme(current === "dark" ? "light" : "dark");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    mountThemeToggle();
    setDashboardLoading("Loading dashboard", "Preparing the workflow snapshot and rendering the dashboard.");
    var payloadNode = readPayloadNode();
    if (!payloadNode) {
      setDashboardLoading("Dashboard payload missing", "The embedded dashboard snapshot could not be loaded.");
      return;
    }
    if (payloadNode.payload_encoding !== "gzip+base64") {
      mountDashboardPayload(payloadNode);
      hideDashboardLoading();
      return;
    }
    decompressPayload(payloadNode).then(function (payload) {
      mountDashboardPayload(payload);
      hideDashboardLoading();
    }).catch(function (error) {
      if (window.console && window.console.error) {
        window.console.error(error);
      }
      setDashboardLoading("Dashboard failed to load", error && error.message ? error.message : "Unable to load the dashboard snapshot.");
    });
  });
}());
