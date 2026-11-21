from .adaptation import Adaptation, normalize_action
from .analysis.exposure import Exposure
from .analysis.study import Study
from .boundaries import (BoundaryMaps, normalize_facility_allocation,
                         normalize_population_share, normalize_station_coverage, view_components)
from .contracts import KINDS, identifier, integer, require, shape, text
from .ledger import Ledger
from .observatory.catalog import Catalog, normalize_metric, normalize_station, normalize_zone
from .observatory.readings import Readings, normalize_reading
from .publication import Publication
from .residents import Residents, normalize_population


NORMALIZERS = {"zone": normalize_zone, "metric": normalize_metric, "station": normalize_station,
               "population": normalize_population, "action": normalize_action, "reading": normalize_reading,
               "population_share": normalize_population_share,
               "station_coverage": normalize_station_coverage,
               "facility_allocation": normalize_facility_allocation}


class Application:
    def __init__(self, path):
        self.ledger = Ledger(path)
        self.publication = Publication(self.ledger)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.ledger.close()

    @staticmethod
    def validate(snapshot):
        Catalog(snapshot).validate()
        Readings(snapshot).validate()
        Residents(snapshot).validate()
        Adaptation(snapshot).validate()
        BoundaryMaps(snapshot).validate()

    def ingest(self, bundle):
        shape(bundle, ("id", "source", "records"))
        identifier(bundle["id"], "batch.id")
        text(bundle["source"], "source")
        require(isinstance(bundle["records"], list) and 0 < len(bundle["records"]) <= 100000,
                "batch", "入账记录数量需在1至100000之间")
        normalized = []
        for item in bundle["records"]:
            shape(item, ("kind", "data"))
            require(item["kind"] in KINDS, "kind", "未知资料类型")
            normalized.append((item["kind"], NORMALIZERS[item["kind"]](item["data"])))
        with self.ledger.transaction(write=True):
            return self.ledger.append(bundle, normalized, self.validate)

    def analyze(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            analysis = Exposure(self.ledger.snapshot(watermark)).analyze(spec)
            return {"watermark": watermark, **analysis}

    def compare(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            analysis = Study(self.ledger.snapshot(watermark)).compare(spec)
            return {"watermark": watermark, **analysis}

    def draft(self, draft_id, scope, spec, author, *, kind="exposure"):
        require(kind in ("exposure", "study"), "kind", "草稿分析类型无效")
        with self.ledger.transaction(write=True):
            snapshot = self.ledger.snapshot()
            analysis = Exposure(snapshot).analyze(spec) if kind == "exposure" else Study(snapshot).compare(spec)
            return self.publication.create(draft_id, scope, analysis, author)

    def review(self, draft_id, digest, reviewer):
        with self.ledger.transaction(write=True):
            return self.publication.review(draft_id, digest, reviewer)

    def publish(self, draft_id, release_id, expected_head=None):
        with self.ledger.transaction(write=True):
            return self.publication.publish(draft_id, release_id, expected_head)

    def inspect(self):
        with self.ledger.transaction():
            snapshot = self.ledger.snapshot()
            self.validate(snapshot)
            return {**self.ledger.integrity(), "objects": {kind: len(rows) for kind, rows in snapshot.items()},
                    "batches": self.ledger.provenance(self.ledger.watermark()),
                    "releases": self.publication.list_releases()}

    def diagnose(self, spec):
        from .analysis.exposure import Exposure
        from .contracts import query_spec
        spec = query_spec(spec)
        with self.ledger.transaction():
            snapshot = self.ledger.snapshot()
            exposure = Exposure(snapshot, boundary=spec["boundary"])
            zones = []
            for zone in spec["zones"]:
                readings, residents, adaptation = exposure.components_for(zone)
                zones.append({"zone": zone,
                              "observations": readings.diagnostics(zone, spec["metric"], spec["start"], spec["end"]),
                              "residents": residents.period_coverage(zone, spec["start"], spec["end"]),
                              "actions": adaptation.utilization(zone, spec["metric"], spec["start"], spec["end"],
                                                                readings, spec["minimum_coverage"])})
            return {"watermark": self.ledger.watermark(), "boundary": spec["boundary"], "zones": zones}

    def history(self, kind, identity):
        with self.ledger.transaction():
            return self.ledger.history(kind, identity)

    def trace(self, zone, *, mode="native", metric=None, day=None):
        from .contracts import contains, day as parse_day
        with self.ledger.transaction():
            snapshot = self.ledger.snapshot()
            maps = BoundaryMaps(snapshot)
            require(mode in ("native", "project"), "boundary", "边界模式需要native或project")
            if day is not None:
                parse_day(day)
            if mode == "native":
                stations = [{"id": s["id"], "revision": s["revision"], "weight": s["weight"],
                             "metrics": s["metrics"], "start": s["start"], "end": s["end"]}
                            for s in snapshot["station"].values() if s["zone"] == zone
                            and (day is None or contains(s, day))]
                population = [{"id": p["id"], "revision": p["revision"], "group": p["group"],
                               "count": p["count"], "vulnerability": p["vulnerability"],
                               "start": p["start"], "end": p["end"]}
                              for p in snapshot["population"].values() if p["zone"] == zone
                              and (day is None or contains(p, day))]
                actions = [{"id": a["id"], "revision": a["revision"], "facility": a["facility"],
                            "metric": a["metric"], "capacity": a["capacity"], "status": a["status"],
                            "groups": a["groups"], "start": a["start"], "end": a["end"]}
                           for a in snapshot["action"].values() if a["zone"] == zone
                           and (day is None or contains(a, day))]
                return {"zone": zone, "mode": mode, "day": day, "native_attribution": True,
                        "stations": stations, "population": population, "actions": actions,
                        "boundary_mappings": {"population_share": [], "station_coverage": [],
                                              "facility_allocation": []}}
            shares = [r for r in snapshot["population_share"].values() if r["target_zone"] == zone
                      and (day is None or contains(r, day))]
            coverage = [r for r in snapshot["station_coverage"].values() if r["target_zone"] == zone
                        and (metric is None or metric in r["metrics"])
                        and (day is None or contains(r, day))]
            allocations = [r for r in snapshot["facility_allocation"].values() if r["target_zone"] == zone
                           and (day is None or contains(r, day))]
            residents = view_components(snapshot, "project")["residents"]
            denominator = residents.day(zone, day) if day is not None else None
            return {"zone": zone, "mode": mode, "day": day, "native_attribution": False,
                    "boundary_mappings": {"population_share": sorted(shares, key=lambda r: r["id"]),
                                          "station_coverage": sorted(coverage, key=lambda r: r["id"]),
                                          "facility_allocation": sorted(allocations, key=lambda r: r["id"])},
                    "denominator": denominator}

    def plan(self, spec, *, seq=None):
        from .analysis.portfolio import Portfolio
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            result = Portfolio(self.ledger.snapshot(watermark)).plan(spec)
            return {"watermark": watermark, **result}

    def reconcile(self, spec, before_seq, after_seq):
        from .analysis.reconciliation import Reconciliation
        with self.ledger.transaction():
            return Reconciliation(self.ledger).compare(spec, before_seq, after_seq)

    def ingest_csv(self, batch_id, source, files):
        from .formats import bundle_from_csv
        return self.ingest(bundle_from_csv(batch_id, source, files))

    def export_csv(self, directory, *, seq=None):
        from .formats import snapshot_csv
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, "files": snapshot_csv(self.ledger.snapshot(watermark), directory)}

    def equity(self, spec, *, seq=None):
        from .analysis.equity import Equity
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, **Equity(self.ledger.snapshot(watermark)).analyze(spec)}

    def audit(self):
        from .audit import Audit
        with self.ledger.transaction():
            return Audit(self.ledger).verify(self.validate)

    def backup(self, destination):
        from .audit import Audit
        return Audit(self.ledger).backup(destination)

    def priorities(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, **Exposure(self.ledger.snapshot(watermark)).priorities(spec)}
