"""行政边界按日期拆合的资料契约与投影解释。

边界资料只改变历史资料的解释方式：人口份额、站点覆盖权重和设施服务容量
各自独立声明，不允许用同一个比例把热读数、人口和容量一起分摊。无法倒推的
观测在投影口径下是缺口，而不是凭份额制造出来的气象条件。
"""
from decimal import Decimal

from .contracts import (contains, days, decimal_text, distinct_strings, identifier,
                        integer, number, overlaps, period, require, shape)
from .residents import Residents, compute_burden


def normalize_population_share(record):
    shape(record, ("id", "revision", "source_zone", "target_zone", "group", "start", "end", "share"))
    start, end = period(record["start"], record["end"])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "source_zone": identifier(record["source_zone"], "source_zone"),
            "target_zone": identifier(record["target_zone"], "target_zone"),
            "group": identifier(record["group"], "group"), "start": start, "end": end,
            "share": str(number(record["share"], "share", "0.000001", 1))}


def normalize_station_coverage(record):
    shape(record, ("id", "revision", "station", "target_zone", "start", "end", "weight", "metrics"))
    start, end = period(record["start"], record["end"])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "station": identifier(record["station"], "station"),
            "target_zone": identifier(record["target_zone"], "target_zone"),
            "start": start, "end": end,
            "weight": str(number(record["weight"], "weight", "0.000001", 1)),
            "metrics": distinct_strings(record["metrics"], "metrics")}


def normalize_facility_allocation(record):
    shape(record, ("id", "revision", "facility", "target_zone", "start", "end", "capacity", "groups"))
    start, end = period(record["start"], record["end"])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "facility": identifier(record["facility"], "facility"),
            "target_zone": identifier(record["target_zone"], "target_zone"),
            "start": start, "end": end,
            "capacity": integer(record["capacity"], "capacity", 1, 1_000_000),
            "groups": distinct_strings(record["groups"], "groups")}


class BoundaryMaps:
    """快照内全部边界映射的只读索引与一致性校验。"""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.zones = snapshot["zone"]
        self.stations = snapshot["station"]
        self.actions = snapshot["action"]
        self.population = snapshot["population"]
        self.shares = snapshot["population_share"]
        self.coverage = snapshot["station_coverage"]
        self.allocations = snapshot["facility_allocation"]

    def validate(self):
        self._validate_references()
        self._validate_no_overlaps()
        self._validate_zone_roles()
        self._validate_shares()
        self._validate_coverage()
        self._validate_allocations()

    def targets(self):
        return {row["target_zone"] for row in list(self.shares.values())
                + list(self.coverage.values()) + list(self.allocations.values())}

    def source_zones(self):
        return {row["source_zone"] for row in self.shares.values()}

    def target_groups(self, target):
        return sorted({row["group"] for row in self.shares.values() if row["target_zone"] == target})

    def active_shares(self, target, when):
        return [row for row in self.shares.values()
                if row["target_zone"] == target and contains(row, when)]

    def active_coverage(self, target, metric, when):
        return [row for row in self.coverage.values()
                if row["target_zone"] == target and metric in row["metrics"] and contains(row, when)]

    def active_allocations(self, target, when):
        return [row for row in self.allocations.values()
                if row["target_zone"] == target and contains(row, when)]

    def _validate_references(self):
        for row in self.shares.values():
            require(row["source_zone"] in self.zones and row["target_zone"] in self.zones,
                    "reference", "人口份额引用了未知行政区", identity=row["id"])
            require(row["source_zone"] != row["target_zone"], "boundary",
                    "人口份额不能将区划映射到自身", identity=row["id"])
        for row in self.coverage.values():
            station = self.stations.get(row["station"])
            require(station is not None, "reference", "站点覆盖引用了未知站点", identity=row["id"])
            require(row["target_zone"] in self.zones, "reference", "站点覆盖目标区不存在", identity=row["id"])
            require(set(row["metrics"]).issubset(station["metrics"]), "reference",
                    "站点覆盖指标超出站点支持范围", identity=row["id"])
        facility_actions = {}
        for action in self.actions.values():
            facility_actions.setdefault(action["facility"], []).append(action)
        for row in self.allocations.values():
            require(row["target_zone"] in self.zones, "reference", "设施分配目标区不存在", identity=row["id"])
            owners = facility_actions.get(row["facility"])
            require(owners, "reference", "设施分配需要至少一个使用该设施的行动", identity=row["id"])
            known_groups = {group for action in owners for group in action["groups"]}
            require(set(row["groups"]).issubset(known_groups), "reference",
                    "设施分配人群不在设施行动的服务人群内", identity=row["id"])

    def _validate_no_overlaps(self):
        buckets = [("population_share", self.shares, lambda r: (r["source_zone"], r["target_zone"], r["group"])),
                   ("station_coverage", self.coverage, lambda r: (r["station"], r["target_zone"])),
                   ("facility_allocation", self.allocations, lambda r: (r["facility"], r["target_zone"]))]
        for kind, rows, key in buckets:
            grouped = {}
            for row in rows.values():
                others = grouped.setdefault(key(row), [])
                require(not any(overlaps(row, other) for other in others), "boundary_overlap",
                        "同一边界关系的有效期不能重叠", kind=kind, identity=row["id"])
                others.append(row)

    def _validate_zone_roles(self):
        native_zones = ({row["zone"] for row in self.stations.values()}
                        | {row["zone"] for row in self.population.values()}
                        | {row["zone"] for row in self.actions.values()})
        targets = self.targets()
        conflict = native_zones & targets
        require(not conflict, "target_native_conflict",
                "目标区不能同时持有原区站点、人口或行动资料", zones=sorted(conflict))

    @staticmethod
    def _span(rows):
        return min(row["start"] for row in rows), max(row["end"] for row in rows)

    def _population_row(self, source, group, when):
        candidates = [row for row in self.population.values()
                      if row["zone"] == source and row["group"] == group and contains(row, when)]
        return candidates[0] if candidates else None

    def _validate_shares(self):
        grouped = {}
        for row in self.shares.values():
            grouped.setdefault((row["source_zone"], row["group"]), []).append(row)
        vulnerability = {}
        for (source, group), rows in grouped.items():
            start, end = self._span(rows)
            for when in days(start, end):
                active = [row for row in rows if contains(row, when)]
                if not active:
                    continue
                total = sum((Decimal(row["share"]) for row in active), Decimal(0))
                require(total == Decimal(1), "share_conservation",
                        "同一生效范围各原区人口份额必须守恒为1", source_zone=source, group=group, day=when)
                population = self._population_row(source, group, when)
                if population is None:
                    continue
                for row in active:
                    projected = Decimal(population["count"]) * Decimal(row["share"])
                    require(projected == projected.to_integral_value(), "share_integral",
                            "人口份额投影必须保持整数人数，不能制造分数居民",
                            source_zone=source, group=group, day=when, identity=row["id"])
                    key = (row["target_zone"], group)
                    vulnerability.setdefault(key, set()).add(population["vulnerability"])
        for (target, group), values in vulnerability.items():
            require(len(values) == 1, "vulnerability_conflict",
                    "同一目标区群体的脆弱度来源不一致", target_zone=target, group=group)

    def _validate_coverage(self):
        grouped = {}
        for row in self.coverage.values():
            grouped.setdefault(row["station"], []).append(row)
        for station_id, rows in grouped.items():
            station = self.stations[station_id]
            for row in rows:
                require(station["start"] <= row["start"] and row["end"] <= station["end"],
                        "coverage_scope", "站点覆盖有效期超出站点本身有效期", identity=row["id"])
            start, end = self._span(rows)
            for when in days(start, end):
                active = [row for row in rows if contains(row, when)]
                total = sum((Decimal(row["weight"]) for row in active), Decimal(0))
                require(total <= Decimal(1), "coverage_weight",
                        "同一站点跨目标区的覆盖权重总和不能超过1", station=station_id, day=when)

    def _validate_allocations(self):
        grouped = {}
        for row in self.allocations.values():
            grouped.setdefault(row["facility"], []).append(row)
        for facility, rows in grouped.items():
            owners = [action for action in self.actions.values() if action["facility"] == facility]
            start, end = self._span(rows)
            for when in days(start, end):
                active = [row for row in rows if contains(row, when)]
                if not active:
                    continue
                for row in active:
                    serving = [action for action in owners
                               if action["status"] == "active" and contains(action, when)
                               and set(row["groups"]).issubset(set(action["groups"]))]
                    require(bool(serving), "allocation_scope",
                            "设施分配日期缺少同人群的生效行动提供服务", facility=facility, day=when)
                serving = [action for action in owners
                           if action["status"] == "active" and contains(action, when)]
                ceiling = max(action["capacity"] for action in serving)
                committed = sum(row["capacity"] for row in active)
                require(committed <= ceiling, "allocation_capacity",
                        "跨目标区服务容量总和不能超过设施原服务人数",
                        facility=facility, day=when, committed=committed, ceiling=ceiling)


class ProjectedResidents:
    """目标区口径下的人口分母：份额只重新解释人数，不改变来源身份。"""

    def __init__(self, snapshot, maps):
        self.maps = maps
        self.native = Residents(snapshot)

    def day(self, target, when):
        registered = self.maps.target_groups(target)
        shares = self.maps.active_shares(target, when)
        if not shares:
            return {"zone": target, "day": when, "complete": False, "missing_groups": registered,
                    "population": 0, "groups": [], "provenance": [], "gap": "population_share_missing"}
        accumulated = {}
        provenance = []
        missing_sources = []
        for row in sorted(shares, key=lambda item: (item["group"], item["source_zone"], item["id"])):
            population = self.maps._population_row(row["source_zone"], row["group"], when)
            if population is None:
                missing_sources.append({"source_zone": row["source_zone"], "group": row["group"],
                                        "share": row["id"]})
                continue
            count = int(Decimal(population["count"]) * Decimal(row["share"]))
            entry = accumulated.setdefault(row["group"], {"group": row["group"], "count": 0,
                                                           "vulnerability": population["vulnerability"],
                                                           "sources": []})
            entry["count"] += count
            entry["sources"].append({"source_zone": row["source_zone"], "source_population": population["id"],
                                     "population_revision": population["revision"], "share": row["id"],
                                     "share_revision": row["revision"], "count": count})
            provenance.append({"source_zone": row["source_zone"], "group": row["group"],
                               "source_population": population["id"], "share": row["id"], "count": count})
        present = set(accumulated)
        missing_groups = sorted(set(registered) - present)
        complete = not missing_groups and not missing_sources
        groups = []
        for group in sorted(accumulated):
            entry = accumulated[group]
            primary = entry["sources"][0]
            groups.append({"group": group, "count": entry["count"], "vulnerability": entry["vulnerability"],
                           "population": primary["share"], "revision": primary["share_revision"],
                           "sources": entry["sources"]})
        return {"zone": target, "day": when, "complete": complete,
                "missing_groups": missing_groups + sorted({item["group"] for item in missing_sources
                                                            if item["group"] not in missing_groups}),
                "population": sum(entry["count"] for entry in accumulated.values()),
                "groups": groups, "provenance": provenance,
                "missing_sources": missing_sources,
                "gap": None if complete else "population_share_gap"}

    def burden(self, target, when, severity, protected):
        population = self.day(target, when)
        require(population["complete"], "population_gap", "投影人口分母不完整，不能形成暴露估计")
        return compute_burden(population["groups"], severity, protected)

    def period_coverage(self, target, start, end):
        daily = [self.day(target, when) for when in days(start, end)]
        return {"zone": target, "complete_days": sum(row["complete"] for row in daily),
                "total_days": len(daily),
                "population_days": sum(row["population"] for row in daily if row["complete"]),
                "missing": [{"day": row["day"], "groups": row["missing_groups"]}
                            for row in daily if not row["complete"]]}


class ProjectedReadings:
    """目标区口径下的站点覆盖：只承认显式覆盖权重，不用人口比例替代。"""

    def __init__(self, snapshot, maps):
        self.maps = maps
        self.snapshot = snapshot
        from .observatory.readings import Readings, convert
        self.native = Readings(snapshot)
        self._convert = convert

    def zone_day(self, target, metric_id, when, minimum_coverage):
        metric = self.native.catalog.metric(metric_id)
        rows = self.maps.active_coverage(target, metric_id, when)
        evidence, issues = [], []
        valid_weight = Decimal(0)
        expected_weight = sum((Decimal(row["weight"]) for row in rows), Decimal(0))
        weighted = Decimal(0)
        if not rows:
            issues.append({"target_zone": target, "reason": "station_coverage_missing"})
        for coverage in sorted(rows, key=lambda item: (item["station"], item["id"])):
            station = self.maps.stations[coverage["station"]]
            key = ":".join((station["id"], metric_id, when))
            reading = self.snapshot["reading"].get(key)
            if reading is None or reading["quality"] != "valid":
                issues.append({"station": station["id"], "coverage": coverage["id"],
                               "reason": "absent" if reading is None else reading["quality"]})
                continue
            weight = Decimal(coverage["weight"])
            value = self._convert(reading["value"], reading["unit"], metric["dimension"])
            weighted += value * weight
            valid_weight += weight
            evidence.append({"reading": key, "revision": reading["revision"], "weight": str(weight),
                             "station": station["id"], "station_revision": station["revision"],
                             "coverage": coverage["id"], "coverage_revision": coverage["revision"],
                             "source_zone": station["zone"]})
        usable = valid_weight > 0 and valid_weight >= Decimal(str(minimum_coverage))
        value = weighted / valid_weight if usable else None
        if rows and valid_weight == 0:
            gap = "observation_gap"
        elif not rows:
            gap = "station_coverage_missing"
        else:
            gap = "observation_gap"
        return {"zone": target, "day": when, "metric": metric_id,
                "value": decimal_text(value), "coverage": decimal_text(valid_weight),
                "expected_weight": decimal_text(expected_weight), "usable": usable,
                "hazardous": self.native.catalog.hazardous(metric_id, value) if usable else None,
                "severity": decimal_text(self.native.catalog.severity(metric_id, value)) if usable else None,
                "evidence": evidence, "issues": issues, "metric_revision": metric["revision"],
                "gap": None if usable else gap}

    def diagnostics(self, target, metric, start, end):
        rows = [self.zone_day(target, metric, when, "1") for when in days(start, end)]
        return {"zone": target, "metric": metric, "days": len(rows),
                "complete": sum(row["usable"] for row in rows),
                "problems": [{"day": row["day"], "coverage": row["coverage"], "issues": row["issues"]}
                             for row in rows if not row["usable"]]}


class ProjectedAdaptation:
    """目标区口径下的行动去向：容量按设施分配落地，不跨区重复放大。"""

    def __init__(self, snapshot, maps):
        self.maps = maps
        self.snapshot = snapshot
        from .adaptation import Adaptation
        self.native = Adaptation(snapshot)

    def _serving(self, target, metric, when):
        result = []
        for allocation in self.maps.active_allocations(target, when):
            for action in self._active_facility_actions(allocation["facility"], metric, when):
                result.append((allocation, action))
        return result

    def _active_facility_actions(self, facility, metric, when):
        return [action for action in self.snapshot["action"].values()
                if action["facility"] == facility and action["metric"] == metric
                and action["status"] == "active" and contains(action, when)]

    def active(self, target, metric, when):
        return [{"action": action, "allocation": allocation}
                for allocation, action in self._serving(target, metric, when)]

    def is_treated(self, target, metric, when):
        return any(allocation["capacity"] > 0 for allocation, _ in self._serving(target, metric, when))

    def allocate(self, target, metric, when, *, hazardous):
        residents = ProjectedResidents(self.snapshot, self.maps)
        population = residents.day(target, when)
        groups = {row["group"]: row for row in population["groups"]}
        protected = {group: 0 for group in groups}
        if not hazardous or not population["complete"]:
            return {"complete": population["complete"], "protected": protected, "actions": [], "unused": 0}
        serving = sorted(self._serving(target, metric, when),
                         key=lambda pair: (-pair[1]["priority"], pair[1]["id"], pair[0]["id"]))
        evidence = []
        unused = 0
        for allocation, action in serving:
            remaining = allocation["capacity"]
            assignments = []
            eligible = sorted((groups[group] for group in action["groups"]
                               if group in groups and group in allocation["groups"]),
                              key=lambda row: (-Decimal(row["vulnerability"]), row["group"]))
            for group in eligible:
                count = min(remaining, group["count"] - protected[group["group"]])
                if count:
                    protected[group["group"]] += count
                    remaining -= count
                    assignments.append({"group": group["group"], "count": count})
            unused += remaining
            evidence.append({"action": action["id"], "revision": action["revision"],
                             "facility": action["facility"], "capacity": allocation["capacity"],
                             "used": allocation["capacity"] - remaining, "unused": remaining,
                             "allocation": allocation["id"], "allocation_revision": allocation["revision"],
                             "source_zone": action["zone"], "assignments": assignments})
        return {"complete": True, "protected": protected, "actions": evidence, "unused": unused}

    def utilization(self, target, metric, start, end, readings, coverage):
        actions = {}
        for when in days(start, end):
            weather = readings.zone_day(target, metric, when, coverage)
            if not weather["usable"]:
                continue
            allocation = self.allocate(target, metric, when, hazardous=weather["hazardous"])
            for item in allocation["actions"]:
                totals = actions.setdefault(item["action"], {"capacity_days": 0, "served_days": 0, "hazard_days": 0})
                totals["capacity_days"] += item["capacity"]
                totals["served_days"] += item["used"]
                totals["hazard_days"] += 1
        from .contracts import ratio
        return [{"action": key, **value, "utilization": ratio(value["served_days"], value["capacity_days"])}
                for key, value in sorted(actions.items())]


def view_components(snapshot, mode):
    """按口径返回同一接口的读数、人口和行动组件。"""
    from .observatory.readings import Readings
    from .adaptation import Adaptation
    if mode == "native":
        return {"mode": mode, "maps": None,
                "readings": Readings(snapshot), "residents": Residents(snapshot),
                "adaptation": Adaptation(snapshot)}
    maps = BoundaryMaps(snapshot)
    return {"mode": mode, "maps": maps, "readings": ProjectedReadings(snapshot, maps),
            "residents": ProjectedResidents(snapshot, maps),
            "adaptation": ProjectedAdaptation(snapshot, maps)}
