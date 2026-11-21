from decimal import Decimal

from ..boundaries import BoundaryMaps, view_components
from ..contracts import days, decimal_text, query_spec, ratio, require


class Exposure:
    def __init__(self, snapshot, *, boundary="native"):
        self.snapshot = snapshot
        self.boundary = boundary
        self.maps = BoundaryMaps(snapshot) if boundary == "project" else None
        native = view_components(snapshot, "native")
        projected = view_components(snapshot, "project") if boundary == "project" else None
        self.native_readings, self.native_residents, self.native_adaptation = (
            native["readings"], native["residents"], native["adaptation"])
        self.projected = projected
        self.readings, self.residents, self.adaptation = (
            native["readings"], native["residents"], native["adaptation"])

    def is_target(self, zone):
        return self.maps is not None and zone in self.maps.targets()

    def components_for(self, zone):
        if self.is_target(zone):
            return (self.projected["readings"], self.projected["residents"],
                    self.projected["adaptation"])
        return self.native_readings, self.native_residents, self.native_adaptation

    def _check_zones(self, zones):
        if self.maps is None:
            for zone in zones:
                require(zone not in BoundaryMaps(self.snapshot).targets(), "boundary",
                        "目标区只存在于投影口径，请显式指定boundary.mode=project", zone=zone)
            return
        sources = self.maps.source_zones()
        for zone in zones:
            require(zone not in sources, "boundary",
                    "投影口径不能直接查询原区，请查询其目标区或改用native口径", zone=zone)

    def daily(self, zone, metric, when, minimum_coverage):
        readings, residents, adaptation = self.components_for(zone)
        weather = readings.zone_day(zone, metric, when, minimum_coverage)
        population = residents.day(zone, when)
        usable = weather["usable"] and population["complete"]
        result = {"zone": zone, "day": when, "usable": usable, "boundary": self.boundary,
                  "weather": weather, "population": population, "hazardous": weather["hazardous"],
                  "protected": None, "exposed": None, "weighted_burden": None,
                  "groups": [], "actions": [], "reason": []}
        if not weather["usable"]:
            result["reason"].append(weather.get("gap") or "observation_gap")
        if not population["complete"]:
            result["reason"].append(population.get("gap") or "population_gap")
        if not usable:
            return result
        allocation = adaptation.allocate(zone, metric, when, hazardous=weather["hazardous"])
        burden = residents.burden(zone, when, weather["severity"], allocation["protected"])
        if not weather["hazardous"]:
            burden["exposed"] = 0
            for group in burden["groups"]:
                group["exposed"] = 0
        result.update(protected=sum(allocation["protected"].values()), exposed=burden["exposed"],
                      weighted_burden=burden["weighted_burden"], groups=burden["groups"],
                      actions=allocation["actions"])
        return result

    def analyze(self, spec):
        spec = query_spec(spec)
        if spec["boundary"] != self.boundary:
            return Exposure(self.snapshot, boundary=spec["boundary"]).analyze(spec)
        self.native_readings.catalog.metric(spec["metric"])
        require(set(spec["zones"]).issubset(self.snapshot["zone"]), "zone", "查询含未知行政区")
        self._check_zones(spec["zones"])
        daily = [self.daily(zone, spec["metric"], when, spec["minimum_coverage"])
                 for zone in spec["zones"] for when in days(spec["start"], spec["end"])]
        strata = [self.summarize(zone, [row for row in daily if row["zone"] == zone]) for zone in spec["zones"]]
        qualified = all(row["complete"] for row in strata)
        all_known = self.summarize("all", daily)
        return {"query": spec, "status": "qualified" if qualified else "incomplete", "strata": strata,
                "total": all_known, "daily": daily, "episodes": self.episodes(daily),
                "boundary": self.boundary_summary()}

    @staticmethod
    def summarize(zone, daily):
        valid = [row for row in daily if row["usable"]]
        hazard = [row for row in valid if row["hazardous"]]
        population_days = sum(row["population"]["population"] for row in valid)
        hazard_population = sum(row["population"]["population"] for row in hazard)
        protected = sum(row["protected"] for row in hazard)
        exposed = sum(row["exposed"] for row in hazard)
        burden = sum((Decimal(row["weighted_burden"]) for row in valid), Decimal(0))
        complete = len(valid) == len(daily)
        return {"zone": zone, "complete": complete, "expected_zone_days": len(daily),
                "usable_zone_days": len(valid), "unknown_zone_days": len(daily) - len(valid),
                "known_population_days": population_days, "known_hazard_population_days": hazard_population,
                "known_protected_person_days": protected, "known_exposed_person_days": exposed,
                "known_weighted_burden": decimal_text(burden),
                "hazard_fraction": ratio(hazard_population, population_days) if complete else None,
                "protection_fraction": ratio(protected, hazard_population) if complete else None,
                "coverage_fraction": ratio(len(valid), len(daily)),
                "groups": Exposure.group_summary(valid, complete)}

    @staticmethod
    def group_summary(daily, complete):
        groups = {}
        for row in daily:
            for group in row["groups"]:
                target = groups.setdefault(group["group"], {"population_days": 0, "hazard_population_days": 0,
                                                            "protected_person_days": 0, "weighted_burden": Decimal(0)})
                target["population_days"] += group["count"]
                if row["hazardous"]:
                    target["hazard_population_days"] += group["count"]
                    target["protected_person_days"] += group["protected"]
                target["weighted_burden"] += Decimal(group["weighted_burden"])
        return [{"group": key, **value, "weighted_burden": decimal_text(value["weighted_burden"]),
                 "protection_fraction": ratio(value["protected_person_days"], value["hazard_population_days"]) if complete else None}
                for key, value in sorted(groups.items())]

    @staticmethod
    def episodes(daily):
        result = []
        for zone in sorted({row["zone"] for row in daily}):
            current = None
            for row in sorted((r for r in daily if r["zone"] == zone), key=lambda r: r["day"]):
                if not row["usable"] or not row["hazardous"]:
                    if current:
                        current["right_boundary"] = "unknown" if not row["usable"] else "observed"
                        result.append(current)
                        current = None
                    continue
                if current is None:
                    current = {"zone": zone, "start": row["day"], "end": row["day"], "days": 0,
                               "exposed_person_days": 0, "right_boundary": "window"}
                current["end"] = row["day"]
                current["days"] += 1
                current["exposed_person_days"] += row["exposed"]
            if current:
                result.append(current)
        return result

    def boundary_summary(self):
        if self.boundary == "native":
            return {"mode": "native", "interpretation": "按资料登记时的原区划解释，未应用边界修订"}
        revisions = sorted({row["revision"] for table in ("population_share", "station_coverage",
                                                           "facility_allocation")
                            for row in self.maps.snapshot[table].values()})
        identities = {"population_share": len(self.maps.shares), "station_coverage": len(self.maps.coverage),
                      "facility_allocation": len(self.maps.allocations),
                      "target_zones": sorted(self.maps.targets())}
        return {"mode": "project", **identities, "revisions": revisions,
                "interpretation": "按选定边界重新解释历史资料；映射缺口是不可投影，不是零暴露，且不改变原始来源身份"}

    def priorities(self, spec):
        analysis = self.analyze(spec)
        rows = []
        deferred = []
        for stratum in analysis["strata"]:
            zone = stratum["zone"]
            if not stratum["complete"]:
                deferred.append({"zone": zone, "reason": "资料不完整，不能将未知负担排为零风险",
                                 "unknown_zone_days": stratum["unknown_zone_days"]})
                continue
            population_days = stratum["known_population_days"]
            burden = Decimal(stratum["known_weighted_burden"])
            if not population_days:
                deferred.append({"zone": zone, "reason": "没有有效人口分母", "unknown_zone_days": 0})
                continue
            rate = burden / population_days
            uncovered = stratum["known_exposed_person_days"]
            hazard = stratum["known_hazard_population_days"]
            rows.append({"zone": zone, "weighted_burden": decimal_text(burden),
                         "burden_per_person_day": decimal_text(rate), "uncovered_person_days": uncovered,
                         "uncovered_fraction": ratio(uncovered, hazard),
                         "hazard_population_days": hazard, "groups": stratum["groups"]})
        rows.sort(key=lambda row: (-Decimal(row["burden_per_person_day"]),
                                   -Decimal(row["weighted_burden"]), row["zone"]))
        rank = 0
        previous = None
        for index, row in enumerate(rows, 1):
            key = (row["burden_per_person_day"], row["weighted_burden"])
            if key != previous:
                rank = index
            row["rank"] = rank
            previous = key
        return {"query": analysis["query"], "priorities": rows, "deferred": deferred,
                "interpretation": "按单位人口日的未保护加权负担比较，不能替代设施可行性或因果评估"}
