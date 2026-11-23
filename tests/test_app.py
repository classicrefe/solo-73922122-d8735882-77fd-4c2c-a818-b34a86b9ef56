"""Unit and end-to-end tests (stdlib unittest, no extra dependencies)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="pump-test-")
os.environ["PUMP_DB"] = os.path.join(_tmp_dir, "test.db")
os.environ["FLASK_SECRET_KEY"] = "test-secret"

from app import create_app, services  # noqa: E402
from app.csvio import parse_csv  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.lifetable import (PumpRecord, build_life_table, incidence_at)  # noqa: E402

SAMPLE_CSV = """泵号,入册累计运行小时,出册累计运行小时,结束方式
P001,0,1000,故障
P002,0,1000,预防更换
P003,0,2000,观察结束
P004,500,2000,故障
P005,1000,3000,观察结束
P006,0,3000,预防更换
"""


def sample_records():
    return parse_csv(SAMPLE_CSV)


class LifeTableMathTests(unittest.TestCase):
    def setUp(self):
        self.rows = build_life_table(sample_records())
        self.by_t = {r["t_hours"]: r for r in self.rows}

    def test_time_points_and_order(self):
        self.assertEqual([r["t_hours"] for r in self.rows],
                         [0.0, 500.0, 1000.0, 2000.0, 3000.0])

    def test_entry_before_risk_count(self):
        # t=0: four pumps enter before any rate is computed
        r0 = self.by_t[0.0]
        self.assertEqual(r0["entered"], 4)
        self.assertEqual(r0["at_risk"], 4)
        # t=500: P004 enters, no exits -> risk set 5
        self.assertEqual(self.by_t[500.0]["at_risk"], 5)
        # t=1000: P005 enters (6 at risk), then 1 failure + 1 preventive leave
        r1000 = self.by_t[1000.0]
        self.assertEqual(r1000["entered"], 1)
        self.assertEqual(r1000["at_risk"], 6)
        self.assertEqual(r1000["failures"], 1)
        self.assertEqual(r1000["preventive"], 1)

    def test_hazards_and_cif(self):
        r1000 = self.by_t[1000.0]
        self.assertAlmostEqual(r1000["hazard_failure"], 1 / 6)
        self.assertAlmostEqual(r1000["hazard_preventive"], 1 / 6)
        self.assertAlmostEqual(r1000["survival"], 2 / 3)
        self.assertAlmostEqual(r1000["cum_failure"], 1 / 6)
        self.assertAlmostEqual(r1000["cum_preventive"], 1 / 6)

        r2000 = self.by_t[2000.0]
        # at risk: 4 remaining (P003,P004,P006 + P005); 1 failure, 1 censored
        self.assertEqual(r2000["at_risk"], 4)
        self.assertEqual(r2000["failures"], 1)
        self.assertEqual(r2000["censored"], 1)
        self.assertAlmostEqual(r2000["hazard_failure"], 1 / 4)
        self.assertAlmostEqual(r2000["survival"], (2 / 3) * (3 / 4))
        # CIF weighted by survival just before t (2/3)
        self.assertAlmostEqual(r2000["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4))
        self.assertAlmostEqual(r2000["cum_preventive"], 1 / 6)

    def test_censoring_only_leaves_risk_set(self):
        r3000 = self.by_t[3000.0]
        # one censored (P005) and one preventive (P006) leave; n=2
        self.assertEqual(r3000["at_risk"], 2)
        self.assertEqual(r3000["censored"], 1)
        self.assertEqual(r3000["failures"], 0)
        self.assertAlmostEqual(r3000["hazard_failure"], 0.0)
        self.assertAlmostEqual(r3000["cum_failure"],
                               self.by_t[2000.0]["cum_failure"])
        # after all exits the risk set is empty
        self.assertEqual(
            r3000["at_risk"] - r3000["failures"] - r3000["preventive"]
            - r3000["censored"], 0)

    def test_arbitrary_hour_step_function(self):
        before = incidence_at(self.rows, 999.0)
        self.assertAlmostEqual(before["cum_failure"], 0.0)
        at = incidence_at(self.rows, 1000.0)
        self.assertAlmostEqual(at["cum_failure"], 1 / 6)
        between = incidence_at(self.rows, 1500.0)
        self.assertAlmostEqual(between["cum_failure"], 1 / 6)
        self.assertEqual(between["basis_row"]["t_hours"], 1000.0)
        after = incidence_at(self.rows, 99999.0)
        self.assertAlmostEqual(after["cum_failure"],
                               self.by_t[3000.0]["cum_failure"])

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 10, 5, "failure")])
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 0, 1, "unknown")])
        with self.assertRaises(ValueError):
            build_life_table([PumpRecord("A", 0, 1, "failure"),
                              PumpRecord("A", 0, 2, "failure")])

    def test_same_hour_entry_and_exit_no_zero_division(self):
        # pump enters and leaves at the same hour: entry is counted first,
        # so the risk set never reaches zero before hazard computation
        rows = build_life_table([
            PumpRecord("A", 100, 100, "failure"),
            PumpRecord("B", 100, 100, "censored"),
        ])
        row = rows[0]
        self.assertEqual(row["t_hours"], 100.0)
        self.assertEqual(row["entered"], 2)
        self.assertEqual(row["at_risk"], 2)
        self.assertAlmostEqual(row["hazard_failure"], 0.5)
        self.assertAlmostEqual(row["cum_failure"], 0.5)
        self.assertAlmostEqual(row["survival"], 0.5)

    def test_all_censored_means_zero_incidence(self):
        rows = build_life_table([
            PumpRecord("A", 0, 100, "censored"),
            PumpRecord("B", 0, 200, "censored"),
        ])
        for row in rows:
            self.assertEqual(row["failures"], 0)
            self.assertEqual(row["preventive"], 0)
            self.assertAlmostEqual(row["cum_failure"], 0.0)
            self.assertAlmostEqual(row["cum_preventive"], 0.0)
            self.assertAlmostEqual(row["survival"], 1.0)

    def test_empty_ledger_gives_empty_summary(self):
        self.assertEqual(build_life_table([]), [])
        self.assertEqual(incidence_at([], 500.0)["cum_failure"], 0.0)


class CsvParseTests(unittest.TestCase):
    def test_english_headers_and_aliases(self):
        text = ("id,entry,exit,event\n"
                "X1,0,100,fail\nX2,0,200,p\nX3,0,300,c\n")
        recs = parse_csv(text)
        self.assertEqual([r.event_type for r in recs],
                         ["failure", "preventive", "censored"])

    def test_bad_rows_collected(self):
        text = ("泵号,入册累计运行小时,出册累计运行小时,结束方式\n"
                "B1,0,100,爆炸\n"
                "B2,abc,100,故障\n"
                "B3,500,100,故障\n")
        with self.assertRaises(ValueError) as ctx:
            parse_csv(text)
        self.assertEqual(str(ctx.exception).count("\n"), 3)

    def test_missing_column(self):
        with self.assertRaises(ValueError):
            parse_csv("泵号,入册,出册\nA,1,2\n")


class ServiceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(_tmp_dir, f"svc-{id(self)}.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_replace_and_query_summary(self):
        services.replace_ledger(self.conn, sample_records(), source="test")
        rows = services.get_life_table(self.conn)
        self.assertEqual(len(rows), 5)
        result = services.query_incidence(self.conn, 2000.0)
        self.assertAlmostEqual(result["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4))

    def test_single_pump_correction_without_other_details(self):
        services.replace_ledger(self.conn, sample_records())
        before = services.query_incidence(self.conn, 2000.0)["cum_failure"]

        # caller only supplies the one pump being corrected
        services.upsert_pump(self.conn,
                             PumpRecord("P004", 500, 2000, "censored"))
        pumps = {r["pump_id"]: r for r in services.list_pumps(self.conn)}
        self.assertEqual(pumps["P004"]["event_type"], "censored")
        # other pumps untouched
        self.assertEqual(pumps["P001"]["event_type"], "failure")
        after = services.query_incidence(self.conn, 2000.0)["cum_failure"]
        self.assertNotAlmostEqual(before, after)

        # insert a brand new pump the same way
        res = services.upsert_pump(self.conn,
                                   PumpRecord("P999", 0, 4000, "failure"))
        self.assertEqual(res["action"], "inserted")
        self.assertEqual(len(services.list_pumps(self.conn)), 7)

    def test_failure_rolls_back_keeps_old_values(self):
        services.replace_ledger(self.conn, sample_records())
        old_rows = [dict(r) for r in services.get_life_table(self.conn)]
        old_pumps = len(services.list_pumps(self.conn))

        def boom(_records):
            raise RuntimeError("simulated rebuild failure")

        original = services.build_life_table
        services.build_life_table = boom
        try:
            with self.assertRaises(RuntimeError):
                services.replace_ledger(
                    self.conn, [PumpRecord("Z", 0, 1, "failure")])
            with self.assertRaises(RuntimeError):
                services.upsert_pump(
                    self.conn, PumpRecord("P001", 0, 999, "censored"))
        finally:
            services.build_life_table = original

        self.assertEqual(len(services.list_pumps(self.conn)), old_pumps)
        new_rows = [dict(r) for r in services.get_life_table(self.conn)]
        self.assertEqual(new_rows, old_rows)
        p001 = services.get_pump(self.conn, "P001")
        self.assertEqual(p001["exit_hours"], 1000.0)
        self.assertEqual(p001["event_type"], "failure")


class FlaskWebTests(unittest.TestCase):
    def setUp(self):
        self.web_db = os.path.join(_tmp_dir, f"web-{id(self)}-{os.getpid()}.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.web_db + suffix):
                os.remove(self.web_db + suffix)
        self.app = create_app(self.web_db)
        self.client = self.app.test_client()

    def test_health_and_empty_pages(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("暂无数据", page)
        self.assertEqual(self.client.get("/query").status_code, 200)

    def test_upload_lifecycle_and_query_api(self):
        import io
        resp = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode("utf-8")),
                               "pumps.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传成功", resp.get_data(as_text=True))

        api = self.client.get("/api/query?hours=2000").get_json()
        self.assertAlmostEqual(api["cum_failure"],
                               1 / 6 + (2 / 3) * (1 / 4), places=12)
        self.assertEqual(api["basis_row"]["at_risk"], 4)

        page = self.client.get("/query?hours=1500").get_data(as_text=True)
        self.assertIn("逐时点计数", page)

        bad = self.client.get("/api/query?hours=abc")
        self.assertEqual(bad.status_code, 400)

    def test_bad_upload_keeps_previous_data(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        good = self.client.get("/api/lifetable").get_json()
        self.assertEqual(len(good), 5)

        bad_csv = "泵号,入册累计运行小时,出册累计运行小时,结束方式\nX,0,1,爆炸\n"
        resp = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(bad_csv.encode()), "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("CSV 解析失败", resp.get_data(as_text=True))
        self.assertEqual(len(self.client.get("/api/lifetable").get_json()), 5)

    def test_query_does_not_modify_summary(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        before = self.client.get("/api/lifetable").get_json()
        for h in (0, 999, 1000, 2500, 100000):
            self.client.get(f"/query?hours={h}")
            self.client.get(f"/api/query?hours={h}")
        after = self.client.get("/api/lifetable").get_json()
        self.assertEqual(before, after)

    def test_single_pump_edit_via_web(self):
        import io
        self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(SAMPLE_CSV.encode()), "ok.csv")},
            content_type="multipart/form-data")
        resp = self.client.post("/pumps/P002/edit", data={
            "pump_id": "P002",
            "entry_hours": "0",
            "exit_hours": "1234",
            "event_type": "failure",
        }, follow_redirects=True)
        self.assertIn("更正成功", resp.get_data(as_text=True))
        with self.app.app_context():
            row = services.get_pump(connect(self.web_db), "P002")
        self.assertEqual(row["exit_hours"], 1234.0)
        self.assertEqual(row["event_type"], "failure")

        # invalid form is rejected and old value preserved
        resp = self.client.post("/pumps/P002/edit", data={
            "pump_id": "P002", "entry_hours": "9000",
            "exit_hours": "10", "event_type": "failure",
        })
        self.assertEqual(resp.status_code, 400)
        row = services.get_pump(connect(self.web_db), "P002")
        self.assertEqual(row["exit_hours"], 1234.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
