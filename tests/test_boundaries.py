import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from climate_ledger.__main__ import main
from climate_ledger.application import Application
from climate_ledger.contracts import DomainError, canonical
from climate_ledger.formats import decode_csv, encode_csv


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class BoundaryTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.app.ingest(example("city.json"))
        self.boundary_bundle = example("boundary.json")
        self.app.ingest(self.boundary_bundle)
        self.project_query = example("boundary_exposure.json")

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def record(self, identity_field, identity):
        return copy.deepcopy(next(item for item in self.boundary_bundle["records"]
                                  if item["data"].get(identity_field) == identity))

    def ingest(self, *records, name="boundary-change"):
        return self.app.ingest({"id": name, "source": self.boundary_bundle["source"],
                                "records": list(records)})


class ProjectionSemanticsTests(BoundaryTestBase):
    def test_population_shares_reproduce_the_worked_example(self):
        trace = self.app.trace("east", mode="project", day="2026-08-01")
        denominator = trace["denominator"]
        groups = {g["group"]: g for g in denominator["groups"]}
        self.assertTrue(denominator["complete"])
        self.assertEqual(groups["adult"]["count"], 60)
        self.assertEqual(groups["senior"]["count"], 5)
        west = self.app.trace("west", mode="project", day="2026-08-01")["denominator"]
        west_groups = {g["group"]: g for g in west["groups"]}
        self.assertEqual(west_groups["adult"]["count"], 20)
        self.assertEqual(west_groups["senior"]["count"], 15)

    def test_projected_population_is_conserved_against_native_zone_every_day(self):
        native = self.app.analyze({"metric": "heat", "zones": ["river"],
                                   "start": "2026-08-01", "end": "2026-08-04",
                                   "minimum_coverage": "0.8"})
        projected = self.app.analyze(self.project_query)
        for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
            east = next(d for d in projected["daily"] if d["zone"] == "east" and d["day"] == day)
            west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == day)
            river = next(d for d in native["daily"] if d["day"] == day)
            self.assertEqual(east["population"]["population"] + west["population"]["population"],
                             river["population"]["population"])

    def test_missing_station_coverage_is_a_gap_not_fabricated_weather(self):
        projected = self.app.analyze(self.project_query)
        east_days = [d for d in projected["daily"] if d["zone"] == "east"]
        self.assertTrue(east_days)
        for day in east_days:
            self.assertFalse(day["usable"])
            self.assertEqual(day["reason"], ["station_coverage_missing"])
            self.assertIsNone(day["weather"]["value"])
            self.assertIsNone(day["hazardous"])
            self.assertGreater(day["population"]["population"], 0)

    def test_population_ratio_is_never_used_as_station_coverage(self):
        # 东片区有人口份额但没有任何站点覆盖：人口比例再高也不能生成气象值
        projected = self.app.analyze(self.project_query)
        east = next(d for d in projected["daily"] if d["zone"] == "east" and d["day"] == "2026-08-01")
        self.assertEqual(east["weather"]["coverage"], "0.000000")
        self.assertEqual(east["weather"]["evidence"], [])

    def test_distinguish_missing_reading_from_missing_coverage(self):
        projected = self.app.analyze(self.project_query)
        west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == "2026-08-04")
        self.assertFalse(west["usable"])
        self.assertEqual(west["reason"], ["observation_gap"])
        self.assertEqual(west["weather"]["issues"][0]["reason"], "missing")

    def test_true_zero_exposure_is_kept_distinct_from_gap(self):
        # 无危险日（hill 的 water 指标不触发高温）保护为零且可用，不是缺测
        query = {"metric": "water", "zones": ["hill"], "start": "2026-07-01", "end": "2026-07-04",
                 "minimum_coverage": "0.8", "boundary": {"mode": "project"}}
        result = self.app.analyze(query)
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["total"]["known_exposed_person_days"], 0)
        self.assertEqual(result["total"]["unknown_zone_days"], 0)

    def test_facility_capacity_is_split_not_duplicated_across_targets(self):
        projected = self.app.analyze(self.project_query)
        west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == "2026-08-03")
        self.assertEqual(west["protected"], 20)
        self.assertEqual(sum(a["capacity"] for a in west["actions"]), 20)
        # 原区当天总容量30，西片区只拿到20，东片区10个名额因缺站点覆盖不能落地
        native = self.app.analyze({"metric": "heat", "zones": ["river"],
                                   "start": "2026-08-03", "end": "2026-08-03",
                                   "minimum_coverage": "0.8"})
        self.assertEqual(native["daily"][0]["protected"], 30)

    def test_protection_respects_group_vulnerability_and_real_headcount(self):
        projected = self.app.analyze(self.project_query)
        west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == "2026-08-03")
        groups = {g["group"]: g for g in west["groups"]}
        # 老年人（脆弱度2）先受保护，西片区15名老年人全部覆盖
        self.assertEqual(groups["senior"]["protected"], 15)
        self.assertEqual(groups["senior"]["exposed"], 0)
        self.assertEqual(groups["adult"]["protected"], 5)
        self.assertLessEqual(groups["adult"]["protected"], groups["adult"]["count"])

    def test_unmapped_zone_falls_through_to_native_interpretation(self):
        projected = self.app.analyze(self.project_query)
        hill = next(d for d in projected["daily"] if d["zone"] == "hill" and d["day"] == "2026-08-01")
        self.assertTrue(hill["usable"])
        self.assertEqual(hill["population"]["population"], 100)
        self.assertNotIn("share", canonical(hill["population"]))

    def test_native_mode_keeps_original_district_attribution(self):
        native = self.app.analyze({"metric": "heat", "zones": ["river", "hill"],
                                   "start": "2026-07-01", "end": "2026-07-04",
                                   "minimum_coverage": "0.8"})
        self.assertEqual(native["boundary"]["mode"], "native")
        self.assertEqual(native["status"], "qualified")
        self.assertEqual(native["total"]["known_exposed_person_days"], 740)

    def test_population_source_gap_is_reported_not_silently_zeroed(self):
        # 让西片区老年份额指向一个原区人口未覆盖的日期，投影分母必须报缺口
        senior = copy.deepcopy(self.app.ledger.snapshot()["population"]["river-senior"])
        senior.update(revision=2, end="2026-08-02")
        self.ingest({"kind": "population", "data": senior}, name="pop-gap")
        day = self.app.trace("west", mode="project", day="2026-08-03")["denominator"]
        self.assertFalse(day["complete"])
        self.assertIn("senior", day["missing_groups"])
        self.assertTrue(day["missing_sources"])
        projected = self.app.analyze({**self.project_query, "zones": ["west"],
                                      "start": "2026-08-03", "end": "2026-08-03"})
        self.assertEqual(projected["daily"][0]["reason"], ["population_share_gap"])

    def test_mode_misuse_is_rejected(self):
        self.assert_error("boundary", lambda: self.app.analyze(
            {**self.project_query, "zones": ["river"]}))
        self.assert_error("boundary", lambda: self.app.analyze(
            {"metric": "heat", "zones": ["east"], "start": "2026-08-01", "end": "2026-08-04",
             "minimum_coverage": "0.8"}))

    def test_projected_rows_carry_original_source_identity(self):
        projected = self.app.analyze(self.project_query)
        west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == "2026-08-03")
        evidence = west["weather"]["evidence"][0]
        self.assertEqual(evidence["source_zone"], "river")
        self.assertEqual(evidence["coverage"], "cover-west")
        source_zones = {s["source_zone"] for g in west["population"]["groups"] for s in g["sources"]}
        self.assertEqual(source_zones, {"river"})
        action = west["actions"][0]
        self.assertEqual(action["source_zone"], "river")
        self.assertEqual(action["allocation"], "alloc-west")


class BoundaryValidationTests(BoundaryTestBase):
    def test_share_conservation_violation_rejects_whole_batch(self):
        row = self.record("id", "share-adult-west")
        row["data"].update(revision=2, share="0.3")
        before = self.app.inspect()["watermark"]
        self.assert_error("share_conservation", lambda: self.ingest({"kind": "population_share", "data": row["data"]}))
        self.assertEqual(self.app.inspect()["watermark"], before)

    def test_fractional_headcount_projection_is_rejected(self):
        # 80 * 0.33 不是整数，禁止制造分数居民
        row = self.record("id", "share-adult-east")
        row["data"].update(revision=2, share="0.67")
        other = self.record("id", "share-adult-west")
        other["data"].update(revision=2, share="0.33")
        self.assert_error("share_integral",
                          lambda: self.ingest({"kind": "population_share", "data": row["data"]},
                                              {"kind": "population_share", "data": other["data"]}))

    def test_station_coverage_weight_cannot_exceed_one_across_targets(self):
        row = self.record("id", "cover-west")
        row["data"].update(revision=2, weight="0.9")
        extra = copy.deepcopy(row["data"])
        extra.update(id="cover-west-2", revision=1, target_zone="east")
        self.assert_error("coverage_weight",
                          lambda: self.ingest({"kind": "station_coverage", "data": row["data"]},
                                              {"kind": "station_coverage", "data": extra}))

    def test_station_coverage_cannot_invent_metrics_or_outlive_station(self):
        row = self.record("id", "cover-west")
        row["data"].update(revision=2, metrics=["heat", "water"], end="2026-12-31")
        self.assert_error("coverage_scope",
                          lambda: self.ingest({"kind": "station_coverage", "data": row["data"]}))
        row = self.record("id", "cover-west")
        row["data"].update(revision=2, metrics=["heat", "unknown-metric"])
        self.assert_error("reference",
                          lambda: self.ingest({"kind": "station_coverage", "data": row["data"]}))

    def test_facility_capacity_cannot_be_duplicated_across_targets(self):
        row = self.record("id", "alloc-west")
        row["data"].update(revision=2, capacity=21)  # 东10 + 西21 > 设施原容量30
        self.assert_error("allocation_capacity",
                          lambda: self.ingest({"kind": "facility_allocation", "data": row["data"]}))

    def test_allocation_requires_an_active_action_at_the_facility(self):
        # 2026-08-01 图书馆只有非热指标的维护行动，热指标分配缺少生效行动
        row = self.record("id", "alloc-west")
        row["data"].update(revision=2, start="2026-08-01", end="2026-08-01", capacity=1)
        self.assert_error("allocation_scope",
                          lambda: self.ingest({"kind": "facility_allocation", "data": row["data"]}))

    def test_target_zone_cannot_hold_native_facts(self):
        station = {"kind": "station", "data": {"id": "east-s", "revision": 1, "zone": "east",
                                               "start": "2026-08-01", "end": "2026-08-04",
                                               "weight": "1", "metrics": ["heat"]}}
        self.assert_error("target_native_conflict", lambda: self.ingest(station))

    def test_overlapping_boundary_relationships_rejected(self):
        row = self.record("id", "share-adult-east")
        row["data"].update(id="share-duplicate", revision=1, end="2026-12-31")
        self.assert_error("boundary_overlap",
                          lambda: self.ingest({"kind": "population_share", "data": row["data"]}))

    def test_conflicting_batch_is_atomic_and_audit_still_verified(self):
        bad = self.record("id", "share-adult-west")
        bad["data"].update(revision=2, share="0.9")
        good_zone = {"kind": "zone", "data": {"id": "extra", "revision": 1, "name": "临时新区"}}
        self.assert_error("share_conservation",
                          lambda: self.ingest({"kind": "population_share", "data": bad["data"]}, good_zone))
        self.assertNotIn("extra", self.app.ledger.snapshot()["zone"])
        self.assertEqual(self.app.audit()["status"], "verified")


class BoundaryStudyTests(BoundaryTestBase):
    def test_study_forms_comparable_cohort_under_one_boundary(self):
        result = self.app.compare(example("boundary_study.json"))
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["boundary"], "project")
        self.assertIsNotNone(result["estimate"])
        for phase in result["phases"].values():
            for pair in phase["pairs"]:
                self.assertIn(pair["control"], ("hill", "west"))

    def test_cross_boundary_protection_can_disqualify_a_control(self):
        # 给 hill（对照区）添加一个生效行动，它在投影口径下仍被识别为已干预
        action = {"kind": "action", "data": {"id": "hill-cool", "revision": 1, "name": "山麓降温",
                                             "zone": "hill", "metric": "heat",
                                             "start": "2026-08-01", "end": "2026-08-04", "status": "active",
                                             "capacity": 10, "groups": ["adult"], "priority": 5,
                                             "facility": "hill-hall"}}
        self.ingest(action, name="hill-action")
        result = self.app.compare(example("boundary_study.json"))
        contaminated = [r for r in result["excluded"] if r["reason"] == "control_contaminated"]
        self.assertTrue(contaminated)

    def test_renaming_zones_without_reinterpreting_is_not_allowed(self):
        # 在 native 口径直接查询目标区会被拒绝，不能只给研究结果换区名
        study = {**example("boundary_study.json"), "boundary": {"mode": "native"}}
        self.assert_error("boundary", lambda: self.app.compare(study))

    def test_non_projectable_days_are_excluded_not_counted_as_safe(self):
        # east 全程缺站点覆盖，作为处理区时所有区日都应被排除
        study = {**example("boundary_study.json"), "treated": ["east"], "minimum_pairs": 4}
        result = self.app.compare(study)
        self.assertEqual(result["status"], "insufficient")
        east_excluded = [r for r in result["excluded"]
                         if r["zone"] == "east" and "station_coverage_missing" in r["reason"]]
        self.assertTrue(east_excluded)


class BoundaryTraceAndFormatTests(BoundaryTestBase):
    def test_trace_reports_native_attribution_and_provenance(self):
        native = self.app.trace("river", mode="native", day="2026-07-01")
        self.assertTrue(native["native_attribution"])
        self.assertTrue(any(s["id"] == "river-s" for s in native["stations"]))
        projected = self.app.trace("west", mode="project", metric="heat", day="2026-08-03")
        self.assertFalse(projected["native_attribution"])
        kinds = projected["boundary_mappings"]
        self.assertTrue(kinds["population_share"] and kinds["station_coverage"] and kinds["facility_allocation"])

    def test_csv_roundtrip_includes_boundary_kinds(self):
        directory = Path(self.temp.name) / "csv"
        exported = self.app.export_csv(directory)
        kinds = {item["kind"] for item in exported["files"]}
        self.assertIn("population_share", kinds)
        self.assertIn("station_coverage", kinds)
        self.assertIn("facility_allocation", kinds)
        for kind in ("population_share", "station_coverage", "facility_allocation"):
            path = Path(next(item["path"] for item in exported["files"] if item["kind"] == kind))
            content = path.read_text(encoding="utf-8")
            decoded = decode_csv(content, kind)
            rerendered = encode_csv([item["data"] for item in decoded], kind)
            self.assertEqual(rerendered, content)
        with Application(Path(self.temp.name) / "restored.sqlite3") as restored:
            restored.ingest(example("city.json"))
            files = {item["kind"]: item["path"] for item in exported["files"]}
            restored.ingest_csv("csv-boundary-restore", self.boundary_bundle["source"], files)
            self.assertEqual(restored.analyze(self.project_query)["total"],
                             self.app.analyze(self.project_query)["total"])

    def test_cli_trace_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--database", str(Path(self.temp.name) / "city.sqlite3"),
                           "trace", "west", "--mode", "project", "--metric", "heat", "--day", "2026-08-01"])
        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "project")
        self.assertEqual(payload["denominator"]["population"], 35)


if __name__ == "__main__":
    unittest.main()
