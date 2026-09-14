# -*- coding: utf-8 -*-
"""Wave1 序6 —— 接管重验凭证（终稿 T4）。

验收锚：04e338e4 事故形态（分析 epoch0 → 执行 epoch1）必须被识别；无凭证/
凭证被改/重验未全过 → 不得进 executor；同 actor 正常轮零负担；输出零模型名。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import actor_attestation as aa  # noqa: E402

CST = timezone(timedelta(hours=8))


def _mk_session(tmp: Path, key: str, models_turns: list[tuple[str, str]]):
    """构造 openclaw 会话 fixture：sessions.json + <id>.jsonl。"""
    sessions_dir = tmp / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = "fixture-" + key
    index_path = sessions_dir / "sessions.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) \
        if index_path.exists() else {}
    index[f"agent:okx-live-trader:{key}"] = {"sessionId": session_id}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    lines = []
    for ts, model in models_turns:
        lines.append(json.dumps({
            "type": "message", "timestamp": ts,
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "x"}]},
        }))
    (sessions_dir / f"{session_id}.jsonl").write_text(
        "\n".join(lines), encoding="utf-8")


def _mk_analysis_db(tmp: Path, cycle: str, ts_cst: str):
    db_root = tmp / "db"
    db_root.mkdir(exist_ok=True)
    db = db_root / "analysis.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE analysis_runs(cycle_id TEXT, status TEXT, ts TEXT)")
    con.execute(
        "CREATE TABLE analysis_signals(cycle_id TEXT, symbol TEXT, "
        "action TEXT, side TEXT, decision_card TEXT)")
    con.execute("INSERT INTO analysis_runs VALUES(?,?,?)", (cycle, "ok", ts_cst))
    con.commit()
    con.close()
    return db_root


class TimelineTests(unittest.TestCase):
    def test_incident_shape_two_epochs_opaque_fps(self):
        """T4：模型切换 → 2 epoch；输出只含指纹，绝无 runtime 身份明文。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mk_session(root, "live-20260810-1115", [
                ("2026-08-10T03:21:01.000Z", "alpha-model-a"),
                ("2026-08-10T03:31:56.000Z", "alpha-model-a"),
                ("2026-08-10T03:32:24.000Z", "beta-model-b"),
                ("2026-08-10T03:35:25.000Z", "beta-model-b"),
            ])
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", root):
                state = aa.timeline_state(
                    "2026-08-10T11:15", "2026-08-10 11:31:01")
        self.assertTrue(state["available"])
        self.assertEqual(state["epoch_count"], 2)
        self.assertEqual(state["analysis_epoch"], 0)
        self.assertEqual(state["current_epoch"], 1)
        self.assertTrue(state["handoff_detected"])
        dumped = json.dumps(state)
        self.assertNotIn("alpha-model-a", dumped)
        self.assertNotIn("beta-model-b", dumped)

    def test_same_actor_no_handoff(self):
        """T8：同 actor 正常轮 handoff=False（零额外负担路径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mk_session(root, "live-20260810-2245", [
                ("2026-08-10T14:46:00.000Z", "alpha-model-a"),
                ("2026-08-10T14:51:00.000Z", "alpha-model-a"),
            ])
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", root):
                state = aa.timeline_state(
                    "2026-08-10T22:45", "2026-08-10 22:49:23")
        self.assertTrue(state["available"])
        self.assertFalse(state["handoff_detected"])

    def test_unresolvable_session_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", Path(tmp)):
                state = aa.timeline_state(
                    "2026-08-10T11:15", "2026-08-10 11:31:01")
        self.assertFalse(state["available"])

    def test_session_key_derivation(self):
        self.assertEqual(
            aa.session_key_for_cycle("2026-08-10T11:15"),
            "live-20260810-1115")
        self.assertIsNone(aa.session_key_for_cycle("garbage"))


class AttestationRoundTripTests(unittest.TestCase):
    def test_handoff_attestation_verifies_and_tamper_fails(self):
        """T4：凭证生成→executor 独立校验通过；改一个字段即失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle = "2026-08-10T11:15"
            _mk_session(root, "live-20260810-1115", [
                ("2026-08-10T03:21:01.000Z", "alpha-model-a"),
                ("2026-08-10T03:32:24.000Z", "beta-model-b"),
            ])
            db_root = _mk_analysis_db(root, cycle, "2026-08-10 11:31:01")
            revalidation = {
                "all_ok": True,
                "analysis_ok": True,
                "open_signal_count": 1,
                "facts_ok": True,
                "news_context_ok": True,
                "ev_check_ok": True,
            }
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", root), \
                 mock.patch.object(aa, "_revalidate",
                                   return_value=revalidation):
                att = aa.build_attestation(cycle, db_root=db_root)
                self.assertTrue(att["timeline"]["handoff_detected"])
                self.assertIn("revalidation", att)
                # 本用例只验证凭证签名与独立重验的一致性；事实包细节另测。
                self.assertTrue(att["revalidation"]["all_ok"], att)
                self.assertEqual(
                    aa.verify_attestation(att, cycle, db_root=db_root), [])
                tampered = json.loads(json.dumps(att))
                tampered["revalidation"]["all_ok"] = True
                tampered["timeline"]["handoff_detected"] = False
                errors = aa.verify_attestation(tampered, cycle,
                                               db_root=db_root)
                self.assertTrue(
                    any("attestation_hash" in e for e in errors), errors)

    def test_missing_or_wrong_cycle_rejected(self):
        errors = aa.verify_attestation(None, "2026-08-10T11:15")
        self.assertTrue(errors)
        errors2 = aa.verify_attestation(
            {"version": aa.ATTESTATION_VERSION, "cycle_id": "2026-08-10T10:00",
             "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
             "attestation_hash": "x"},
            "2026-08-10T11:15")
        self.assertTrue(any("不符" in e for e in errors2), errors2)

    def test_stale_attestation_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle = "2026-08-10T11:15"
            _mk_session(root, "live-20260810-1115", [
                ("2026-08-10T03:21:01.000Z", "alpha-model-a"),
                ("2026-08-10T03:32:24.000Z", "beta-model-b"),
            ])
            db_root = _mk_analysis_db(root, cycle, "2026-08-10 11:31:01")
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", root):
                att = aa.build_attestation(cycle, db_root=db_root)
                old = (datetime.now(CST) - timedelta(seconds=3600)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                att_stale = {k: v for k, v in att.items()
                             if k != "attestation_hash"}
                att_stale["generated_at"] = old
                import hashlib
                att_stale["attestation_hash"] = hashlib.sha256(
                    json.dumps(att_stale, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16]
                errors = aa.verify_attestation(att_stale, cycle,
                                               db_root=db_root)
                self.assertTrue(any("过期" in e for e in errors), errors)

    def test_post_attestation_switch_detected(self):
        """凭证生成后 actor 再次切换 → chain hash 不符 → 拒。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle = "2026-08-10T11:15"
            turns = [
                ("2026-08-10T03:21:01.000Z", "alpha-model-a"),
                ("2026-08-10T03:32:24.000Z", "beta-model-b"),
            ]
            _mk_session(root, "live-20260810-1115", turns)
            db_root = _mk_analysis_db(root, cycle, "2026-08-10 11:31:01")
            with mock.patch.object(aa, "_OPENCLAW_AGENT_DIR", root):
                att = aa.build_attestation(cycle, db_root=db_root)
                _mk_session(root, "live-20260810-1115", turns + [
                    ("2026-08-10T03:40:00.000Z", "gamma-model-c"),
                ])
                errors = aa.verify_attestation(att, cycle, db_root=db_root)
                self.assertTrue(
                    any("actor_chain_hash" in e for e in errors), errors)


class SQLiteTimelineTests(unittest.TestCase):
    CYCLE = "2026-09-05T12:15"
    ANALYSIS = "2026-09-05 12:19:00"

    def make_store(self, root, second_actor="actor-a"):
        path = root / "agent" / "openclaw-agent.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as c, c:
            c.executescript("""
                PRAGMA user_version=19;
                CREATE TABLE session_nodes(session_key TEXT PRIMARY KEY,current_session_id TEXT,entry_valid INTEGER);
                CREATE TABLE session_windows(session_id TEXT PRIMARY KEY,session_key TEXT);
                CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT,PRIMARY KEY(session_id,seq));
            """)
            key = "agent:okx-live-trader:live-20260905-1215"
            c.execute("INSERT INTO session_nodes VALUES(?,?,1)", (key,"sid-1"))
            c.execute("INSERT INTO session_windows VALUES(?,?)", ("sid-1",key))
            for seq, ts, identity in [(1,"2026-09-05T04:16:00Z","actor-a"),
                                      (2,"2026-09-05T04:20:00Z",second_actor)]:
                c.execute("INSERT INTO transcript_events VALUES(?,?,?)", ("sid-1",seq,json.dumps({
                    "timestamp":ts,"message":{"role":"assistant","model":identity}})))
        return path

    def state(self, root):
        with mock.patch.object(aa,"_OPENCLAW_AGENT_DIR",root):
            return aa.timeline_state(self.CYCLE,self.ANALYSIS)

    def test_sqlite_same_actor_and_handoff(self):
        for second, expected in [("actor-a",False),("actor-b",True)]:
            with self.subTest(second=second), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path=self.make_store(root,second)
                before=path.read_bytes(); state=self.state(root)
                self.assertTrue(state["available"],state)
                self.assertEqual(state["handoff_detected"],expected)
                self.assertEqual(state["session_id"],"sid-1")
                self.assertNotIn("actor-a",json.dumps(state))
                self.assertEqual(path.read_bytes(),before)

    def test_authoritative_sqlite_failure_never_uses_legacy_export(self):
        for mutation in ["PRAGMA user_version=99", "DELETE FROM session_nodes",
                         "UPDATE session_windows SET session_key='another-cycle'",
                         "UPDATE session_nodes SET entry_valid=0",
                         "UPDATE transcript_events SET event_json='broken' WHERE seq=2"]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path=self.make_store(root)
                _mk_session(root,"live-20260905-1215",[("2026-09-05T04:16:00Z","actor-a")])
                with contextlib.closing(sqlite3.connect(path)) as c, c:c.execute(mutation)
                self.assertFalse(self.state(root)["available"])

    def test_missing_identity_timestamp_or_earlier_analysis_blocks(self):
        for event in [{"timestamp":"2026-09-05T04:20:00Z","message":{"role":"assistant"}},
                      {"timestamp":"2026-09-05T04:20:00","message":{"role":"assistant","model":"a"}},
                      {"timestamp":"2026-09-05T04:15:00Z","message":{"role":"assistant","model":"a"}}]:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);path=self.make_store(root)
                with contextlib.closing(sqlite3.connect(path)) as c, c:
                    c.execute("UPDATE transcript_events SET event_json=? WHERE seq=2",(json.dumps(event),))
                self.assertFalse(self.state(root)["available"])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.make_store(root)
            with mock.patch.object(aa,"_OPENCLAW_AGENT_DIR",root):
                self.assertFalse(aa.timeline_state(self.CYCLE,"2026-09-05 12:15:01")["available"])

    def test_attestation_rechecks_session_rotation_and_store_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.make_store(root)
            db_root=_mk_analysis_db(root,self.CYCLE,self.ANALYSIS)
            with mock.patch.object(aa,"_OPENCLAW_AGENT_DIR",root):
                att=aa.build_attestation(self.CYCLE,db_root=db_root)
                self.assertEqual(aa.verify_attestation(att,self.CYCLE,db_root=db_root),[])
                with contextlib.closing(sqlite3.connect(path)) as c, c:
                    c.execute("UPDATE session_nodes SET current_session_id='sid-2'")
                    c.execute("UPDATE session_windows SET session_id='sid-2'")
                    c.execute("UPDATE transcript_events SET session_id='sid-2'")
                self.assertTrue(any("session_id" in x for x in aa.verify_attestation(att,self.CYCLE,db_root=db_root)))
                with contextlib.closing(sqlite3.connect(path)) as c, c:c.execute("DELETE FROM transcript_events")
                self.assertTrue(any("actor_timeline_required" in x for x in aa.verify_attestation(att,self.CYCLE,db_root=db_root)))

    def test_gateway_publication_is_not_a_model_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.make_store(root)
            event={"timestamp":"2026-09-05T04:21:00Z","message":{
                "role":"assistant","provider":"openclaw","model":"gateway-injected",
                "content":[{"type":"text","text":"run ended"}]}}
            with contextlib.closing(sqlite3.connect(path)) as c, c:
                c.execute("INSERT INTO transcript_events VALUES(?,?,?)",("sid-1",3,json.dumps(event)))
            self.assertFalse(self.state(root)["handoff_detected"])
            event['message']['content']=[{"type":"toolCall","name":"exec"}]
            with contextlib.closing(sqlite3.connect(path)) as c, c:
                c.execute("UPDATE transcript_events SET event_json=? WHERE seq=3",(json.dumps(event),))
            self.assertTrue(self.state(root)["handoff_detected"])


if __name__ == "__main__":
    unittest.main()
