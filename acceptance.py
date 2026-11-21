import copy
import json
import tempfile
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError, canonical
from climate_ledger.publication import Publication


def run():
    examples = Path(__file__).parent / "examples"
    load = lambda name: json.loads((examples / name).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with Application(root / "city.sqlite3") as app:
            bundle = load("city.json")
            assert app.ingest(bundle)["seq"] == 1
            assert app.ingest(bundle)["replayed"]
            exposure = app.analyze(load("exposure.json"))
            assert exposure["total"]["known_exposed_person_days"] == 740
            study = app.compare(load("study.json"))
            assert study["estimate"] == "-2.500000"
            plan = app.plan(load("portfolio.json"))
            assert plan["selected"] == ["shared-river"]
            assert app.priorities(load("exposure.json"))["priorities"][0]["zone"] == "hill"
            draft = app.draft("july", "heat-july", load("exposure.json"), "分析员")
            app.review("july", draft["digest"], "审核员")
            release = app.publish("july", "july-v1")
            app.publication.export("july-v1", root / "july.json")
            assert Publication.verify_export(root / "july.json")["valid"]
            correction = copy.deepcopy(next(row for row in bundle["records"] if row["kind"] == "population"
                                             and row["data"]["id"] == "river-senior"))
            correction["data"].update(revision=2, count=40)
            app.ingest({"id": "population-corrected", "source": bundle["source"], "records": [correction]})
            assert app.analyze(load("exposure.json"), seq=1) == exposure
            assert app.publication.release("july-v1")["digest"] == release["digest"]
            assert not app.publication.release("july-v1")["current_evidence"]
            changed = app.reconcile(load("exposure.json"), 1, 2)
            assert len(changed["affected_zone_days"]) == 4
            assert app.audit()["status"] == "verified"
            app.backup(root / "backup.sqlite3")
        with Application(root / "backup.sqlite3") as restored:
            assert restored.audit()["batches"] == 2
            assert restored.publication.release("july-v1")["digest"] == release["digest"]

        # 边界拆合：第三个资料包引入东、西目标区与三类边界映射，整包原子入账
        with Application(root / "city.sqlite3") as app:
            app.ingest(load("boundary.json"))
            project = load("boundary_exposure.json")
            projected = app.analyze(project)
            # 份额示例（基于更正后的最新分母：成人80、老年40）：东片区成人60、老年10
            east = app.trace("east", mode="project", day="2026-08-01")["denominator"]
            east_groups = {g["group"]: g["count"] for g in east["groups"]}
            assert east_groups == {"adult": 60, "senior": 10}
            # 原始修订身份仍可在投影来源中追溯，份额不改变来源对象
            assert {s["source_population"] for g in east["groups"] for s in g["sources"]} == {
                "river-adult", "river-senior"}
            # 东片区缺站点覆盖：是不可投影缺口，不是零暴露，也不凭人口份额造气象
            east_days = [d for d in projected["daily"] if d["zone"] == "east"]
            assert all(not d["usable"] and d["reason"] == ["station_coverage_missing"] for d in east_days)
            assert all(d["weather"]["value"] is None for d in east_days)
            # 西片区可投影：人口与容量守恒，设施30名额拆为东10、西20且不重复放大
            west = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == "2026-08-03")
            assert west["protected"] == 20 and west["population"]["population"] == 50
            assert sum(a["capacity"] for a in west["actions"]) == 20
            # 合并目标区人口每日等于原区人口
            native = app.analyze({"metric": "heat", "zones": ["river"], "start": "2026-08-01",
                                  "end": "2026-08-04", "minimum_coverage": "0.8"})
            for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
                e = next(d for d in projected["daily"] if d["zone"] == "east" and d["day"] == day)
                w = next(d for d in projected["daily"] if d["zone"] == "west" and d["day"] == day)
                r = next(d for d in native["daily"] if d["day"] == day)
                assert e["population"]["population"] + w["population"]["population"] == r["population"]["population"]
            # 前后期研究在同一投影边界下成队，跨界保护改变对照资格，而不是只换区名
            study = app.compare(load("boundary_study.json"))
            assert study["status"] == "qualified" and study["boundary"] == "project"
            # 历史水位下目标区尚不存在，投影查询被拒绝；native 旧口径仍可回查
            try:
                app.analyze(project, seq=1)
                raise AssertionError("历史水位不应能投影尚不存在的目标区")
            except DomainError as exc:
                assert exc.code == "zone"
            assert app.analyze(load("exposure.json"), seq=1) == exposure
            # 溯源可查到原始归属、边界修订与分组分母
            trace = app.trace("west", mode="project", metric="heat", day="2026-08-03")
            assert {k for k in trace["boundary_mappings"]} == {
                "population_share", "station_coverage", "facility_allocation"}
            assert app.audit()["status"] == "verified"
        print(canonical({"status": "通过", "synthetic": True, "exposure": exposure["total"],
                         "study_estimate": study["estimate"], "portfolio": plan["selected"],
                         "corrected_zone_days": len(changed["affected_zone_days"]), "archive_verified": True,
                         "boundary": "project", "projected_targets": ["east", "west"]}))


if __name__ == "__main__":
    run()
