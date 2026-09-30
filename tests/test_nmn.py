import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from nmn import cli, miniyaml, model, server, state

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = 1789997240       # époque des runs finis de la fixture
NOW_ACTIVE = 1790027204  # le run en cours de la fixture, activité fraîche
LINGER = 90

RUN_ACTIVE = "01M32VN3N4QYNAVVSS4KXMH5FX"   # en cours, étape ci en surveillance
RUN_PARKED = "01M3217A3C5RRQRXX5PYJYBXAT"   # fini, garé 3,4 h au total
RUN_DONE = "01M320VN03Y89F7X1X3EJN1277"
RUN_FAILED = "01M320RN8712B2GQZXNRMK9TQT"
RUN_CANCELLED = "01M2X4CYP2BE00ZK2DD9A2EK2R"
RUN_CANCELLED_RECENT = "01M32VDM00QWD6NJFKZT5X4G5J"
RUN_SKIPPED = "01KYRT86HJKT5BB6M0CE8XHR3M"

# Colonnes lues par l'outil, avec les types du schéma réel.
SCHEMA = """
CREATE TABLE repos (id TEXT PRIMARY KEY, working_path TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, branch TEXT NOT NULL,
  status TEXT NOT NULL, error TEXT, awaiting_agent_since INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  head_sha TEXT, base_sha TEXT, intent TEXT, intent_source TEXT, worktree_dir TEXT,
  no_mistakes_version TEXT, pr_url TEXT, pr_state TEXT, ci_ready_at INTEGER, parked_ms INTEGER);
CREATE TABLE step_results (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_name TEXT NOT NULL,
  step_order INTEGER NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, findings_json TEXT,
  error TEXT, started_at INTEGER, completed_at INTEGER, last_activity_at INTEGER,
  last_activity TEXT, agent_pid INTEGER, round_started_at INTEGER, skip_reason TEXT,
  exit_code INTEGER, override_reason TEXT);
CREATE TABLE step_rounds (id TEXT PRIMARY KEY, step_result_id TEXT NOT NULL,
  round INTEGER NOT NULL, trigger_type TEXT NOT NULL, findings_json TEXT,
  selected_finding_ids TEXT, selection_source TEXT, fix_summary TEXT, duration_ms INTEGER);
CREATE TABLE agent_invocations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
  step_name TEXT NOT NULL, round INTEGER, purpose TEXT NOT NULL,
  agent TEXT NOT NULL, model TEXT, started_at INTEGER NOT NULL, duration_ms INTEGER,
  exit_status TEXT, failure_category TEXT, fallback_reason TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_creation_tokens INTEGER, reasoning_tokens INTEGER, model_roundtrips INTEGER,
  tool_calls INTEGER, tool_wait_calls INTEGER, tool_test_lint_calls INTEGER,
  tool_edit_calls INTEGER, tool_read_calls INTEGER, tool_git_calls INTEGER,
  tool_other_calls INTEGER);
"""

# Schéma d'une base plus ancienne : les colonnes récentes n'existent pas.
OLD_SCHEMA = """
CREATE TABLE repos (id TEXT PRIMARY KEY, working_path TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, branch TEXT NOT NULL,
  status TEXT NOT NULL, error TEXT, awaiting_agent_since INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE step_results (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_name TEXT NOT NULL,
  step_order INTEGER NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, findings_json TEXT,
  error TEXT, started_at INTEGER, completed_at INTEGER, last_activity_at INTEGER,
  last_activity TEXT, agent_pid INTEGER, round_started_at INTEGER, skip_reason TEXT);
CREATE TABLE step_rounds (id TEXT PRIMARY KEY, step_result_id TEXT NOT NULL,
  round INTEGER NOT NULL, trigger_type TEXT NOT NULL);
CREATE TABLE agent_invocations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
  step_name TEXT NOT NULL, round INTEGER NOT NULL, purpose TEXT NOT NULL,
  agent TEXT NOT NULL, model TEXT, started_at INTEGER NOT NULL);
"""


def build_db(path, fixture="runs.json"):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    if fixture:
        with open(os.path.join(FIXTURES, fixture), encoding="utf-8") as handle:
            tables = json.load(handle)
        for table, rows in tables.items():
            for row in rows:
                marks = ",".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO {table} ({','.join(row)}) VALUES ({marks})", list(row.values())
                )
    conn.commit()
    return conn


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def no_log(run_id, step_name):
    return None


class FromRealState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "state.sqlite")
        self.conn = build_db(self.db)
        self.addCleanup(self.conn.close)
        self.config = state.read_config(os.path.join(FIXTURES, "config.yaml"))

    def tabs(self, now=NOW, log_tail=no_log, pid_alive=None):
        snapshot = state.read_snapshot(self.db, now, LINGER)
        return model.build_tabs(snapshot, self.config, log_tail, now, LINGER, pid_alive)["tabs"]

    def tab(self, run_id, **kwargs):
        return next(t for t in self.tabs(**kwargs) if t["id"] == run_id)

    def test_one_tab_per_run_oldest_first(self):
        tabs = self.tabs()
        self.assertEqual([t["id"] for t in tabs],
                         [RUN_PARKED, RUN_CANCELLED_RECENT, RUN_ACTIVE])
        self.assertEqual([t["status"] for t in tabs], ["echoue", "annule", "en_cours"])
        self.assertEqual(tabs[-1]["project"], "language-islands-app")
        self.assertEqual(tabs[-1]["branch"], "fm/li-arch-draft-audio-wait")
        self.assertIsNone(tabs[-1]["closes_at"])

    def test_nine_nodes_in_pipeline_order(self):
        for tab in self.tabs():
            self.assertEqual(tuple(n["name"] for n in tab["nodes"]), model.STEPS)

    def test_run_carries_branch_intent_shas_pr_and_version(self):
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        self.assertEqual(tab["intent"], "faire le chantier décrit dans la revue d'architecture")
        self.assertEqual(tab["intent_source"], "agent")
        self.assertEqual(tab["base_sha"], "7c736bf3371e")
        self.assertEqual(tab["head_sha"], "3ba59f484307")
        self.assertEqual(tab["worktree_dir"], "/home/user/.no-mistakes/worktrees/wt/01M32VN3N4QY")
        self.assertEqual(tab["no_mistakes_version"], "v1.75.2")
        self.assertEqual(tab["pr_url"], "http://127.0.0.1:1/pr/42")
        self.assertEqual((tab["pr_state"], tab["pr_state_label"]), ("open", "ouverte"))
        self.assertEqual(tab["ci_ready_at"], 1790026628)
        # Run sans PR et sans intention (base plus vieille) : rien à afficher.
        none_tab = self.tab(RUN_SKIPPED, now=1785437659 + 5)
        self.assertIsNone(none_tab["pr_url"])
        self.assertIsNone(none_tab["pr_state"])
        self.assertIsNone(none_tab["intent"])

    def test_running_run_reports_active_and_total_time(self):
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        self.assertEqual(tab["total_s"], NOW_ACTIVE - 1790023732)
        self.assertEqual(tab["active_s"], NOW_ACTIVE - 1790023732)
        self.assertIsNone(tab["wait_s"])
        self.assertIsNone(tab["waiting_since"])

    def test_run_parked_at_a_gate_waits_for_a_decision(self):
        self.conn.execute(
            "UPDATE runs SET awaiting_agent_since=? WHERE id=?", (NOW_ACTIVE - 40, RUN_ACTIVE))
        self.conn.execute(
            "UPDATE step_results SET status='awaiting_approval', agent_pid=NULL,"
            " last_activity='status: awaiting_approval' WHERE run_id=? AND step_name='ci'",
            (RUN_ACTIVE,))
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        self.assertEqual((tab["status"], tab["status_label"]), ("decision", "attend une décision"))
        self.assertEqual(tab["waiting_since"], NOW_ACTIVE - 40)
        self.assertAlmostEqual(tab["wait_s"], 40.0)
        node = tab["nodes"][8]
        self.assertEqual(node["state"], "decision")
        self.assertIsNone(node["live"]["agent"])
        self.assertEqual(node["live"]["waiting_since"], NOW_ACTIVE - 40)
        self.assertEqual(node["live"]["last_activity"], "en attente d'approbation")

    def test_finished_run_splits_wait_from_active_time(self):
        tab = self.tab(RUN_PARKED, now=NOW)
        # 12 168 715 ms garés sur un run de 16 421 s.
        self.assertAlmostEqual(tab["wait_s"], 12168.715)
        self.assertAlmostEqual(tab["total_s"], 16421)
        self.assertAlmostEqual(tab["active_s"], 16421 - 12168.715)
        self.assertIsNone(tab["waiting_since"])  # fini : plus d'attente courante

    def test_stuck_step_flagged_by_dead_pid_and_long_silence(self):
        self.conn.execute(
            "UPDATE step_results SET agent_pid=999999999 WHERE run_id=? AND step_name='ci'",
            (RUN_ACTIVE,))
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE, pid_alive=lambda pid: False)
        node = tab["nodes"][8]
        self.assertEqual(
            node["live"]["stuck"],
            ["l'agent pid 999999999 n'existe plus"],
        )
        self.assertEqual(tab["stuck"], ["ci"])
        # Un pid vivant et une activité fraîche : rien à signaler.
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE, pid_alive=lambda pid: True)
        self.assertIsNone(tab["nodes"][8]["live"]["stuck"])
        self.assertEqual(tab["stuck"], [])

    def test_stuck_step_flagged_by_silence_only(self):
        self.conn.execute(
            "UPDATE step_results SET last_activity_at=? WHERE run_id=? AND step_name='ci'",
            (NOW_ACTIVE - 1200, RUN_ACTIVE))
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        node = tab["nodes"][8]
        self.assertEqual(node["live"]["stuck"], ["aucune activité depuis 20 min"])
        self.assertEqual(node["live"]["pid"], None)

    def test_decision_silence_is_not_stuck(self):
        # Une étape qui attend une décision a le droit au silence.
        self.conn.execute(
            "UPDATE runs SET awaiting_agent_since=? WHERE id=?", (NOW_ACTIVE - 3600, RUN_ACTIVE))
        self.conn.execute(
            "UPDATE step_results SET status='awaiting_approval', agent_pid=NULL"
            " WHERE run_id=? AND step_name='ci'", (RUN_ACTIVE,))
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        node = tab["nodes"][8]
        self.assertEqual(node["state"], "decision")
        self.assertIsNone(node["live"]["stuck"])
        self.assertEqual(tab["stuck"], [])

    def test_pid_alive_checks_locally(self):
        self.assertTrue(state.pid_alive(os.getpid()))
        self.assertFalse(state.pid_alive(0))
        self.assertFalse(state.pid_alive(None))
        self.assertFalse(state.pid_alive(999999999))

    def test_fixing_step_shows_agent_round_and_activity(self):
        # État observé pendant un correctif : même base, étape mise en correctif.
        self.conn.execute(
            "UPDATE step_results SET status='fixing', completed_at=NULL, agent_pid=1566632,"
            " last_activity='log: Both' WHERE run_id=? AND step_name='ci'", (RUN_ACTIVE,))
        self.conn.execute(
            "INSERT INTO step_rounds (id, step_result_id, round, trigger_type, duration_ms)"
            " VALUES ('01FIXROUNDA', '01M32VN560HMNC4NEM0V5WSFGM', 2, 'auto_fix', 5000)")
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, log_tail=lambda r, s: {"tool": "claude", "last_line": "x"})
        self.assertEqual(tab["focus"], "ci")
        node = tab["nodes"][8]
        self.assertEqual((node["state"], node["state_label"]), ("correctif", "en correctif"))
        live = node["live"]
        self.assertEqual(live["agent"]["tool"], "claude")
        self.assertEqual(live["agent"]["model"], "claude-opus-5")
        self.assertEqual(live["agent"]["tool_source"], "log")
        self.assertEqual(live["round_label"], "correctif 2")
        self.assertEqual(live["pid"], 1566632)

    def test_running_step_falls_back_to_configured_agent(self):
        # L'agent enregistré suffit à déclencher la résolution depuis la config.
        self.conn.execute(
            "UPDATE step_results SET agent_pid=1234 WHERE run_id=? AND step_name='ci'",
            (RUN_ACTIVE,))
        self.conn.commit()
        node = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)["nodes"][8]
        self.assertEqual(node["state"], "en_cours")
        self.assertEqual(node["live"]["agent"]["tool"], "claude")
        self.assertEqual(node["live"]["agent"]["tool_source"], "config")
        self.assertEqual(node["live"]["pid"], 1234)
        self.assertEqual(node["live"]["last_activity"],
                         "all CI checks passed - still monitoring until merged or")

    def test_finished_nodes_carry_duration_findings_and_used_agents(self):
        nodes = {n["name"]: n for n in self.tab(RUN_ACTIVE, now=NOW_ACTIVE)["nodes"]}
        review = nodes["review"]
        self.assertEqual(review["state"], "fini")
        self.assertEqual(review["duration_ms"], 598744)
        self.assertEqual(review["rounds"], 1)
        self.assertEqual(
            [(a["tool"], a["model"], a["calls"]) for a in review["agents"]],
            [("claude", "claude-opus-5", 1)],
        )
        self.assertEqual(review["findings"], 2)
        self.assertEqual(
            review["finding_list"],
            [
                {"id": "f1", "title": None, "severity": "info",
                 "file": "architecture-improvement-historic.md", "line": 834,
                 "description": None},
                {"id": "f2", "title": None, "severity": "info",
                 "file": "draft-audio-wait.ts", "line": 33, "description": None},
            ],
        )
        self.assertEqual(nodes["intent"]["exit_code"], 0)
        self.assertIsNone(nodes["intent"]["override_reason"])

    def test_rounds_carry_duration_selection_and_summary(self):
        nodes = {n["name"]: n for n in self.tab(RUN_ACTIVE, now=NOW_ACTIVE)["nodes"]}
        self.assertEqual(
            nodes["document"]["round_list"],
            [{
                "round": 1, "trigger": "initial", "trigger_label": "initial",
                "duration_ms": 247474, "selection_source": None,
                "fix_summary": "changes applied", "findings_count": 0, "selected": [],
            }],
        )
        self.assertEqual(
            nodes["lint"]["round_list"][0]["fix_summary"],
            "fix stale test count and Draft audio wait pointers in ADRs",
        )

    def test_round_selection_names_the_findings_or_their_ids(self):
        nodes = {n["name"]: n for n in self.tab(RUN_CANCELLED, now=1789834892 + 5)["nodes"]}
        rebase = nodes["rebase"]["round_list"][0]
        self.assertEqual(rebase["selection_source"], "user")
        # Les ids sélectionnés ne figurent pas dans les findings de la fixture :
        # ils restent affichés par leur id seul.
        self.assertEqual(rebase["selected"], [{"id": "rebase-1"}])
        self.assertEqual(rebase["findings_count"], 1)
        self.assertEqual(rebase["duration_ms"], 3250)
        # Sélection refusée : liste vide.
        failed = {n["name"]: n for n in self.tab(RUN_FAILED, now=1789995612 + 5)["nodes"]}
        self.assertEqual(failed["review"]["round_list"][0]["selection_source"], "user")
        self.assertEqual(failed["review"]["round_list"][0]["selected"],
                         [{"id": "route-suite-stale-entitlement"}])

    def test_step_error_exit_code_and_override_are_kept(self):
        nodes = {n["name"]: n for n in self.tab(RUN_FAILED, now=1789995612 + 5)["nodes"]}
        review = nodes["review"]
        self.assertEqual(review["state"], "echoue")
        self.assertEqual(review["exit_code"], 0)
        self.assertIsNone(review["override_reason"])
        self.assertTrue(review["note"].startswith("agent fix: pi reported error"))
        done_at = 1789996662
        done = {n["name"]: n for n in self.tab(RUN_DONE, now=done_at + 30)["nodes"]}
        self.assertEqual(done["document"]["exit_code"], 0)
        self.assertIsNone(done["document"]["override_reason"])

    def test_sessions_table_and_run_totals(self):
        sessions = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)["sessions"]
        rows = sessions["rows"]
        self.assertEqual(len(rows), 5)
        review = rows[0]
        self.assertEqual(
            (review["step"], review["round"], review["purpose"], review["agent"], review["model"]),
            ("review", 1, "review", "claude", "claude-opus-5"),
        )
        self.assertEqual(review["effort"], "high")
        self.assertEqual(review["duration_ms"], 598659)
        self.assertEqual(review["exit"], "ok")
        self.assertIsNone(review["failure"])
        self.assertEqual(
            review["tokens"],
            {"input": 150, "output": 348, "cache_read": 6083921, "cache_creation": 234307},
        )
        self.assertIsNone(review["roundtrips"])
        totals = sessions["totals"]
        self.assertEqual(totals["count"], 5)
        self.assertEqual(totals["duration_ms"], 2299165)
        self.assertEqual(
            totals["tokens"],
            {"input": 716, "output": 2343, "cache_read": 29815913, "cache_creation": 779797},
        )
        self.assertEqual(totals["roundtrips"], 0)
        self.assertEqual(totals["tools"], {})

    def test_session_effort_comes_from_config_while_the_model_matches(self):
        # Pi en correcteur de revue : effort lu dans `--thinking` ; un appel sur
        # un autre modèle que celui de la config n'a pas d'effort connu.
        self.conn.executemany(
            "INSERT INTO agent_invocations (id, run_id, step_name, round, purpose, agent, model,"
            " started_at) VALUES (?, '01M320VN03Y89F7X1X3EJN1277', 'review', 2, 'review-fix',"
            " 'pi', ?, ?)",
            [
                ("01EF000000000000000000000A", "glm-5.3-flash", 1789995640),
                ("01EF000000000000000000000B", "deepseek-v4.1-flash", 1789995900),
            ],
        )
        self.conn.commit()
        rows = self.tab(RUN_DONE, now=1789996662 + 30)["sessions"]["rows"]
        effort = {r["model"]: r["effort"] for r in rows if r["agent"] == "pi"}
        self.assertEqual(effort["glm-5.3-flash"], "max")
        self.assertIsNone(effort["deepseek-v4.1-flash"])

    def rewind_to_review(self):
        """État observé quand la CI renvoie le run en revue : un appel ci, puis la
        revue relancée et les étapes suivantes remises en attente."""
        self.conn.execute(
            "INSERT INTO agent_invocations (id, run_id, step_name, round, purpose, agent, model,"
            " started_at) VALUES ('01CI000000000000000000000A', ?, 'ci', 2, 'ci', 'claude',"
            " 'claude-opus-5', 1790026200)", (RUN_ACTIVE,))
        self.conn.execute(
            "UPDATE step_results SET status='running', started_at=1790026600, completed_at=NULL"
            " WHERE run_id=? AND step_name='review'", (RUN_ACTIVE,))
        self.conn.execute(
            "UPDATE step_results SET status='pending', started_at=NULL, completed_at=NULL"
            " WHERE run_id=? AND step_name IN ('test','document','lint','push','pr','ci')",
            (RUN_ACTIVE,))
        self.conn.commit()

    def test_rewound_run_opens_a_new_pass(self):
        self.rewind_to_review()
        ci_log = {"lines": [
            "committed CI repair for revalidation",
            "cannot prove the repaired head continues the reviewed head: repaired head 8031"
            " does not descend from reviewed head 57dd; revalidating from Review instead of publishing",
        ]}
        tab = self.tab(RUN_ACTIVE, now=1790026700,
                       log_tail=lambda r, s: ci_log if s == "ci" else None)
        self.assertEqual(tab["pass"], 2)
        [restart] = tab["restarts"]
        self.assertEqual((restart["step"], restart["at"], restart["after"]),
                         ("review", 1790026600, "ci"))
        self.assertIn("réécrit l'historique", restart["reason"]["label"])
        self.assertTrue(restart["reason"]["raw"].startswith("cannot prove"))
        # Tous les appels finis datent du premier passage.
        self.assertEqual([r["pass"] for r in tab["sessions"]["rows"]], [1] * 6)
        nodes = {n["name"]: n for n in tab["nodes"]}
        self.assertEqual(nodes["review"]["state"], "en_cours")
        self.assertEqual(nodes["review"]["agents"], [])
        self.assertEqual(nodes["review"]["earlier_agents"][0]["tool"], "claude")
        self.assertEqual((nodes["test"]["state_label"], nodes["test"]["redo"]), ("à refaire", True))
        self.assertEqual(nodes["test"]["agents"], [])
        self.assertEqual(nodes["test"]["earlier_agents"][0]["calls"], 2)

    def test_calls_after_the_rewind_belong_to_the_new_pass(self):
        self.rewind_to_review()
        self.conn.execute(
            "INSERT INTO agent_invocations (id, run_id, step_name, round, purpose, agent, model,"
            " started_at) VALUES ('01RV000000000000000000000A', ?, 'review', 1, 'review', 'pi',"
            " 'glm-5.3-flash', 1790026610)", (RUN_ACTIVE,))
        self.conn.commit()
        tab = self.tab(RUN_ACTIVE, now=1790026700)
        self.assertEqual(tab["pass"], 2)
        self.assertEqual(len(tab["restarts"]), 1)  # l'appel et l'étape active disent la même reprise
        self.assertIsNone(tab["restarts"][0]["reason"])  # pas de log : raison inconnue
        self.assertEqual([r["pass"] for r in tab["sessions"]["rows"]], [1] * 6 + [2])
        review = next(n for n in tab["nodes"] if n["name"] == "review")
        self.assertEqual([a["tool"] for a in review["agents"]], ["pi"])

    def test_straight_run_stays_a_single_pass(self):
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        self.assertEqual((tab["pass"], tab["restarts"]), (1, []))
        self.assertFalse(any(n["redo"] for n in tab["nodes"]))

    def test_session_row_keeps_failure_and_fallback(self):
        sessions = self.tab(RUN_FAILED, now=1789995612 + 5)["sessions"]
        fixer = next(r for r in sessions["rows"] if r["purpose"] == "review-fix")
        self.assertEqual((fixer["agent"], fixer["exit"], fixer["failure"]),
                         ("pi", "error", "other"))
        cancelled = self.tab(RUN_CANCELLED, now=1789834892 + 5)["sessions"]["rows"][0]
        self.assertEqual((cancelled["exit"], cancelled["failure"]), ("cancelled", "cancelled"))
        self.assertTrue(all(r["fallback"] in (None, "exit")
                            for r in sessions["rows"] + [cancelled]))

    def test_session_totals_sum_tokens_roundtrips_and_tools(self):
        # Appels avec allers-retours et appels d'outils (valeurs observées chez codex).
        self.conn.executemany(
            "INSERT INTO agent_invocations (id, run_id, step_name, round, purpose, agent, model,"
            " started_at, duration_ms, exit_status, model_roundtrips, tool_calls,"
            " tool_edit_calls, tool_read_calls, tool_other_calls)"
            " VALUES (?, '01M320VN03Y89F7X1X3EJN1277', 'review', 1, 'review', 'codex',"
            " 'gpt-5.6-sol', ?, ?, 'ok', ?, ?, ?, ?, ?)",
            [
                ("01RT000000000000000000000A", 1789995640, 30000, 13, 9, 3, 5, 1),
                ("01RT000000000000000000000B", 1789995900, 20000, 7, 4, 1, 2, 1),
            ],
        )
        self.conn.commit()
        done_at = 1789996662
        sessions = self.tab(RUN_DONE, now=done_at + 30)["sessions"]
        codex = [r for r in sessions["rows"] if r["agent"] == "codex"]
        self.assertEqual(len(codex), 2)
        self.assertEqual(codex[0]["roundtrips"], 13)
        self.assertEqual(codex[0]["tools_total"], 9)
        self.assertEqual(codex[0]["tools"], {"edit": 3, "read": 5, "other": 1})
        self.assertEqual(sessions["totals"]["roundtrips"], 20)
        self.assertEqual(sessions["totals"]["tools"], {"edit": 4, "read": 7, "other": 2})

    def test_tab_carries_the_last_log_lines_of_the_focused_step(self):
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE)
        self.assertIsNone(tab["log_lines"])
        tail = {"tool": None, "last_line": None, "lines": ["démarrage de la CI", "check 1 : ok"]}
        tab = self.tab(RUN_ACTIVE, now=NOW_ACTIVE, log_tail=lambda r, s: tail)
        self.assertEqual(tab["log_lines"], ["démarrage de la CI", "check 1 : ok"])

    def test_finished_run_keeps_its_tab_for_a_moment_then_closes(self):
        done_at = 1789996662
        tab = self.tab(RUN_DONE, now=done_at + 30)
        self.assertEqual(tab["status"], "fini")
        self.assertEqual(tab["closes_at"], done_at + LINGER)
        self.assertTrue(all(n["state"] in ("fini", "saute") for n in tab["nodes"]))
        later = [t["id"] for t in self.tabs(now=done_at + LINGER + 1)]
        self.assertNotIn(RUN_DONE, later)

    def test_failed_run_points_at_the_failed_node(self):
        tab = self.tab(RUN_FAILED, now=1789995612 + 5)
        self.assertEqual(tab["status"], "echoue")
        self.assertEqual(tab["error"], "step review failed: agent fix: pi reported error: 403: {\"typ")
        self.assertEqual(tab["focus"], "review")
        node = tab["nodes"][2]
        self.assertEqual(node["state"], "echoue")
        self.assertEqual(tab["nodes"][3]["state"], "attente")

    def test_cancelled_run_and_skipped_node(self):
        tab = self.tab(RUN_CANCELLED, now=1789834892 + 5)
        self.assertEqual(tab["status_label"], "annulé")
        skipped = self.tab(RUN_SKIPPED, now=1785437659 + 5)
        self.assertIn("saute", [n["state"] for n in skipped["nodes"]])

    def test_database_is_opened_read_only(self):
        self.conn.close()
        before = read_bytes(self.db)
        conn = state.connect_readonly(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("UPDATE runs SET status='failed'")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")
        conn.close()
        state.read_snapshot(self.db, NOW, LINGER)
        self.assertEqual(read_bytes(self.db), before)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["state.sqlite"])



class OlderSchema(unittest.TestCase):
    """Une base sans les colonnes récentes : tout ce qui manque reste absent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "state.sqlite")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)  # pose toutes les colonnes, puis on les retire
        conn.executescript(
            "DROP TABLE agent_invocations; DROP TABLE step_rounds; DROP TABLE step_results;"
            " DROP TABLE runs;"
        )
        conn.executescript(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, branch TEXT NOT NULL,"
            " status TEXT NOT NULL, error TEXT, awaiting_agent_since INTEGER,"
            " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
            "CREATE TABLE step_results (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,"
            " step_name TEXT NOT NULL, step_order INTEGER NOT NULL, status TEXT NOT NULL,"
            " duration_ms INTEGER, findings_json TEXT, error TEXT, started_at INTEGER,"
            " completed_at INTEGER, last_activity_at INTEGER, last_activity TEXT,"
            " agent_pid INTEGER, round_started_at INTEGER, skip_reason TEXT);"
            "CREATE TABLE step_rounds (id TEXT PRIMARY KEY, step_result_id TEXT NOT NULL,"
            " round INTEGER NOT NULL, trigger_type TEXT NOT NULL);"
            "CREATE TABLE agent_invocations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,"
            " step_name TEXT NOT NULL, round INTEGER NOT NULL, purpose TEXT NOT NULL,"
            " agent TEXT NOT NULL, model TEXT, started_at INTEGER NOT NULL);"
            "INSERT INTO repos VALUES ('r1', '/home/user/projects/app');"
            "INSERT INTO runs VALUES ('run1', 'r1', 'fm/old-schema', 'running', NULL, NULL,"
            " 1789990000, 1789990100);"
            "INSERT INTO step_results VALUES ('st1', 'run1', 'review', 3, 'completed', 40000,"
            " NULL, NULL, 1789990010, 1789990050, 1789990050, 'status: completed', NULL, NULL,"
            " NULL);"
            "INSERT INTO step_rounds VALUES ('rd1', 'st1', 1, 'initial');"
            "INSERT INTO agent_invocations VALUES ('ai1', 'run1', 'review', 1, 'review',"
            " 'claude', '', 1789990010);"
        )
        conn.commit()
        conn.close()

    def test_missing_columns_degrade_to_absent_without_crash(self):
        snapshot = state.read_snapshot(self.db, NOW_ACTIVE, LINGER)
        run = snapshot["runs"][0]
        for absent in ("intent", "pr_url", "base_sha", "head_sha", "parked_ms", "ci_ready_at",
                       "worktree_dir", "no_mistakes_version"):
            self.assertIsNone(run[absent])
        tabs = model.build_tabs(snapshot, {}, no_log, NOW_ACTIVE, LINGER)["tabs"]
        tab = tabs[0]
        self.assertIsNone(tab["intent"])
        self.assertIsNone(tab["intent_source"])
        self.assertIsNone(tab["base_sha"])
        self.assertIsNone(tab["pr_url"])
        self.assertIsNone(tab["parked_ms"])
        self.assertIsNone(tab["ci_ready_at"])
        node = tab["nodes"][0]
        self.assertIsNone(node["exit_code"])
        self.assertIsNone(node["override_reason"])
        self.assertEqual(node["finding_list"], [])
        self.assertEqual(node["round_list"][0]["duration_ms"], None)
        self.assertEqual(node["round_list"][0]["selection_source"], None)
        session = tab["sessions"]["rows"][0]
        self.assertEqual(session["tokens"], {})
        self.assertEqual(session["tools"], {})
        self.assertEqual(session["round"], 1)  # colonne de base : toujours là
        self.assertIsNone(session["duration_ms"])
        self.assertIsNone(session["exit"])
        self.assertEqual(tab["sessions"]["totals"]["duration_ms"], 0)


class WebWindow(unittest.TestCase):
    """La fenêtre est servie en fichiers statiques : on vérifie ce qu'elle embarque."""

    @classmethod
    def setUpClass(cls):
        web = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "nmn", "web")
        cls.js = read_bytes(os.path.join(web, "app.js")).decode("utf-8")
        cls.css = read_bytes(os.path.join(web, "app.css")).decode("utf-8")
        cls.html = read_bytes(os.path.join(web, "index.html")).decode("utf-8")

    def test_passes_are_shown_in_header_tab_nodes_and_table(self):
        self.assertIn('class="restartline"', self.js)
        self.assertIn('class="again"', self.js)
        self.assertIn('node.redo ? " redo" : ""', self.js)
        self.assertIn('class="pass-sep"', self.js)
        self.assertIn('"old-pass"', self.js)
        for selector in (".node.redo", ".sessions .pass-sep", ".sessions .old-pass", ".tab .again"):
            self.assertIn(selector, self.css)

    def test_q_quits_and_the_footer_says_so(self):
        self.assertIn('event.key === "q"', self.js)
        self.assertIn('fetch("/api/quit", { method: "POST" })', self.js)
        self.assertIn("<kbd>q</kbd> quitter et revenir au terminal", self.html)
        self.assertIn("footer .quit", self.css)

    def test_header_offers_a_branch_copy_button(self):
        self.assertIn('data-copy="${esc(tab.branch)}"', self.js)
        self.assertIn("navigator.clipboard.writeText", self.js)
        self.assertIn('closest("[data-copy]")', self.js)  # délégation : survit au re-rendu

    def test_details_block_is_collapsible_and_remembered(self):
        self.assertIn('<details class="rundetails" data-open-key="run:', self.js)
        self.assertIn('<details data-open-key="log:', self.js)
        self.assertIn("openState[key] = box.open", self.js)  # l'état survit au rafraîchissement
        self.assertIn('box.open = openState[key] === true', self.js)  # fermé par défaut

    def test_sessions_table_lists_tokens_tools_and_exit(self):
        self.assertIn("allers-retours", self.js)
        self.assertIn("cache écrit", self.js)
        self.assertIn("raisonnement", self.js)
        self.assertIn("<th title=\"lu dans la config actuelle", self.js)
        self.assertIn("r.effort", self.js)
        self.assertIn("EXIT_LABELS", self.js)
        self.assertIn("fallback", self.js)

    def test_findings_render_id_severity_and_file(self):
        self.assertIn("findingLine", self.js)
        self.assertIn('class="sev s-${esc(f.severity)}"', self.js)
        self.assertIn('title="${esc(f.file)}', self.js)

    def test_pr_and_ci_are_shown_in_the_header(self):
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.js)
        self.assertIn("tab.pr_url", self.js)
        self.assertIn("CI prête", self.js)

    def test_waiting_and_stuck_are_visible_on_tab_and_header(self):
        self.assertIn('class="wait"', self.js)
        self.assertIn('class="stuck"', self.js)
        self.assertIn("attend depuis", self.js)
        self.assertIn("sans signe de vie", self.js)

    def test_styles_cover_the_new_panels(self):
        for selector in (".rundetails", ".sessions", "pre.log", ".sev", ".tab .wait", ".node .stuck"):
            self.assertIn(selector, self.css)

    def test_copy_button_is_styled_and_window_stays_offline(self):
        self.assertIn(".runhead .copy", self.css)
        for asset in (self.js, self.css):
            self.assertNotIn("http://", asset)
            self.assertNotIn("https://", asset)


class QuitFromTheWindow(unittest.TestCase):
    """POST /api/quit arrête la commande, seulement depuis la page servie ici."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quit = threading.Event()
        self.httpd = server.make_server(0, self.tmp.name, LINGER, self.quit.set)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, path, headers):
        conn = http.client.HTTPConnection(self.host, timeout=2)
        self.addCleanup(conn.close)
        conn.request("POST", path, headers=headers)
        return conn.getresponse().status

    def test_same_origin_post_stops_the_command(self):
        status = self.post("/api/quit", {"Origin": f"http://{self.host}"})
        self.assertEqual(status, 200)
        self.assertTrue(self.quit.wait(2))

    def test_other_origins_cannot_stop_it(self):
        self.assertEqual(self.post("/api/quit", {}), 403)
        other = f"127.0.0.1:{self.httpd.server_address[1] + 1}"
        self.assertEqual(self.post("/api/quit", {"Origin": f"http://{other}"}), 403)
        self.assertEqual(self.post("/api/quit", {"Host": "rebound.test", "Origin": "null"}), 403)
        self.assertFalse(self.quit.is_set())


class FindBrowser(unittest.TestCase):
    """Sur macOS, le navigateur se trouve dans son bundle .app quand le PATH ne l'a pas."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tmp.name  # aucun navigateur sur le PATH
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)

    def app(self, name):
        folder = os.path.join(self.tmp.name, f"{name}.app", "Contents", "MacOS")
        os.makedirs(folder)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_mac_finds_the_first_installed_bundle(self):
        brave = self.app("Brave Browser")
        found = cli._find_app_browser(None, "darwin", [self.tmp.name])
        self.assertEqual(found, brave)
        chrome = self.app("Google Chrome")
        self.assertEqual(cli._find_app_browser(None, "darwin", [self.tmp.name]), chrome)

    def test_mac_honours_the_browser_option(self):
        self.app("Google Chrome")
        edge = self.app("Microsoft Edge")
        self.assertEqual(cli._find_app_browser("Microsoft Edge", "darwin", [self.tmp.name]), edge)

    def test_linux_ignores_app_bundles(self):
        self.app("Google Chrome")
        self.assertIsNone(cli._find_app_browser(None, "linux", [self.tmp.name]))

    def test_mac_profile_goes_to_library_caches(self):
        old = os.environ.pop("XDG_CACHE_HOME", None)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, "XDG_CACHE_HOME", old)
        self.assertEqual(cli._cache_dir("darwin"), os.path.expanduser("~/Library/Caches"))
        self.assertEqual(cli._cache_dir("linux"), os.path.expanduser("~/.cache"))


class NoRuns(unittest.TestCase):
    def test_empty_database_gives_no_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.sqlite")
            build_db(db, fixture=None).close()
            snapshot = state.read_snapshot(db, NOW, LINGER)
            self.assertEqual(model.build_tabs(snapshot, {}, no_log, NOW, LINGER)["tabs"], [])


class Config(unittest.TestCase):
    def test_reads_the_subset_used_by_no_mistakes(self):
        config = state.read_config(os.path.join(FIXTURES, "config.yaml"))
        self.assertEqual(config["agent"], "claude")
        self.assertEqual(config["ci_timeout"], "168h")
        self.assertIs(config["session_reuse"], True)
        self.assertEqual(config["auto_fix"], {"rebase": 3, "lint": 3, "review": 0})
        self.assertEqual(
            config["agent_args_override"]["pi"],
            ["--model", "opencode-go/glm-5.3-flash", "--thinking", "max"],
        )
        self.assertEqual(config["review_agents"]["fixer"], {"agent": "pi"})

    def test_inline_list_block_scalar_and_list_of_maps(self):
        config = miniyaml.load(
            "agent: [codex, claude]\ntest:\n  instructions: |\n    a: b\n    - c\n  x: 1\n"
            "items:\n  - name: a\n    n: 2\n  - name: b\n"
        )
        self.assertEqual(config["agent"], ["codex", "claude"])
        self.assertEqual(config["test"], {"instructions": None, "x": 1})
        self.assertEqual(config["items"], [{"name": "a", "n": 2}, {"name": "b"}])

    def test_agent_list_uses_its_first_entry_and_known_model(self):
        agent = model.resolve_agent({"agent": ["codex", "claude"]}, "test", False, None,
                                    {"codex": "gpt-5.6-sol"})
        self.assertEqual((agent["tool"], agent["model"]), ("codex", "gpt-5.6-sol"))
        self.assertEqual(agent["model_source"], "dernier appel")

    def test_missing_config_gives_no_agent(self):
        self.assertEqual(state.read_config("/nonexistent/config.yaml"), {})
        self.assertIsNone(model.resolve_agent({}, "test", False, None, {}))


class Findings(unittest.TestCase):
    def test_light_shape_from_real_payload(self):
        payload = json.dumps({"findings": [
            {"id": "a", "severity": "major", "file": "/x/y/z.ts", "line": 12, "description": "d"},
            {"id": "b"},
            "pas un dict",
        ]})
        self.assertEqual(
            model.parse_findings(payload),
            [
                {"id": "a", "title": None, "severity": "major", "file": "/x/y/z.ts",
                 "line": 12, "description": "d"},
                {"id": "b", "title": None, "severity": None, "file": None, "line": None,
                 "description": None},
            ],
        )

    def test_count_and_findings_tolerate_junk(self):
        self.assertEqual(model.parse_findings(None), [])
        self.assertEqual(model.parse_findings("{pas du json"), [])
        self.assertEqual(model.parse_findings('{"findings": null}'), [])
        self.assertEqual(model.count_findings('{"findings": [1, 2]}'), 2)
        self.assertEqual(model.count_findings("n'importe quoi"), 0)


class WaitTime(unittest.TestCase):
    def test_human_durations_in_french(self):
        self.assertEqual(model._human_s(45), "45 s")
        self.assertEqual(model._human_s(1200), "20 min")
        self.assertEqual(model._human_s(3600), "1,0 h")
        self.assertEqual(model._human_s(7200), "2,0 h")


class StepLog(unittest.TestCase):
    def test_names_the_agent_still_running(self):
        with open(os.path.join(FIXTURES, "review.log"), encoding="utf-8") as handle:
            tail = state.parse_log_tail(handle.read())
        self.assertEqual(tail["tool"], "pi")
        self.assertEqual(tail["last_line"], "Checking one detail first, then applying the fix.")
        self.assertIn("pi started", "\n".join(tail["lines"]))

    def test_no_agent_once_everyone_exited(self):
        tail = state.parse_log_tail(
            "claude started pid=7\nhello\nclaude exited pid=7 status=success\n")
        self.assertEqual(tail["tool"], None)
        self.assertEqual(tail["last_line"], "hello")
        self.assertEqual(len(tail["lines"]), 3)  # le panneau garde tout, lifecycle compris

    def test_keeps_only_the_last_thirty_lines(self):
        text = "\n".join(f"ligne {i}" for i in range(40))
        tail = state.parse_log_tail(text)
        self.assertEqual(tail["lines"], [f"ligne {i}" for i in range(10, 40)])
        self.assertEqual(tail["last_line"], "ligne 39")

    def test_rejects_paths_outside_the_logs_directory(self):
        self.assertIsNone(state.read_log_tail(FIXTURES, "../fixtures", "review"))


if __name__ == "__main__":
    unittest.main()
