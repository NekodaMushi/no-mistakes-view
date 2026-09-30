"""Lecture seule de l'état que no-mistakes garde sous ~/.no-mistakes/.

Rien ici n'écrit : la base s'ouvre avec `mode=ro` et `query_only`, les
fichiers de config et de log s'ouvrent en lecture.
"""

import os
import re
import sqlite3

from . import miniyaml

ACTIVE_RUN_STATUSES = ("pending", "running")
LOG_TAIL_BYTES = 4 * 1024 * 1024
LOG_TAIL_LINES = 30

_AGENT_START = re.compile(r"^(\S+) started pid=(\d+)\s*$")
_AGENT_EXIT = re.compile(r"^(\S+) exited pid=(\d+)\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def home_dir():
    return os.environ.get("NO_MISTAKES_HOME") or os.path.expanduser("~/.no-mistakes")


def connect_readonly(db_path):
    """Ouvre la base en lecture seule ; toute écriture lève une erreur SQLite."""
    uri = "file:" + db_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        # Base WAL sans fichier -shm (démon arrêté) : `immutable` lit sans verrou.
        conn.close()
        conn = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=1.0)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params)]


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _select(conn, table, columns):
    """Colonnes demandées qui existent vraiment ; les autres liront None."""
    known = _columns(conn, table)
    return [c for c in columns if c in known], [c for c in columns if c not in known]


_RUN_BASE = ("id", "branch", "status", "error", "awaiting_agent_since", "created_at", "updated_at")
_RUN_MORE = (
    "head_sha", "base_sha", "intent", "intent_source", "worktree_dir",
    "no_mistakes_version", "pr_url", "pr_state", "ci_ready_at", "parked_ms",
)
_STEP_BASE = (
    "id", "step_name", "step_order", "status", "duration_ms", "findings_json",
    "error", "started_at", "completed_at", "last_activity_at", "last_activity",
    "agent_pid", "round_started_at", "skip_reason",
)
_STEP_MORE = ("exit_code", "override_reason")
_ROUND_BASE = ("id", "step_result_id", "round", "trigger_type")
_ROUND_MORE = ("findings_json", "selected_finding_ids", "selection_source", "fix_summary", "duration_ms")
_CALL_BASE = ("id", "step_name", "purpose", "agent", "model", "started_at")
_CALL_MORE = (
    "round", "duration_ms", "exit_status", "failure_category", "fallback_reason",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "reasoning_tokens", "model_roundtrips", "tool_calls", "tool_wait_calls",
    "tool_test_lint_calls", "tool_edit_calls", "tool_read_calls", "tool_git_calls",
    "tool_other_calls",
)


def _rows_partial(conn, table, base, more, where, params, order):
    """Lignes de `table` : colonnes de base + celles de `more` qui existent ;
    une colonne absente du schéma arrive à None."""
    present, missing = _select(conn, table, base + more)
    if not present:
        return []
    rows = _rows(conn, f"SELECT {','.join(present)} FROM {table} WHERE {where} ORDER BY {order}", params)
    for row in rows:
        for name in missing:
            row[name] = None
    return rows


def read_snapshot(db_path, now, linger_s):
    """Runs en cours, plus ceux finis depuis moins de `linger_s` secondes."""
    conn = connect_readonly(db_path)
    try:
        marks = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        runs = _rows(
            conn,
            "SELECT r.id, r.branch, r.status, r.error, r.awaiting_agent_since,"
            " r.created_at, r.updated_at, p.working_path"
            " FROM runs r JOIN repos p ON p.id = r.repo_id"
            f" WHERE r.status IN ({marks}) OR r.updated_at >= ?"
            " ORDER BY r.created_at, r.id",
            (*ACTIVE_RUN_STATUSES, now - linger_s),
        )
        for run in runs:
            extra = _rows_partial(
                conn, "runs", (), _RUN_MORE, "id = ?", (run["id"],), "created_at"
            )
            run.update(extra[0] if extra else {name: None for name in _RUN_MORE})
            run["steps"] = _rows_partial(
                conn, "step_results", _STEP_BASE, _STEP_MORE,
                "run_id = ?", (run["id"],), "step_order",
            )
            for step in run["steps"]:
                step["rounds"] = _rows_partial(
                    conn, "step_rounds", _ROUND_BASE, _ROUND_MORE,
                    "step_result_id = ?", (step.pop("id"),), "round",
                )
            run["invocations"] = _rows_partial(
                conn, "agent_invocations", _CALL_BASE, _CALL_MORE,
                "run_id = ?", (run["id"],), "started_at, id",
            )
        known_models = {
            row["agent"]: row["model"]
            for row in _rows(
                conn,
                "SELECT agent, model FROM agent_invocations"
                " WHERE model IS NOT NULL AND model != '' ORDER BY started_at",
            )
        }
    finally:
        conn.close()
    return {"runs": runs, "known_models": known_models}


def pid_alive(pid):
    """Le pid existe-t-il encore sur cette machine ? (os.kill(pid, 0), sans écriture)"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # verdict inconnu : on ne déclare pas mort


def read_config(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return miniyaml.load(handle.read())
    except OSError:
        return {}


def parse_log_tail(text):
    """Agent encore lancé d'après le log d'étape, dernière ligne utile, et les
    LOG_TAIL_LINES dernières lignes pour le panneau de détails."""
    running = {}
    lines = []
    last_line = None
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        if not line.strip():
            continue
        start = _AGENT_START.match(line.strip())
        if start:
            running[start.group(2)] = start.group(1)
        else:
            stop = _AGENT_EXIT.match(line.strip())
            if stop:
                running.pop(stop.group(2), None)
            else:
                last_line = line.strip()
        lines.append(line)
        if len(lines) > LOG_TAIL_LINES:
            lines.pop(0)
    tool = list(running.values())[-1] if running else None
    return {"tool": tool, "last_line": last_line, "lines": lines}


_log_cache = {}


def read_log_tail(logs_dir, run_id, step_name):
    """Relit le log d'étape seulement quand sa taille ou sa date a changé."""
    if not re.fullmatch(r"[0-9A-Za-z]+", run_id) or not re.fullmatch(r"[a-z-]+", step_name):
        return None
    path = os.path.join(logs_dir, run_id, step_name + ".log")
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
        cached = _log_cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        with open(path, "rb") as handle:
            handle.seek(max(0, stat.st_size - LOG_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return None
    parsed = parse_log_tail(data.decode("utf-8", "replace"))
    if len(_log_cache) > 64:
        _log_cache.clear()
    _log_cache[path] = (stamp, parsed)
    return parsed
