"""Mise en forme : instantané brut de no-mistakes → onglets et noeuds.

Fonctions pures, sans accès disque : `build_tabs` reçoit l'instantané, la
config déjà lue, une fonction qui rend la fin d'un log d'étape et une qui dit
si un pid existe encore.
"""

import json
import os

STEPS = ("intent", "rebase", "review", "test", "document", "lint", "push", "pr", "ci")

# Au-delà de ce silence, une étape active est signalée comme bloquée.
STALE_S = 15 * 60
INTENT_MAX = 4000

STATE_LABELS = {
    "attente": "en attente",
    "en_cours": "en cours",
    "correctif": "en correctif",
    "decision": "attend une décision",
    "fini": "fini",
    "echoue": "échoué",
    "saute": "sauté",
}

RUN_LABELS = {
    "en_cours": "en cours",
    "decision": "attend une décision",
    "fini": "fini",
    "echoue": "échoué",
    "annule": "annulé",
}

_STEP_STATES = {
    "pending": "attente",
    "running": "en_cours",
    "fixing": "correctif",
    "awaiting_approval": "decision",
    "fix_review": "decision",
    "completed": "fini",
    "failed": "echoue",
    "skipped": "saute",
}

_RUN_STATES = {
    "pending": "en_cours",
    "running": "en_cours",
    "completed": "fini",
    "failed": "echoue",
    "cancelled": "annule",
}

_LIVE = ("en_cours", "correctif", "decision")

_PASS_NAMES = {
    "intent": "lecture de l'intention",
    "rebase": "rebase",
    "review": "revue",
    "test": "tests",
    "document": "documentation",
    "lint": "lint",
    "push": "push",
    "pr": "pull request",
    "ci": "surveillance de la CI",
}

_PR_STATES = {
    "open": "ouverte",
    "merged": "fusionnée",
    "closed": "fermée",
    "draft": "brouillon",
}

_TRIGGER_LABELS = {"initial": "initial", "auto_fix": "correctif auto", "user": "demandé"}


def count_findings(findings_json):
    if not findings_json:
        return 0
    try:
        data = json.loads(findings_json)
    except ValueError:
        return 0
    found = data.get("findings") if isinstance(data, dict) else None
    return len(found) if isinstance(found, list) else 0


def parse_findings(findings_json):
    """Findings en forme légère : id, titre, sévérité, fichier, ligne."""
    if not findings_json:
        return []
    try:
        data = json.loads(findings_json)
    except ValueError:
        return []
    found = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(found, list):
        return []
    out = []
    for item in found:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        out.append(
            {
                "id": _text(item.get("id")) or _text(item.get("title")),
                "title": _text(item.get("title")),
                "severity": _text(item.get("severity")),
                "file": _text(item.get("file")),
                "line": line if isinstance(line, int) else None,
                "description": _text(item.get("description")),
            }
        )
    return out


def _selected_ids(selected_json):
    if not selected_json:
        return []
    try:
        ids = json.loads(selected_json)
    except ValueError:
        return []
    if not isinstance(ids, list):
        return []
    return [str(i) for i in ids]


def _text(value):
    text = str(value).strip() if value is not None else ""
    return text or None


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _flag_value(args, *flags):
    if not isinstance(args, list):
        return None
    for i, arg in enumerate(args[:-1]):
        if arg in flags:
            return str(args[i + 1])
    return None


def _role(config, step_name, fixing):
    if step_name != "review":
        return {}
    roles = config.get("review_agents") or {}
    return roles.get("fixer" if fixing else "reviewer") or {}


def _configured(config, role, tool):
    """Modèle et effort que la config donne à `tool` pour ce rôle."""
    same_as_role = tool == role.get("agent")
    per_agent = (config.get("agent_config") or {}).get(tool) or {}
    args = (config.get("agent_args_override") or {}).get(tool)
    model = (
        (role.get("model") if same_as_role else None)
        or per_agent.get("model")
        or _flag_value(args, "--model", "-m")
    )
    effort = (
        (role.get("effort") if same_as_role else None)
        or per_agent.get("effort")
        or _flag_value(args, "--thinking", "--effort")
    )
    return model, effort


def resolve_agent(config, step_name, fixing, log_tool, known_models):
    """Outil et modèle du noeud actif.

    L'outil vient du log d'étape quand il nomme un agent lancé, sinon de la
    config. Le modèle vient de la config, sinon du dernier appel connu de cet
    outil dans la base.
    """
    role = _role(config, step_name, fixing)
    configured = role.get("agent") or _first(config.get("agent"))
    tool = log_tool or configured
    if not tool or tool == "auto":
        return None
    model, effort = _configured(config, role, tool)
    source = "config"
    if not model and known_models.get(tool):
        model = known_models[tool]
        source = "dernier appel"
    return {
        "tool": tool,
        "model": model.split("/")[-1] if model else None,
        "effort": str(effort) if effort else None,
        "tool_source": "log" if log_tool else "config",
        "model_source": source if model else None,
    }


def _human_s(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = seconds / 3600
    return f"{hours:.1f} h".replace(".", ",")


def _round_label(step_name, state, rounds):
    """Un tour enregistré est un passage fini ; ceux hors `initial` suivent un correctif."""
    done = len(rounds)
    fixes = sum(1 for r in rounds if r.get("trigger_type") != "initial")
    name = _PASS_NAMES.get(step_name, step_name)
    if state == "correctif":
        return f"correctif {fixes + 1}"
    if state == "decision":
        return f"{name}, tour {max(done, 1)} : décision attendue"
    if done == 0:
        return f"{name}, premier passage"
    return f"{name}, tour {done + 1}"


def _order(step_name):
    return STEPS.index(step_name) if step_name in STEPS else len(STEPS)


# Raisons de reprise connues, lues dans le log de l'étape qui a renvoyé en arrière.
_RESTART_REASONS = (
    ("cannot prove the repaired head",
     "la réparation de CI a réécrit l'historique : ce n'est plus le code relu, "
     "no-mistakes refait les contrôles"),
)
_RESTART_MARKS = ("revalidating from", "restarting validation", "restart validation")


def _restart_reason(lines):
    for line in reversed(lines or []):
        low = line.lower()
        if any(mark in low for mark in _RESTART_MARKS):
            raw = line.strip()[:400]
            label = next((fr for key, fr in _RESTART_REASONS if key in low), None)
            return {"label": label, "raw": raw}
    return None


def _passes(run, log_tail):
    """Passages du pipeline dans le run.

    no-mistakes peut renvoyer un run à une étape antérieure (réparation de CI
    qu'il ne peut pas rattacher au code relu, par exemple) sans créer de
    nouveau run : les étapes suivantes repassent en attente, les appels déjà
    faits restent. Un appel, ou l'étape active, qui recule dans l'ordre du
    pipeline ouvre un nouveau passage.
    """
    calls = run.get("invocations") or []
    call_pass = []
    restarts = []
    current = 1
    prev = None
    for call in calls:
        if prev is not None and _order(call["step_name"]) < _order(prev["step_name"]):
            current += 1
            restarts.append({"pass": current, "step": call["step_name"],
                             "at": call.get("started_at"), "after": prev["step_name"]})
        call_pass.append(current)
        prev = call
    live = next((s for s in run.get("steps") or []
                 if _STEP_STATES.get(s["status"]) in _LIVE), None)
    if (live and prev and live.get("started_at")
            and _order(live["step_name"]) < _order(prev["step_name"])
            and live["started_at"] >= (prev.get("started_at") or 0)):
        current += 1
        restarts.append({"pass": current, "step": live["step_name"],
                         "at": live["started_at"], "after": prev["step_name"]})
    for restart in restarts:
        tail = log_tail(run["id"], restart["after"]) or {}
        restart["reason"] = _restart_reason(tail.get("lines"))
    return {"current": current, "call_pass": call_pass, "restarts": restarts}


def _used_agents(invocations, step_name):
    seen = {}
    for call in invocations:
        if call["step_name"] != step_name:
            continue
        key = (call["agent"], (call.get("model") or "").split("/")[-1])
        seen[key] = seen.get(key, 0) + 1
    return [{"tool": k[0], "model": k[1] or None, "calls": n} for k, n in seen.items()]


_STATUS_ACTIVITY = {
    "awaiting_approval": "en attente d'approbation",
    "fix_review": "correctif à valider",
    "completed": "terminé",
    "running": "en cours",
    "fixing": "correctif en cours",
}


def _clean_activity(text):
    if not text:
        return None
    if text.startswith("status: "):
        status = text[len("status: "):]
        return _STATUS_ACTIVITY.get(status, status)
    for prefix in ("log: ", "status: "):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _stuck(step, state, now, pid_alive):
    """Signes d'une étape active bloquée : agent mort ou silence trop long.
    Une étape qui attend une décision a le droit au silence : elle est signalée
    ailleurs (badge d'attente)."""
    reasons = []
    pid = step.get("agent_pid")
    if pid and pid_alive and pid_alive(pid) is False:
        reasons.append(f"l'agent pid {pid} n'existe plus")
    if state != "decision" and now and step.get("last_activity_at"):
        age = now - step["last_activity_at"]
        if age > STALE_S:
            reasons.append(f"aucune activité depuis {_human_s(age)}")
    return reasons


def _round_list(rounds):
    """Un passage fini : durée, findings vus, sélection et son origine."""
    out = []
    for r in rounds:
        findings = parse_findings(r.get("findings_json"))
        by_id = {f["id"]: f for f in findings if f["id"]}
        selected = [by_id.get(i) or {"id": i} for i in _selected_ids(r.get("selected_finding_ids"))]
        out.append(
            {
                "round": r.get("round"),
                "trigger": r.get("trigger_type"),
                "trigger_label": _TRIGGER_LABELS.get(r.get("trigger_type"), r.get("trigger_type")),
                "duration_ms": r.get("duration_ms"),
                "selection_source": _text(r.get("selection_source")),
                "fix_summary": _text(r.get("fix_summary")),
                "findings_count": len(findings),
                "selected": selected,
            }
        )
    return out


_TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation", "reasoning")
_TOKEN_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_creation": "cache_creation_tokens",
    "reasoning": "reasoning_tokens",
}
_TOOL_KEYS = ("edit", "read", "git", "wait", "test_lint", "other")
_TOOL_COLUMNS = {
    "edit": "tool_edit_calls",
    "read": "tool_read_calls",
    "git": "tool_git_calls",
    "wait": "tool_wait_calls",
    "test_lint": "tool_test_lint_calls",
    "other": "tool_other_calls",
}


def _call_effort(config, call, model):
    """Effort d'un appel fini. La base ne l'enregistre pas : il vient de la
    config actuelle, et seulement si elle donne encore le modèle de l'appel."""
    role = _role(config, call["step_name"], call.get("purpose") == "review-fix")
    configured, effort = _configured(config, role, call["agent"])
    if configured and model and configured.split("/")[-1] != model:
        return None
    return str(effort) if effort else None


def _sessions(invocations, config, call_pass):
    """Appels d'agent finis de ce run, avec leurs coûts, et les totaux du run."""
    rows = []
    totals = {"count": 0, "duration_ms": 0, "tokens": {}, "roundtrips": 0, "tools": {}}
    for call, number in zip(invocations, call_pass):
        tokens = {k: call.get(column) for k, column in _TOKEN_COLUMNS.items()}
        tools = {k: call.get(column) for k, column in _TOOL_COLUMNS.items()}
        model = (call.get("model") or "").split("/")[-1] or None
        row = {
            "pass": number,
            "step": call["step_name"],
            "round": call.get("round"),
            "purpose": call.get("purpose"),
            "agent": call["agent"],
            "model": model,
            "effort": _call_effort(config, call, model),
            "duration_ms": call.get("duration_ms"),
            "tokens": {k: v for k, v in tokens.items() if v is not None},
            "roundtrips": call.get("model_roundtrips"),
            "tools_total": call.get("tool_calls"),
            "tools": {k: v for k, v in tools.items() if v},
            "exit": _text(call.get("exit_status")) or None,
            "failure": _text(call.get("failure_category")) or None,
            "fallback": _text(call.get("fallback_reason")) or None,
        }
        rows.append(row)
        totals["count"] += 1
        totals["duration_ms"] += call.get("duration_ms") or 0
        for key in _TOKEN_KEYS:
            if tokens.get(key):
                totals["tokens"][key] = totals["tokens"].get(key, 0) + tokens[key]
        if call.get("model_roundtrips"):
            totals["roundtrips"] += call["model_roundtrips"]
        for key in _TOOL_KEYS:
            if tools.get(key):
                totals["tools"][key] = totals["tools"].get(key, 0) + tools[key]
    return {"rows": rows, "totals": totals}


def _node(step, run, config, known_models, log_tail, now=None, pid_alive=None, passes=None):
    state = _STEP_STATES.get(step["status"], "attente")
    if run.get("awaiting_agent_since") and state in ("en_cours", "correctif"):
        state = "decision"
    calls = run.get("invocations") or []
    if passes:
        # Seuls les appels du passage en cours décrivent l'étape telle qu'elle est.
        now_calls = [c for c, n in zip(calls, passes["call_pass"]) if n == passes["current"]]
        earlier = [c for c, n in zip(calls, passes["call_pass"]) if n < passes["current"]]
    else:
        now_calls, earlier = calls, []
    # Étape faite au passage précédent, remise en attente par la reprise.
    redo = state == "attente" and bool(
        step.get("rounds") or any(c["step_name"] == step["step_name"] for c in earlier))
    node = {
        "name": step["step_name"],
        "state": state,
        "state_label": "à refaire" if redo else STATE_LABELS[state],
        "redo": redo,
        "findings": count_findings(step.get("findings_json")),
        "finding_list": parse_findings(step.get("findings_json")),
        "duration_ms": step.get("duration_ms"),
        "started_at": step.get("started_at"),
        "completed_at": step.get("completed_at"),
        "rounds": len(step.get("rounds") or []),
        "round_list": _round_list(step.get("rounds") or []),
        "agents": _used_agents(now_calls, step["step_name"]),
        "earlier_agents": _used_agents(earlier, step["step_name"]),
        "exit_code": step.get("exit_code"),
        "override_reason": _text(step.get("override_reason")),
        "note": step.get("skip_reason") or None,
        "live": None,
    }
    if state == "echoue" and step.get("error"):
        node["note"] = step["error"][:240]
    if state not in _LIVE:
        return node
    tail = log_tail(run["id"], step["step_name"]) or {}
    agent = None
    if state != "decision" and (step.get("agent_pid") or tail.get("tool")):
        agent = resolve_agent(
            config, step["step_name"], state == "correctif", tail.get("tool"), known_models
        )
    node["live"] = {
        "agent": agent,
        "round_label": _round_label(step["step_name"], state, step.get("rounds") or []),
        "round_started_at": step.get("round_started_at") or step.get("started_at"),
        "last_activity": _clean_activity(step.get("last_activity")),
        "last_activity_at": step.get("last_activity_at"),
        "last_log_line": (tail.get("last_line") or "")[:200] or None,
        "waiting_since": run.get("awaiting_agent_since") if state == "decision" else None,
        "pid": step.get("agent_pid"),
        "stuck": _stuck(step, state, now, pid_alive) or None,
    }
    return node


def _focus(nodes):
    for wanted in (_LIVE, ("echoue",)):
        for node in nodes:
            if node["state"] in wanted:
                return node["name"]
    done = [n["name"] for n in nodes if n["state"] in ("fini", "saute")]
    return done[-1] if done else (nodes[0]["name"] if nodes else None)


def _times(run, status, now):
    """Temps d'attente et temps actif du run (attente = garé + décision courante)."""
    started = run["created_at"]
    ended = run["updated_at"] if status in ("fini", "echoue", "annule") else now
    total = max(0, (ended or now) - started)
    waiting_since = run.get("awaiting_agent_since") if status == "decision" else None
    wait = (run.get("parked_ms") or 0) / 1000.0
    if waiting_since:
        wait += max(0, now - waiting_since)
    return {
        "total_s": total,
        "wait_s": wait,
        "active_s": max(0, total - wait),
        "waiting_since": waiting_since,
    }


def build_tabs(snapshot, config, log_tail, now, linger_s, pid_alive=None):
    tabs = []
    known_models = snapshot.get("known_models") or {}
    for run in snapshot.get("runs") or []:
        status = _RUN_STATES.get(run["status"], "en_cours")
        if status == "en_cours" and run.get("awaiting_agent_since"):
            status = "decision"
        finished = status in ("fini", "echoue", "annule")
        closes_at = run["updated_at"] + linger_s if finished else None
        if finished and closes_at <= now:
            continue
        passes = _passes(run, log_tail)
        nodes = [_node(step, run, config, known_models, log_tail, now, pid_alive, passes)
                 for step in run["steps"]]
        nodes.sort(key=lambda n: STEPS.index(n["name"]) if n["name"] in STEPS else len(STEPS))
        project = os.path.basename(run["working_path"].rstrip("/")) or run["working_path"]
        times = _times(run, status, now)
        focus = _focus(nodes)
        focused = next((n for n in nodes if n["name"] == focus), None)
        tail = log_tail(run["id"], focused["name"]) if focused and focused["live"] else None
        pr_state = run.get("pr_state") or None
        if pr_state == "none":
            pr_state = None
        intent = run.get("intent")
        tabs.append(
            {
                "id": run["id"],
                "project": project,
                "branch": run["branch"],
                "status": status,
                "status_label": RUN_LABELS[status],
                "started_at": run["created_at"],
                "finished_at": run["updated_at"] if finished else None,
                "closes_at": closes_at,
                "error": (run.get("error") or "")[:240] or None,
                "focus": focus,
                "nodes": nodes,
                "stuck": [n["name"] for n in nodes if n["live"] and n["live"]["stuck"]],
                "waiting_since": times["waiting_since"],
                "parked_ms": run.get("parked_ms"),
                "wait_s": times["wait_s"] if times["wait_s"] else None,
                "active_s": times["active_s"] if times["active_s"] else None,
                "total_s": times["total_s"] if times["total_s"] else None,
                "intent": (intent or "")[:INTENT_MAX] or None,
                "intent_source": _text(run.get("intent_source")),
                "base_sha": _text(run.get("base_sha")),
                "head_sha": _text(run.get("head_sha")),
                "worktree_dir": _text(run.get("worktree_dir")),
                "no_mistakes_version": _text(run.get("no_mistakes_version")),
                "pr_url": _text(run.get("pr_url")),
                "pr_state": pr_state,
                "pr_state_label": _PR_STATES.get(pr_state, pr_state),
                "ci_ready_at": run.get("ci_ready_at"),
                "sessions": _sessions(run.get("invocations") or [], config, passes["call_pass"]),
                "pass": passes["current"],
                "restarts": passes["restarts"],
                "log_lines": (tail or {}).get("lines") or None,
            }
        )
    return {"now": now, "linger_s": linger_s, "tabs": tabs}
