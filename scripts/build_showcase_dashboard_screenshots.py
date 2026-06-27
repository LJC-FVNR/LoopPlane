#!/usr/bin/env python3
"""Capture showcase screenshots from the real LoopPlane dashboard UI.

The screenshots are rendered by the dashboard's HTML/CSS/JS. The only synthetic
piece is a deterministic read-model fixture that moves an existing completed
workflow snapshot back into an in-progress objective-verification state.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = ROOT / "artifacts" / "showcase"
SOURCE_INDEX = SHOWCASE / "dashboard_test2" / "index.html"
DASHBOARD_PUBLIC = ROOT / "dashboard" / "public"

PF02_WORKER_NODE = "node_worker_PF02_run_20260616_093357_87635d94"
FINAL_OBJECTIVE_VERIFIER_NODE = "node_objective_verifier_run_20260616_093404_0addfe8a"


class PayloadError(RuntimeError):
    pass


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required for real dashboard screenshots. Install with:\n"
            "  python3 -m pip install playwright\n"
            "  python3 -m playwright install chromium"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="loopplane-dashboard-showcase-") as temp_dir:
        fixture_dir = Path(temp_dir)
        index_path = _build_fixture(fixture_dir)
        url = index_path.resolve().as_uri()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(device_scale_factor=1)
                compact_graph_path = fixture_dir / "dashboard_graph_panel_compact.png"
                _capture_plan_review(context, url)
                _capture_compact_graph_panel(context, url, compact_graph_path)
                _capture_workflow_graph(context, url)
                _capture_active_monitoring(context, url)
            finally:
                browser.close()
        _compose_dashboard_overview(compact_graph_path)
    return 0


def _build_fixture(fixture_dir: Path) -> Path:
    if not SOURCE_INDEX.exists():
        raise PayloadError(f"Missing source dashboard bundle: {SOURCE_INDEX}")
    if not DASHBOARD_PUBLIC.exists():
        raise PayloadError(f"Missing dashboard public assets: {DASHBOARD_PUBLIC}")

    html = SOURCE_INDEX.read_text(encoding="utf-8")
    payload = _extract_payload(html)
    _mutate_to_midrun_fixture(payload)
    html = _replace_payload(html, payload)
    html = _replace_workspace_selector(html, payload)

    index_path = fixture_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    for name in (
        "static_dashboard.css",
        "static_dashboard.js",
        "loopplane_logo.svg",
        "loopplane_logo.png",
        "loopplane_logo_light.png",
        "loopplane_logo_dark.png",
    ):
        source = DASHBOARD_PUBLIC / name
        if source.exists():
            shutil.copy2(source, fixture_dir / name)

    return index_path


def _extract_payload(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="loopplane-read-models" type="application/json">([\s\S]*?)</script>',
        html,
    )
    if not match:
        raise PayloadError("Could not find embedded dashboard payload.")
    outer = json.loads(match.group(1))
    if outer.get("payload_encoding") == "gzip+base64":
        compressed = outer.get("payload_compressed") or outer.get("payload")
        if not compressed:
            raise PayloadError("Compressed payload marker exists, but no payload field was found.")
        return json.loads(gzip.decompress(base64.b64decode(compressed)))
    return outer


def _replace_payload(html: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    outer = {
        "payload_compressed": base64.b64encode(compressed).decode("ascii"),
        "payload_compressed_bytes": len(compressed),
        "payload_encoding": "gzip+base64",
        "payload_schema_version": payload.get("schema_version"),
        "payload_uncompressed_bytes": len(raw),
        "schema_version": payload.get("schema_version"),
        "status": payload.get("read_models", {}).get("workflow_status.json", {}).get("status"),
        "workflow_id": payload.get("workflow_id"),
        "workspace": payload.get("workspace"),
    }
    script = json.dumps(outer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return re.sub(
        r'(<script id="loopplane-read-models" type="application/json">)([\s\S]*?)(</script>)',
        lambda match: f"{match.group(1)}{script}{match.group(3)}",
        html,
        count=1,
    )


def _replace_workspace_selector(html: str, payload: dict[str, Any]) -> str:
    sys.path.insert(0, str(ROOT))
    from runtime.dashboard import _render_workspace_selector

    selector_html = _render_workspace_selector(payload)
    rendered, count = re.subn(
        r'<section class="workflow-selector-shell"[\s\S]*?</section>',
        selector_html,
        html,
        count=1,
    )
    if count != 1:
        raise PayloadError("Could not replace the pre-rendered workflow selector.")
    return rendered


def _mutate_to_midrun_fixture(payload: dict[str, Any]) -> None:
    read_models = _dict(payload.get("read_models"))
    workflow_status = _dict(read_models.get("workflow_status.json"))
    plan_index = _dict(read_models.get("plan_index.json"))
    graph = _dict(read_models.get("workflow_graph.json"))
    metrics = _dict(read_models.get("metrics.json"))
    version_control = _dict(read_models.get("version_control_status.json"))

    payload["rendered_at"] = "2026-06-16T09:34:05Z"
    payload["workflow_title"] = "Civic Analytics Portfolio"

    workflow_status["status"] = "running"
    workflow_status["phase"] = "objective_verification"
    workflow_status["active_run_id"] = "run_20260616_093404_0addfe8a"
    workflow_status["active_task_id"] = None
    workflow_status["current_activity"] = {
        "started_at": "2026-06-16T09:34:04Z",
        "title": "Objective verifier reviewing final dashboard handoff evidence",
        "type": "objective_verifier",
    }
    workflow_status["progress"] = {
        "blocked_tasks": 0,
        "completed_tasks": 2,
        "partial_tasks": 0,
        "progress_percent": 92.0,
        "skipped_tasks": 0,
        "total_tasks": 2,
    }
    workflow_status["objective_progress"] = {
        "closed": 0,
        "needs_expansion": 0,
        "needs_verification": 1,
        "objective_unresolved": 0,
        "open": 1,
        "parse_error_count": 0,
        "total": 2,
    }
    workflow_status["requires_attention"] = [
        {
            "kind": "objective_gate",
            "message": "Final objective gate is reviewing worker evidence before completion.",
            "status": "running",
        }
    ]
    _mark_objectives_pending(workflow_status.get("objectives"))
    _mark_status_source(workflow_status)

    for phase in _list(plan_index.get("phases")):
        phase["status"] = "running"
        _mark_objectives_pending(phase.get("objectives"))
        for task in _list(phase.get("tasks")):
            _mark_task_done(task)
    _mark_objectives_pending(plan_index.get("objectives"))
    for task in _list(plan_index.get("tasks")):
        _mark_task_done(task)
    _mark_status_source(plan_index)

    counts = _dict(metrics.get("counts"))
    counts["tasks_done"] = 2
    counts["tasks_total"] = 2
    counts["runs_total"] = max(int(counts.get("runs_total") or 0), 9)
    _mark_status_source(metrics)

    checkpoint = _dict(version_control.get("latest_checkpoint"))
    checkpoint["reason"] = "after_objective_gate_started"
    version_control["latest_checkpoint"] = checkpoint
    _mark_status_source(version_control)

    current_workflow: dict[str, Any] | None = None
    for workflow in _list(payload.get("workflows")):
        if workflow.get("workflow_id") == payload.get("workflow_id"):
            workflow["status"] = "running"
            workflow["completed_at"] = None
            workflow["completion_marker"] = None
            workflow["final_verification_report"] = None
            workflow["summary"] = {
                "one_line": "Workflow is running: 2/2 tasks complete, final objective gate in progress.",
                "tasks_blocked": 0,
                "tasks_completed": 2,
                "tasks_skipped": 0,
                "tasks_total": 2,
            }
            current_workflow = workflow

    _mutate_graph(graph)
    _mutate_node_details(payload)
    if current_workflow:
        _sync_current_workflow_snapshot(payload, current_workflow, read_models)


def _sync_current_workflow_snapshot(
    payload: dict[str, Any],
    workflow: dict[str, Any],
    read_models: dict[str, Any],
) -> None:
    snapshots = _dict(payload.get("workflow_snapshots"))
    workflow_id = str(payload.get("workflow_id") or "")
    snapshot = _dict(snapshots.get(workflow_id))
    if not snapshot:
        return
    snapshot["workflow"] = copy.deepcopy(workflow)
    snapshot["workflow_title"] = payload.get("workflow_title") or "Civic Analytics Portfolio"
    snapshot["read_models"] = copy.deepcopy(read_models)
    snapshot["status"] = "ok"
    snapshots[workflow_id] = snapshot
    payload["workflow_snapshots"] = snapshots


def _mark_task_done(task: dict[str, Any]) -> None:
    task["checkbox"] = "[x]"
    task["status"] = "done"
    task["display"] = {
        "badge": "pass",
        "highlight": "",
        "subtitle": "Validated evidence captured.",
    }


def _mark_objectives_pending(objectives: Any) -> None:
    for objective in _list(objectives):
        objective_id = str(objective.get("objective_id") or "")
        objective["checkbox"] = "[ ]"
        objective["closed"] = False
        objective["expandable"] = True
        objective["plan_status"] = "needs_verification" if objective_id == "PF.O1" else "open"
        objective["report_status"] = "running" if objective_id == "FO1" else "needs_verification"
        objective["status"] = "running" if objective_id == "FO1" else "needs_verification"
        objective["verified_at"] = None
        objective["result"] = {
            "agent_rationale": "Worker evidence is present; the objective gate is still reviewing whether it is sufficient for handoff.",
            "confidence": "pending",
            "evidence_reviewed": objective.get("result", {}).get("evidence_reviewed", []),
            "expandable": True,
            "gap_summary": "",
            "objective_id": objective_id,
            "status": "running" if objective_id == "FO1" else "needs_verification",
            "unmet_action": objective.get("unmet_action") or "self_expand",
            "verdict": "pending",
        }


def _mutate_graph(graph: dict[str, Any]) -> None:
    for node in _list(graph.get("nodes")):
        node_id = node.get("node_id")
        if node_id == "objective_PF_O1":
            node["status"] = "needs_verification"
            node["summary"] = {
                "highlights": ["Worker evidence captured; objective gate is reviewing sufficiency."],
                "one_line": "Objective PF.O1: needs verification",
                "risks": [],
            }
        elif node_id == "objective_FO1":
            node["status"] = "running"
            node["summary"] = {
                "highlights": ["Final handoff objective is under review."],
                "one_line": "Objective FO1: objective verifier running",
                "risks": [],
            }
        elif node_id == FINAL_OBJECTIVE_VERIFIER_NODE:
            node["status"] = "running"
            node["title"] = "Objective verifier reviewing dashboard handoff evidence."
            node["summary"] = {
                "highlights": [
                    "PF01 and PF02 evidence is available.",
                    "Final objective gate is deciding whether the workflow can exit.",
                ],
                "one_line": "Final objective verifier is running.",
                "risks": [],
            }
        elif node_id in {"node_event_000000000086", "node_event_000000000087"}:
            node["status"] = "running"
            node["title"] = "Event: Objective Verifier Running"
            node["summary"] = {
                "highlights": [],
                "one_line": "Objective verifier is reviewing current workflow evidence.",
                "risks": [],
            }
        elif node_id in {"node_event_000000000094", "node_event_000000000095"}:
            node["status"] = "pending"
            node["title"] = "Event: Completion Pending Objective Gate"
            node["summary"] = {
                "highlights": [],
                "one_line": "Workflow completion is waiting on final objective verification.",
                "risks": [],
            }
    _mark_status_source(graph)


def _mutate_node_details(payload: dict[str, Any]) -> None:
    node_details = _dict(payload.get("node_details"))
    nodes = _dict(node_details.get("nodes"))
    detail = _dict(nodes.get(FINAL_OBJECTIVE_VERIFIER_NODE))
    if detail:
        sections = _list(detail.get("sections"))
        sections.insert(
            0,
            {
                "available": True,
                "content": "Final objective verification is running. The verifier is checking whether worker evidence and dashboard read models satisfy the handoff objective before LoopPlane exits.",
                "key": "runtime_state",
                "title": "Runtime State",
            },
        )
        detail["sections"] = sections
        detail["available_sections"] = ["runtime_state", *list(detail.get("available_sections") or [])]
        nodes[FINAL_OBJECTIVE_VERIFIER_NODE] = detail
    node_details["nodes"] = nodes
    payload["node_details"] = node_details


def _mark_status_source(model: dict[str, Any]) -> None:
    model["generated_at"] = "2026-06-16T09:34:05Z"
    model["last_event_seq"] = min(int(model.get("last_event_seq") or 101), 95)
    model["source_event_id"] = "evt_000000000095"


def _capture_plan_review(context: Any, url: str) -> None:
    page = _new_page(context, 1792, 790)
    _goto_dashboard(page, url)
    page.screenshot(path=str(SHOWCASE / "dashboard_plan_review.png"), full_page=False)
    page.close()


def _capture_compact_graph_panel(context: Any, url: str, output_path: Path) -> None:
    page = _new_page(context, 1792, 1400)
    _goto_dashboard(page, url)
    _select_node(page, FINAL_OBJECTIVE_VERIFIER_NODE)
    page.locator(".graph-panel").first.screenshot(path=str(output_path))
    page.close()


def _capture_workflow_graph(context: Any, url: str) -> None:
    page = _new_page(context, 1792, 1280)
    _goto_dashboard(page, url)
    _expand_graph(page)
    _select_node(page, FINAL_OBJECTIVE_VERIFIER_NODE)
    page.evaluate("document.querySelector('.graph-panel').scrollIntoView({block: 'start'});")
    page.wait_for_timeout(150)
    page.screenshot(path=str(SHOWCASE / "dashboard_midrun_graph.png"), full_page=False)
    page.close()


def _capture_active_monitoring(context: Any, url: str) -> None:
    page = _new_page(context, 1792, 1180)
    _goto_dashboard(page, url)
    _expand_graph(page)
    _select_node(page, PF02_WORKER_NODE)
    page.evaluate("document.querySelector('.detail-panel').scrollIntoView({block: 'start'});")
    page.wait_for_timeout(150)
    page.screenshot(path=str(SHOWCASE / "dashboard_active_monitoring.png"), full_page=False)
    page.close()


def _compose_dashboard_overview(compact_graph_path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to compose the dashboard overview collage. Install with:\n"
            "  python3 -m pip install Pillow"
        ) from exc

    canvas = Image.new("RGB", (1600, 900), "#07111f")
    draw = ImageDraw.Draw(canvas)
    _draw_dashboard_background(draw, width=1600, height=900)

    font_regular = _font(ImageFont, 18)
    font_small = _font(ImageFont, 13)
    font_small_bold = _font(ImageFont, 13, bold=True)
    font_body = _font(ImageFont, 21)
    font_h1 = _font(ImageFont, 52, bold=True)
    font_card = _font(ImageFont, 27, bold=True)

    logo_path = DASHBOARD_PUBLIC / "loopplane_logo_dark.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((62, 62), Image.Resampling.LANCZOS)
        canvas.paste(logo, (48, 56), logo)

    draw.text((122, 62), "LOOPPLANE DASHBOARD", fill="#93c5fd", font=font_small_bold)
    draw.text((122, 84), "Plan, evidence, graph, and handoff state", fill="#f8fafc", font=font_regular)

    draw.multiline_text(
        (48, 150),
        "Review the loop\nwhile agents work.",
        fill="#f8fafc",
        font=font_h1,
        spacing=6,
    )
    _wrapped_text(
        draw,
        "LoopPlane turns agent execution into a local review surface: inspect the plan, watch objective gates, open run evidence, and follow the workflow graph without digging through runtime files.",
        xy=(50, 345),
        max_width=510,
        font=font_body,
        fill="#b8c7dd",
        line_gap=8,
    )

    card_specs = [
        ("Plan review", "tasks, status, approvals"),
        ("Objective gates", "phase + final checks"),
        ("Evidence trail", "runs, artifacts, validation"),
        ("Workflow graph", "agent pipeline at a glance"),
    ]
    for index, (title, subtitle) in enumerate(card_specs):
        col = index % 2
        row = index // 2
        box = (48 + col * 276, 570 + row * 116, 308 + col * 276, 676 + row * 116)
        _panel(draw, box, fill="#222c3d", outline="#334155", radius=12)
        draw.text((box[0] + 18, box[1] + 18), title, fill="#f8fafc", font=font_card)
        draw.text((box[0] + 18, box[1] + 62), subtitle.upper(), fill="#b9d4f7", font=font_small_bold)

    plan = Image.open(SHOWCASE / "dashboard_plan_review.png").convert("RGB")
    graph = Image.open(compact_graph_path).convert("RGB")
    active = Image.open(SHOWCASE / "dashboard_active_monitoring.png").convert("RGB")

    shell = (690, 48, 1552, 830)
    _shadowed_panel(canvas, ImageDraw, ImageFilter, shell, radius=24)
    draw = ImageDraw.Draw(canvas)
    _browser_bar(draw, shell, font_small)

    _paste_cover_card(
        canvas,
        draw,
        plan,
        box=(722, 118, 1520, 466),
        crop=(0, 0, 1792, 790),
        radius=14,
    )
    _paste_cover_card(
        canvas,
        draw,
        graph,
        box=(722, 494, 1176, 806),
        crop=(0, 0, 1120, 735),
        centering=(0.0, 0.0),
        radius=14,
    )
    _paste_cover_card(
        canvas,
        draw,
        active,
        box=(1198, 494, 1520, 806),
        crop=(0, 0, 450, 420),
        centering=(0.0, 0.0),
        radius=14,
    )

    canvas.save(SHOWCASE / "dashboard_showcase_overview.png", "PNG")


def _draw_dashboard_background(draw: Any, *, width: int, height: int) -> None:
    draw.rectangle((0, 0, width, height), fill="#07111f")
    for x in range(0, width + 44, 44):
        draw.line((x, 0, x, height), fill="#0d2840", width=1)
    for y in range(0, height + 44, 44):
        draw.line((0, y, width, y), fill="#0d2840", width=1)
    draw.rectangle((0, 0, width, height), outline="#111a2d", width=1)


def _font(image_font: Any, size: int, *, bold: bool = False) -> Any:
    family = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu") / family,
        Path("/usr/local/share/fonts") / family,
    ]
    for path in candidates:
        if path.exists():
            return image_font.truetype(str(path), size)
    return image_font.load_default()


def _wrapped_text(
    draw: Any,
    value: str,
    *,
    xy: tuple[int, int],
    max_width: int,
    font: Any,
    fill: str,
    line_gap: int,
) -> None:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if not current or draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += font.size + line_gap


def _panel(
    draw: Any,
    box: tuple[int, int, int, int],
    *,
    fill: str,
    outline: str,
    radius: int,
    width: int = 1,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _shadowed_panel(
    canvas: Any,
    image_draw: Any,
    image_filter: Any,
    box: tuple[int, int, int, int],
    *,
    radius: int,
) -> None:
    from PIL import Image

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = image_draw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (box[0], box[1] + 18, box[2], box[3] + 18),
        radius=radius,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(image_filter.GaussianBlur(24))
    canvas.paste(shadow.convert("RGB"), mask=shadow.split()[-1])
    image_draw.Draw(canvas).rounded_rectangle(
        box,
        radius=radius,
        fill="#0b1323",
        outline="#2a3a57",
        width=2,
    )


def _browser_bar(draw: Any, box: tuple[int, int, int, int], font: Any) -> None:
    x1, y1, x2, _ = box
    draw.rounded_rectangle((x1 + 24, y1 + 20, x2 - 24, y1 + 64), radius=10, fill="#0f172a", outline="#26344d")
    for index, color in enumerate(("#fb7185", "#facc15", "#34d399")):
        draw.ellipse((x1 + 44 + index * 18, y1 + 36, x1 + 54 + index * 18, y1 + 46), fill=color)
    draw.rounded_rectangle((x1 + 126, y1 + 31, x2 - 46, y1 + 53), radius=11, fill="#111827", outline="#26344d")
    draw.text((x1 + 144, y1 + 34), "local dashboard: plan, graph, evidence, handoff", fill="#9fb0c9", font=font)


def _paste_cover_card(
    canvas: Any,
    draw: Any,
    source: Any,
    *,
    box: tuple[int, int, int, int],
    crop: tuple[int, int, int, int] | None = None,
    centering: tuple[float, float] = (0.5, 0.0),
    radius: int,
) -> None:
    from PIL import Image, ImageDraw, ImageOps

    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1
    frame = source.crop(crop) if crop else source
    frame = ImageOps.fit(frame, (width, height), Image.Resampling.LANCZOS, centering=centering)
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
    canvas.paste(frame, (x1, y1), mask)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, outline="#2b3a56", width=2)


def _paste_contain_card(
    canvas: Any,
    draw: Any,
    source: Any,
    *,
    box: tuple[int, int, int, int],
    radius: int,
) -> None:
    from PIL import Image, ImageDraw, ImageOps

    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1
    base = Image.new("RGB", (width, height), "#0b1323")
    contained = ImageOps.contain(source, (width - 22, height - 22), Image.Resampling.LANCZOS)
    px = (width - contained.width) // 2
    py = (height - contained.height) // 2
    base.paste(contained, (px, py))
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
    canvas.paste(base, (x1, y1), mask)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, outline="#2b3a56", width=2)


def _new_page(context: Any, width: int, height: int) -> Any:
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": height})
    return page


def _goto_dashboard(page: Any, url: str) -> None:
    page.goto(url, wait_until="networkidle")
    page.add_style_tag(
        content="""
        *, *::before, *::after {
          animation: none !important;
          transition: none !important;
          scroll-behavior: auto !important;
        }
        """
    )
    page.wait_for_selector("#graph-panel-body .graph-node", state="visible")
    page.wait_for_timeout(250)


def _expand_graph(page: Any) -> None:
    button = page.locator("[data-graph-expand-toggle]")
    if button.count():
        button.first.click()
        page.wait_for_timeout(100)


def _select_node(page: Any, node_id: str) -> None:
    locator = page.locator(f'[data-node-id="{node_id}"]').first
    locator.click()
    page.wait_for_timeout(100)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


if __name__ == "__main__":
    raise SystemExit(main())
