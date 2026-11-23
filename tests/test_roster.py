"""Tests for the case-control matching roster (algorithm, services, web)."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".pylibs"))

_tmp_dir = tempfile.mkdtemp(prefix="pump-roster-test-")
os.environ["PUMP_DB"] = os.path.join(_tmp_dir, "test.db")
os.environ["FLASK_SECRET_KEY"] = "test-secret"

from app import create_app, services  # noqa: E402
from app.csvio import parse_csv  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.lifetable import PumpRecord  # noqa: E402
from app.matching import (MAX_CONTROL_PICKS, MAX_FAILURE_PICKS,  # noqa: E402
                          build_matching, is_eligible)
from werkzeug.datastructures import MultiDict  # noqa: E402

SAMPLE_CSV = """泵号,入册累计运行小时,出册累计运行小时,结束方式
F1,0,1000,故障
F2,0,2000,故障
F3,500,2500,故障
F4,1000,3000,故障
F5,0,4000,故障
F6,1500,3500,故障
C1,0,3000,预防更换
C2,100,2800,观察结束
C3,0,1800,预防更换
C4,800,3200,观察结束
C5,1000,3000,观察结束
C6,1200,4200,预防更换
C7,0,5000,观察结束
C8,1500,3600,观察结束
C9,200,2200,预防更换
C10,0,4500,预防更换
C11,500,5000,观察结束
C12,2000,4000,观察结束
"""


def sample_records():
    return parse_csv(SAMPLE_CSV)


def roster_form(max_diff="500"):
    return MultiDict([
        *[("failure_ids", f"F{i}") for i in range(1, 7)],
        *[("candidate_ids", f"C{i}") for i in range(1, 13)],
        ("max_diff_hours", max_diff),
    ])


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.f = PumpRecord("A", 100, 1000, "failure")

    def test_control_must_be_registered_at_failure_hour(self):
        self.assertTrue(is_eligible(self.f, PumpRecord("X", 1000, 2000, "censored"), 900))
        self.assertFalse(is_eligible(self.f, PumpRecord("X", 1001, 2000, "censored"), 901))

    def test_exit_after_failure_or_censored_exactly_at_failure(self):
        self.assertTrue(is_eligible(self.f, PumpRecord("X", 100, 1001, "preventive"), 0))
        self.assertTrue(is_eligible(self.f, PumpRecord("X", 100, 1000, "censored"), 0))
        self.assertFalse(is_eligible(self.f, PumpRecord("X", 100, 1000, "preventive"), 0))
        self.assertFalse(is_eligible(self.f, PumpRecord("X", 100, 1000, "failure"), 0))
        self.assertFalse(is_eligible(self.f, PumpRecord("X", 0, 999, "censored"), 100))

    def test_entry_hour_diff_limit(self):
        self.assertTrue(is_eligible(self.f, PumpRecord("X", 600, 2000, "censored"), 500))
        self.assertFalse(is_eligible(self.f, PumpRecord("X", 601, 2000, "censored"), 500))


class MatchingOptimisationTests(unittest.TestCase):
    def test_maximum_cardinality_first_even_at_cost_of_retention(self):
        recs = [
            PumpRecord("A", 0, 1000, "failure"),
            PumpRecord("B", 0, 2000, "failure"),
            PumpRecord("X", 0, 3000, "censored"),
            PumpRecord("Y", 0, 1500, "censored"),
        ]
        # previous roster paired A-X, B-Y, but Y is not eligible for B now;
        # keeping A-X leaves B with nothing, so the optimum drops retention.
        res = build_matching(recs, ["A", "B"], ["X", "Y"], 100,
                             {"A": "X", "B": "Y"})
        self.assertEqual(res["pair_count"], 2)
        self.assertEqual(res["retained_count"], 0)
        pairs = {p.failure_id: p.control_id for p in res["pairs"]}
        self.assertEqual(pairs, {"A": "Y", "B": "X"})

    def test_retention_maximised_among_maximum_matchings(self):
        recs = [
            PumpRecord("A", 0, 1000, "failure"),
            PumpRecord("B", 0, 2000, "failure"),
            PumpRecord("X", 0, 3000, "censored"),
            PumpRecord("Y", 0, 4000, "censored"),
        ]
        res = build_matching(recs, ["A", "B"], ["X", "Y"], 100, {"A": "X"})
        self.assertEqual(res["pair_count"], 2)
        self.assertEqual(res["retained_count"], 1)
        pairs = {p.failure_id: p.control_id for p in res["pairs"]}
        self.assertEqual(pairs, {"A": "X", "B": "Y"})

    def test_each_control_used_once(self):
        recs = [PumpRecord(f"F{i}", 0, 1000 + 100 * i, "failure") for i in range(3)]
        recs += [PumpRecord("C1", 0, 5000, "censored"),
                 PumpRecord("C2", 0, 5000, "preventive")]
        res = build_matching(recs, ["F0", "F1", "F2"], ["C1", "C2"], 10)
        self.assertEqual(res["pair_count"], 2)
        self.assertEqual(len(res["used_controls"]), 2)

    def test_selected_failures_never_serve_as_controls(self):
        recs = [PumpRecord("A", 0, 1000, "failure"),
                PumpRecord("B", 0, 2000, "failure")]
        res = build_matching(recs, ["A", "B"], ["A", "B"], 100)
        self.assertEqual(res["control_total"], 0)
        self.assertEqual(res["pair_count"], 0)

    def test_statuses_missing_not_failure_unmatched(self):
        recs = [PumpRecord("A", 0, 1000, "failure"),
                PumpRecord("B", 0, 500, "preventive")]
        res = build_matching(recs, ["A", "GONE", "B"], ["B"], 100)
        states = {s.pump_id: s.state for s in res["statuses"]}
        self.assertEqual(states, {"A": "unmatched", "GONE": "missing",
                                  "B": "not_failure"})

    def test_matched_excluded_from_status_others_coexist(self):
        recs = [PumpRecord("A", 0, 1000, "failure"),
                PumpRecord("B", 0, 2000, "failure"),
                PumpRecord("X", 0, 5000, "censored")]
        res = build_matching(recs, ["A", "B", "GONE"], ["X"], 10)
        states = {s.pump_id: s.state for s in res["statuses"]}
        self.assertEqual(states, {"B": "unmatched", "GONE": "missing"})
        self.assertEqual([p.failure_id for p in res["pairs"]], ["A"])

    def test_deterministic_rerun_retains_everything(self):
        recs = sample_records()
        fids = [f"F{i}" for i in range(1, 7)]
        cids = [f"C{i}" for i in range(1, 13)]
        first = build_matching(recs, fids, cids, 500)
        prev = {p.failure_id: p.control_id for p in first["pairs"]}
        second = build_matching(recs, fids, cids, 500, prev)
        self.assertEqual(second["pair_count"], first["pair_count"])
        self.assertEqual(second["retained_count"], first["pair_count"])

    def test_order_independent_and_deterministic(self):
        recs = sample_records()
        fids = [f"F{i}" for i in range(1, 7)]
        cids = [f"C{i}" for i in range(1, 13)]
        r1 = build_matching(recs, fids, cids, 500)
        r2 = build_matching(recs, list(reversed(fids)), list(reversed(cids)), 500)
        key = lambda rs: sorted((p.failure_id, p.control_id) for p in rs["pairs"])
        self.assertEqual(key(r1), key(r2))


class RosterServiceTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(_tmp_dir, f"roster-svc-{id(self)}.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.remove(self.db_path + suffix)
        self.conn = connect(self.db_path)
        init_db(self.conn)
        services.replace_ledger(self.conn, sample_records(), source="test")

    def tearDown(self):
        self.conn.close()

    def _fids(self):
        return [f"F{i}" for i in range(1, 7)]

    def _cids(self):
        return [f"C{i}" for i in range(1, 13)]

    def test_save_and_read_roster(self):
        out = services.save_roster(self.conn, self._fids(), self._cids(), 500)
        self.assertEqual(out["pair_count"], 6)
        roster = services.get_roster(self.conn)
        self.assertEqual(len(roster["pairs"]), 6)
        self.assertEqual(roster["stats"]["pair_count"], 6)
        controls = [p["control_id"] for p in roster["pairs"]]
        self.assertEqual(len(controls), len(set(controls)))
        for p in roster["pairs"]:
            self.assertLessEqual(p["diff_hours"], 500 + 1e-9)

    def test_limits_and_validation(self):
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, [], self._cids(), 10)
        with self.assertRaises(ValueError):
            services.save_roster(
                self.conn, [f"F{i}" for i in range(MAX_FAILURE_PICKS + 1)],
                self._cids(), 10)
        with self.assertRaises(ValueError):
            services.save_roster(
                self.conn, self._fids(),
                [f"C{i}" for i in range(MAX_CONTROL_PICKS + 1)], 10)
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, ["F1"], ["F1"], 10)
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, ["F1"], ["NOPE"], 10)
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, ["C1"], ["C2"], 10)  # C1 not failure
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, ["F1"], ["C1"], -5)

    def test_roster_rebuilds_on_single_pump_correction(self):
        services.save_roster(self.conn, self._fids(), self._cids(), 500)
        before = {p["failure_id"]: p["control_id"]
                  for p in services.get_roster(self.conn)["pairs"]}
        self.assertEqual(len(before), 6)

        # F3 becomes non-failure: it must be flagged and its control freed
        services.upsert_pump(self.conn,
                             PumpRecord("F3", 500, 2500, "preventive"))
        roster = services.get_roster(self.conn)
        states = {f["pump_id"]: f["state"] for f in roster["failures"]}
        self.assertEqual(states["F3"], "not_failure")
        self.assertEqual(roster["stats"]["pair_count"], 5)
        # all remaining pairs retained
        self.assertTrue(all(p["retained"] for p in roster["pairs"]))
        still = {p["failure_id"]: p["control_id"] for p in roster["pairs"]}
        for fid, cid in still.items():
            self.assertEqual(before[fid], cid)

    def test_missing_record_flagged_after_whole_ledger_replace(self):
        services.save_roster(self.conn, self._fids(), self._cids(), 500)
        smaller = [r for r in sample_records() if r.pump_id != "F6"]
        services.replace_ledger(self.conn, smaller, source="csv:smaller")
        roster = services.get_roster(self.conn)
        states = {f["pump_id"]: f["state"] for f in roster["failures"]}
        self.assertEqual(states["F6"], "missing")
        self.assertNotIn("F6", [p["failure_id"] for p in roster["pairs"]])

    def test_failed_save_keeps_previous_roster(self):
        services.save_roster(self.conn, self._fids(), self._cids(), 500)
        old = services.get_roster(self.conn)
        with self.assertRaises(ValueError):
            services.save_roster(self.conn, ["F1"], ["F1"], 500)
        again = services.get_roster(self.conn)
        self.assertEqual([p["control_id"] for p in again["pairs"]],
                         [p["control_id"] for p in old["pairs"]])
        self.assertEqual(again["max_diff_hours"], 500)

    def test_roster_rebuild_failure_rolls_back_ledger(self):
        services.save_roster(self.conn, self._fids(), self._cids(), 500)
        old_pumps = len(services.list_pumps(self.conn))
        original = services.build_matching

        def boom(*_a, **_k):
            raise RuntimeError("simulated matching failure")

        services.build_matching = boom
        try:
            with self.assertRaises(RuntimeError):
                services.upsert_pump(
                    self.conn, PumpRecord("F1", 0, 1001, "failure"))
        finally:
            services.build_matching = original
        self.assertEqual(len(services.list_pumps(self.conn)), old_pumps)
        row = services.get_pump(self.conn, "F1")
        self.assertEqual(row["exit_hours"], 1000.0)


class RosterWebTests(unittest.TestCase):
    def setUp(self):
        self.web_db = os.path.join(_tmp_dir, f"web-{id(self)}-{os.getpid()}.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.web_db + suffix):
                os.remove(self.web_db + suffix)
        self.app = create_app(self.web_db)
        self.client = self.app.test_client()
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "pumps.csv")},
            content_type="multipart/form-data")

    def test_roster_page_empty_then_save_flow(self):
        page = self.client.get("/roster").get_data(as_text=True)
        self.assertIn("新建配对名单", page)
        self.assertEqual(self.client.get("/api/roster").status_code, 404)

        resp = self.client.post("/roster/save", data=roster_form(),
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("名单已保存", resp.get_data(as_text=True))

        api = self.client.get("/api/roster").get_json()
        self.assertEqual(len(api["pairs"]), 6)
        self.assertEqual(api["stats"]["pair_count"], 6)
        self.assertEqual(len({p["control_id"] for p in api["pairs"]}), 6)

    def test_bad_roster_form_rejected(self):
        resp = self.client.post("/roster/save", data=MultiDict([
            ("failure_ids", "F1"), ("candidate_ids", "F1"),
            ("max_diff_hours", "500"),
        ]), follow_redirects=True)
        self.assertIn("名单参数有误", resp.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/roster").status_code, 404)

        resp = self.client.post("/roster/save", data=MultiDict([
            ("failure_ids", "F1"), ("candidate_ids", "C1"),
            ("max_diff_hours", "abc"),
        ]), follow_redirects=True)
        self.assertIn("工时差上限", resp.get_data(as_text=True))

    def test_single_pump_correction_updates_roster_and_page_flags(self):
        self.client.post("/roster/save", data=roster_form())
        before = self.client.get("/api/roster").get_json()
        self.assertEqual(before["stats"]["retained_count"], 0)

        self.client.post("/pumps/F3/edit", data={
            "pump_id": "F3", "entry_hours": "500",
            "exit_hours": "2500", "event_type": "preventive",
        }, follow_redirects=True)

        page = self.client.get("/roster").get_data(as_text=True)
        self.assertIn("已改为非故障", page)
        api = self.client.get("/api/roster").get_json()
        self.assertEqual(api["stats"]["not_failure_count"], 1)
        self.assertEqual(api["stats"]["pair_count"], 5)
        self.assertEqual(api["stats"]["retained_count"], 5)
        f3 = next(f for f in api["failures"] if f["pump_id"] == "F3")
        self.assertEqual(f3["state"], "not_failure")

    def test_bad_upload_keeps_roster_intact(self):
        self.client.post("/roster/save", data=roster_form())
        before = self.client.get("/api/roster").get_json()
        bad_csv = "泵号,入册累计运行小时,出册累计运行小时,结束方式\nX,0,1,爆炸\n"
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(bad_csv.encode()), "bad.csv")},
            content_type="multipart/form-data")
        after = self.client.get("/api/roster").get_json()
        self.assertEqual([p["control_id"] for p in after["pairs"]],
                         [p["control_id"] for p in before["pairs"]])

    def test_corrected_full_reupload_demo_scenario(self):
        # initial roster on the full sample
        self.client.post("/roster/save", data=roster_form())
        corrected_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "sample", "pumps_sample_corrected.csv")
        with open(corrected_path, encoding="utf-8") as fh:
            corrected = fh.read()
        resp = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(corrected.encode()), "v2.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("上传成功", resp.get_data(as_text=True))
        api = self.client.get("/api/roster").get_json()
        # F3 turned non-failure and C3 disappeared from the ledger
        states = {f["pump_id"]: f["state"] for f in api["failures"]}
        self.assertEqual(states["F3"], "not_failure")
        c3 = next(c for c in api["candidates"] if c["pump_id"] == "C3")
        self.assertFalse(c3["exists"])
        self.assertEqual(api["stats"]["pair_count"], 5)
        # retained pairs keep the original controls
        self.assertTrue(all(p["retained"] for p in api["pairs"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
