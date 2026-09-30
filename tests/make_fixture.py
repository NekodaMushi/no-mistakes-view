"""Exporte quelques runs réels en fixture assainie (lecture seule).

Usage : python3 tests/make_fixture.py RUN_ID [RUN_ID...] > tests/fixtures/runs.json

Assainissement : pas de contenu de findings (id, sévérité et fichier seulement),
pas d'intention, pas de fournisseur de modèle, pas d'URL hors boucle locale
(bannière de la PR réécrite en 127.0.0.1), chemins réduits au nom du projet,
textes d'erreur et d'activité tronqués.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nmn import state  # noqa: E402

TABLES = {
    "runs": "id, repo_id, branch, status, error, awaiting_agent_since, created_at, updated_at,"
            " head_sha, base_sha, intent, intent_source, worktree_dir, no_mistakes_version,"
            " pr_url, pr_state, ci_ready_at, parked_ms",
    "step_results": "id, run_id, step_name, step_order, status, duration_ms, findings_json,"
                    " error, started_at, completed_at, last_activity_at, last_activity,"
                    " agent_pid, round_started_at, skip_reason, exit_code, override_reason",
    "step_rounds": "id, step_result_id, round, trigger_type, findings_json,"
                   " selected_finding_ids, selection_source, fix_summary, duration_ms",
    "agent_invocations": "id, run_id, step_name, round, purpose, agent, model, started_at,"
                         " duration_ms, exit_status, failure_category, fallback_reason,"
                         " input_tokens, output_tokens, cache_read_tokens,"
                         " cache_creation_tokens, reasoning_tokens, model_roundtrips,"
                         " tool_calls, tool_wait_calls, tool_test_lint_calls, tool_edit_calls,"
                         " tool_read_calls, tool_git_calls, tool_other_calls",
}

_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost)[^\s)\]]+")
_TOKEN_WORDS = re.compile(
    r"(?i)\b(anthropic|openai|mistral|google|deepseek|qwen|mistralai|zhipu)\b"
)


def _short(text, size=60):
    return text[:size] if text else text


def _scrub(text):
    """Pas d'URL hors boucle locale ni de mot de fournisseur dans la fixture."""
    if not text:
        return text
    text = _URL.sub("(url)", text)
    return _TOKEN_WORDS.sub("(fournisseur)", text)


def _stub_findings(raw):
    if not raw:
        return raw
    try:
        found = (json.loads(raw) or {}).get("findings") or []
    except ValueError:
        return None
    stubs = []
    for i, f in enumerate(found):
        if not isinstance(f, dict):
            continue
        stub = {"id": f"f{i + 1}"}
        if f.get("severity"):
            stub["severity"] = f["severity"]
        if f.get("file"):
            stub["file"] = os.path.basename(str(f["file"]))
        if isinstance(f.get("line"), int):
            stub["line"] = f["line"]
        stubs.append(stub)
    return json.dumps({"findings": stubs}) if stubs else None


def _sanitize_pr_url(url):
    if not url:
        return url
    return "http://127.0.0.1:1/pr/42"


def main(run_ids):
    conn = state.connect_readonly(os.path.join(state.home_dir(), "state.sqlite"))
    marks = ",".join("?" for _ in run_ids)
    out = {}
    for table, columns in TABLES.items():
        if table == "step_rounds":
            continue  # rattachée aux étapes, pas aux runs
        key = "id" if table == "runs" else "run_id"
        out[table] = [dict(r) for r in conn.execute(
            f"SELECT {columns} FROM {table} WHERE {key} IN ({marks}) ORDER BY id", run_ids)]
    step_ids = [s["id"] for s in out["step_results"]]
    out["step_rounds"] = [dict(r) for r in conn.execute(
        "SELECT id, step_result_id, round, trigger_type, findings_json, selected_finding_ids,"
        " selection_source, fix_summary, duration_ms FROM step_rounds"
        f" WHERE step_result_id IN ({','.join('?' for _ in step_ids)}) ORDER BY id", step_ids)]
    repo_ids = sorted({r["repo_id"] for r in out["runs"]})
    out["repos"] = [
        {"id": r["id"], "working_path": "/home/user/projects/" + os.path.basename(r["working_path"])}
        for r in conn.execute(
            f"SELECT id, working_path FROM repos WHERE id IN ({','.join('?' for _ in repo_ids)})",
            repo_ids)
    ]
    conn.close()
    for run in out["runs"]:
        run["error"] = _short(_scrub(run["error"]))
        # L'intention réelle contient des URLs et des chemins : stub neutre,
        # la source reste la colonne d'origine.
        run["intent"] = ("faire le chantier décrit dans la revue d'architecture" if run.get("intent") else None)
        run["pr_url"] = _sanitize_pr_url(run["pr_url"])
        run["worktree_dir"] = (
            "/home/user/.no-mistakes/worktrees/wt/" + run["id"][:12]
            if run.get("worktree_dir") else None
        )
        run["head_sha"] = (run.get("head_sha") or "")[:12] or None
        run["base_sha"] = (run.get("base_sha") or "")[:12] or None
    for step in out["step_results"]:
        step["findings_json"] = _stub_findings(step["findings_json"])
        step["error"] = _short(_scrub(step["error"]))
        step["last_activity"] = _short(_scrub(step["last_activity"]))
        step["skip_reason"] = _short(_scrub(step["skip_reason"]))
        step["override_reason"] = _short(_scrub(step["override_reason"]))
    for r in out["step_rounds"]:
        r["findings_json"] = _stub_findings(r["findings_json"])
        r["fix_summary"] = _short(_scrub(r["fix_summary"]))
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1:])
