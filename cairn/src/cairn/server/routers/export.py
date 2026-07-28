from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from datetime import datetime
import yaml

from cairn.server.db import get_conn
from cairn.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    # Keep UTC — strip ISO format, append explicit UTC label for reproducibility
    ts = value.replace("T", " ")
    if ts.endswith("Z"):
        ts = ts[:-1] + " UTC"
    elif ts.endswith("+00:00"):
        ts = ts[:-6] + " UTC"
    return ts


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [{"id": f["id"], "description": f["description"]} for f in facts]

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


def _export_report(conn, project_id: str) -> str:
    """Generate a Markdown security assessment report matching standard vulnerability report format."""
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}
    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    status_icon = {"active": "🟢", "stopped": "🟡", "completed": "✅"}
    icon = status_icon.get(proj["status"], "⚪")

    lines = []
    # ── Report Header ──
    lines.append("# 安全评估报告\n")
    lines.append(f"- **项目**: {origin_desc or proj['title']}")
    lines.append(f"- **项目 ID**: {project_id}")
    lines.append(f"- **项目状态**: {icon} {proj['status']}")
    lines.append(f"- **导出时间**: {now_str}")
    lines.append(f"- **来源**: 当前项目实时报告\n")
    lines.append("---\n")

    # ── Section 1: 项目概况 ──
    lines.append(f"## 1. 项目概况\n")
    if goal_desc:
        lines.append(f"**评估目标**: {goal_desc}\n")
    if origin_desc:
        lines.append(f"**测试目标**: `{origin_desc}`\n")
    lines.append(f"**创建时间**: {format_export_timestamp(proj['created_at'])}\n")
    lines.append(f"**发现总数**: {len([f for f in facts if f['id'] not in ('origin', 'goal')])} | **探索方向**: {len(intents)}\n")


    # Hints
    if hints:
        lines.append("\n**人工提示**:\n")
        for h in hints:
            lines.append(f"- {h['creator']} ({format_export_timestamp(h['created_at'])}): {h['content']}")
        lines.append("")

    lines.append("\n## 2. 发现详情\n")

    # Findings (non-origin/goal facts) in vulnerability format
    findings = [f for f in facts if f["id"] not in ("origin", "goal")]
    if findings:
        for idx, f in enumerate(findings, 1):
            # Find producer intent
            producer = None
            for i in intents:
                if i["to_fact_id"] == f["id"]:
                    producer = i
                    break

            lines.append(f"### 发现 {idx}：{f['id']}\n")
            lines.append(f"- **发现名称**：{f['id']}")
            if origin_desc:
                lines.append(f"- **影响目标**：{origin_desc}")
            if producer:
                src = ", ".join(sources_by_intent.get(producer["id"], []))
                lines.append(f"- **来源**：{producer['id']} ({producer['description']})")
                lines.append(f"- **来源事实**：{src}")
                lines.append(f"- **执行器**：{producer['worker'] or '—'}")
                if producer["concluded_at"]:
                    lines.append(f"- **发现时间**：{format_export_timestamp(producer['concluded_at'])}")
            lines.append(f"- **详细描述**：{f['description']}\n")

    # ── Section 3: 探索记录 ──
    lines.append("## 3. 探索记录\n")

    completed_intents = [i for i in intents if i["concluded_at"] and i["to_fact_id"] not in (None, "goal")]
    running_intents = [i for i in intents if i["worker"] and not i["concluded_at"]]
    pending_intents = [i for i in intents if not i["worker"] and not i["concluded_at"] and i["description"] != "bootstrap"]
    bootstrap_intents = [i for i in intents if i["description"] == "bootstrap"]

    # Timeline in chronological order
    if bootstrap_intents:
        lines.append("### 1. 初始侦察\n")
        for i in bootstrap_intents:
            src = ", ".join(sources_by_intent.get(i["id"], []))
            lines.append(f"- **{i['id']}**：{i['description']}")
            if i["to_fact_id"] and i["to_fact_id"] in facts_by_id:
                lines.append(f"  - 发现：{i['to_fact_id']} — {facts_by_id[i['to_fact_id']][:120]}")
            if i["concluded_at"]:
                lines.append(f"  - 完成时间：{format_export_timestamp(i['concluded_at'])}")
        lines.append("")

    if completed_intents:
        lines.append("### 2. 深度探测\n")
        for i in completed_intents:
            src = ", ".join(sources_by_intent.get(i["id"], []))
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            lines.append(f"- **{i['id']}**：{i['description']}")
            lines.append(f"  - 来源：{src} → 发现：{i['to_fact_id']}")
            if fact_desc:
                lines.append(f"  - 证据：{fact_desc[:200]}")
            lines.append(f"  - 执行器：{i['worker']}（{format_export_timestamp(i['concluded_at'])}）")
        lines.append("")

    if running_intents:
        lines.append("### 🔄 进行中\n")
        for i in running_intents:
            lines.append(f"- **{i['id']}**：{i['description']}（执行者：{i['worker']}）")
        lines.append("")

    if pending_intents:
        lines.append("### ⏳ 待执行\n")
        for i in pending_intents:
            src = ", ".join(sources_by_intent.get(i["id"], []))
            lines.append(f"- **{i['id']}**：{i['description']}（来自：{src}）")
        lines.append("")

    # ── Section 4: 评估结论 ──
    lines.append("## 4. 评估结论\n")
    total_findings = len(findings)
    lines.append(f"本次评估共发现 **{total_findings} 个已确认的安全发现**。\n")

    if findings:
        lines.append("| 发现编号 | 描述摘要 | 来源意图 |\n")
        lines.append("|---------|---------|--------|\n")
        for f in findings:
            producer = None
            for i in intents:
                if i["to_fact_id"] == f["id"]:
                    producer = i
                    break
            desc_short = f["description"][:60] + "..." if len(f["description"]) > 60 else f["description"]
            source_label = producer["id"] if producer else "手动创建"
            lines.append(f"| {f['id']} | {desc_short} | {source_label} |\n")

    # Exploration stats
    lines.append(f"\n**探索统计**：\n")
    lines.append(f"- 已完成探索：{len(completed_intents)}")
    lines.append(f"- 进行中：{len(running_intents)}")
    lines.append(f"- 待执行：{len(pending_intents)}")
    lines.append(f"- 人工提示：{len(hints)}")

    # Completion info
    completion_intent = next((i for i in intents if i["to_fact_id"] == "goal"), None)
    if completion_intent:
        lines.append("\n---\n")
        lines.append("## 🏁 项目完成\n")
        lines.append(f"由 **{completion_intent['worker']}** 于 {format_export_timestamp(completion_intent['created_at'])} 完成\n")
        lines.append(f"{completion_intent['description']}\n")

    return "\n".join(lines)


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "report"):
        raise HTTPException(400, "Supported formats: yaml, timeline, report")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "report":
            text = _export_report(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")
