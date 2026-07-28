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
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


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
    """Generate a Markdown report summarizing the entire project."""
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}
    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")

    status_icon = {"active": "🟢", "stopped": "🟡", "completed": "✅"}
    icon = status_icon.get(proj["status"], "⚪")

    lines = []
    lines.append(f"# {proj['title']} 安全测试报告\n")
    lines.append(f"**项目状态**: {icon} {proj['status']}\n")
    if origin_desc:
        lines.append(f"**测试目标**: `{origin_desc}`\n")
    if goal_desc:
        lines.append(f"**测试目的**: {goal_desc}\n")
    lines.append(f"**创建时间**: {format_export_timestamp(proj['created_at'])}\n")
    lines.append(f"**事实总数**: {len(facts)} | **探索方向**: {len(intents)}\n")
    lines.append("---\n")

    # Hints section
    if hints:
        lines.append("## 💡 提示\n")
        for h in hints:
            lines.append(f"- **{h['creator']}** ({format_export_timestamp(h['created_at'])}): {h['content']}")
        lines.append("")
        lines.append("---\n")

    # Findings (non-origin/goal facts)
    findings = [f for f in facts if f["id"] not in ("origin", "goal")]
    if findings:
        lines.append("## 📊 发现汇总\n")
        for f in findings:
            # Find which intent produced this fact
            producer = None
            for i in intents:
                if i["to_fact_id"] == f["id"]:
                    producer = i
                    break
            label = f"**{f['id']}**"
            if producer:
                label += f" — 来自 {producer['id']}"
            lines.append(f"### {label}\n")
            lines.append(f"{f['description']}\n")
        lines.append("---\n")

    # Exploration history
    lines.append("## 🔍 探索记录\n")

    completed_intents = [i for i in intents if i["concluded_at"] and i["to_fact_id"] not in (None, "goal")]
    running_intents = [i for i in intents if i["worker"] and not i["concluded_at"]]
    pending_intents = [i for i in intents if not i["worker"] and not i["concluded_at"] and i["description"] != "bootstrap"]

    if completed_intents:
        lines.append("### ✅ 已完成\n")
        for i in completed_intents:
            src = ", ".join(sources_by_intent.get(i["id"], []))
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            lines.append(f"- **{i['id']}**: {i['description']}")
            lines.append(f"  - 来源: {src} → 发现: {i['to_fact_id']}")
            if fact_desc:
                lines.append(f"  - {fact_desc[:120]}")
            lines.append(f"  - 执行: {i['worker']} ({format_export_timestamp(i['concluded_at'])})")
            lines.append("")
    if running_intents:
        lines.append("### 🔄 进行中\n")
        for i in running_intents:
            lines.append(f"- **{i['id']}**: {i['description']} (执行者: {i['worker']})")
        lines.append("")
    if pending_intents:
        lines.append("### ⏳ 待执行\n")
        for i in pending_intents:
            src = ", ".join(sources_by_intent.get(i["id"], []))
            lines.append(f"- **{i['id']}**: {i['description']} (来自: {src})")
        lines.append("")

    # Completion info
    completion_intent = next((i for i in intents if i["to_fact_id"] == "goal"), None)
    if completion_intent:
        lines.append("---\n")
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
