"""Product/technology readers and evidence-bound graph projections. No inference."""

from __future__ import annotations

import json
from collections import defaultdict, deque

from api.research_database import snapshot
from searchos.knowledge_store.catalog import DIMENSIONS, ENTITY_KINDS

PRIMARY_KINDS = {"product", "technology"}
LINEAGE = {"variant_of", "evolves_from", "replaces", "subtechnology_of", "generation_of"}
TECHNICAL = {"technology", "route", "component", "material", "process", "interface", "reference"}
INVERSE = {
    "variant_of": (7, "7.models", "包含型号/版本"),
    "subtechnology_of": (4, "4.children", "下位技术"),
    "evolves_from": (2, "2.route_evolution", "后续演进"),
    "replaces": (2, "2.route_evolution", "被替代于"),
    "uses_technology": (7, "7.products", "采用该技术的产品"),
    "reference_for": (4, "4.support", "独立开源参考"),
    "part_of": (4, "4.components", "组成对象"),
    "integrates": (4, "4.support", "集成于"),
    "role:associated_organization": (7, "7.actors", "关联机构"),
    "role:supplier": (7, "7.actors", "供应方"),
    "role:user": (7, "7.users", "主要用户"),
    "role:researcher": (6, "6.directions", "研究主体"),
    "role:developer": (6, "6.teams", "研发主体"),
    "role:member": (6, "6.experts", "成员"),
    "role:affiliated": (6, "6.affiliation", "隶属主体"),
}


def _entities(db, slug):
    return {
        r["id"]: dict(r)
        for r in db.execute(
            "SELECT e.* FROM entities e JOIN topic_entities t ON t.entity_id=e.id "
            "WHERE t.topic_id=? AND e.state='active' ORDER BY e.canonical_name",
            ("topic:" + slug,),
        )
    }


def asset_summary(store):
    with store.connect() as db:
        result = {}
        for row in db.execute(
            "SELECT t.slug,e.kind,count(DISTINCT e.id) n FROM topics t "
            "JOIN topic_entities x ON x.topic_id=t.id JOIN entities e ON e.id=x.entity_id "
            "WHERE e.state='active' AND e.kind IN ('product','technology') GROUP BY t.slug,e.kind"
        ):
            result.setdefault(row["slug"], {"product": 0, "technology": 0, "sources": 0})[
                row["kind"]
            ] = row["n"]
        for row in db.execute(
            "SELECT t.slug,count(DISTINCT s.revision_id) n FROM topics t "
            "JOIN claims c ON c.topic_id=t.id JOIN claim_evidence v ON v.claim_id=c.id "
            "JOIN source_segments s ON s.id=v.segment_id WHERE c.status='accepted' GROUP BY t.slug"
        ):
            result.setdefault(row["slug"], {"product": 0, "technology": 0, "sources": 0})[
                "sources"
            ] = row["n"]
        return result


def _node(row):
    return {
        "id": row["id"],
        "name": row["canonical_name"],
        "kind": row["kind"],
        "kind_label": ENTITY_KINDS[row["kind"]][0],
        "namespace": row["namespace"],
        "can_open": row["kind"] in PRIMARY_KINDS,
    }


def _connections(db, slug):
    """Every edge comes from a published claim and an actual foreign key."""
    providers = {
        "entity_relations": "SELECT r.claim_id,r.object_id target_id,r.relation_type_id predicate,"
        "t.label,'' detail FROM entity_relations r "
        "JOIN relation_types t ON t.id=r.relation_type_id",
        "composition_items": "SELECT claim_id,component_id target_id,'has_component' predicate,"
        "'组成/载荷' label,coalesce(configuration,'') detail FROM composition_items",
        "participations": "SELECT p.claim_id,p.object_id target_id,'role:'||p.role_id predicate,"
        "r.label,'' detail FROM participations p JOIN participation_roles r ON r.id=p.role_id",
        "interface_connections": "SELECT claim_id,interface_id target_id,"
        "'exposes_interface' predicate,"
        "'接口' label,coalesce(protocol_version,'') detail FROM interface_connections",
        "route_steps": "SELECT claim_id,step_entity_id target_id,'route_step' predicate,"
        "'路线步骤' label,description detail FROM route_steps WHERE step_entity_id IS NOT NULL",
        "product_capabilities": "SELECT claim_id,capability_id target_id,"
        "'has_capability' predicate,"
        "'能力状态' label,support_status detail FROM product_capabilities",
        "metric_observations": "SELECT claim_id,metric_id target_id,'has_metric' predicate,"
        "'指标观测' label,raw_value||' '||coalesce(raw_unit,'') detail FROM metric_observations",
    }
    result = []
    for table, query in providers.items():
        for row in db.execute(
            "SELECT x.*,c.subject_id source_id,c.scope_note,c.recorded_at FROM ("
            + query
            + ") x JOIN claims c ON c.id=x.claim_id WHERE c.topic_id=? AND c.status='accepted'",
            ("topic:" + slug,),
        ):
            result.append({**dict(row), "table": table})
    return result


def _evidence(db, claim_ids):
    result = defaultdict(list)
    ids = sorted(set(claim_ids))
    if not ids:
        return result
    for row in db.execute(
        "SELECT e.claim_id,e.quote,e.stance,s.revision_id,d.title,d.canonical_uri url "
        "FROM claim_evidence e JOIN source_segments s ON s.id=e.segment_id "
        "JOIN source_revisions r ON r.id=s.revision_id "
        "JOIN source_documents d ON d.id=r.document_id WHERE e.claim_id IN ("
        + ",".join("?" for _ in ids)
        + ") ORDER BY e.id",
        ids,
    ):
        value = dict(row)
        result[value.pop("claim_id")].append(value)
    return result


def asset_index(store, slug="anduril", kind="product", query="", offset=0, limit=24):
    with store.connect() as db:
        entities = _entities(db, slug)
        stats = {
            r["subject_id"]: dict(r)
            for r in db.execute(
                "SELECT c.subject_id,count(DISTINCT CASE WHEN c.claim_kind<>'identity' "
                "THEN c.id END) field_count,count(DISTINCT s.revision_id) source_count,"
                "group_concat(DISTINCT s.revision_id) source_ids,"
                "max(c.recorded_at) updated_at FROM claims c "
                "LEFT JOIN claim_evidence v ON v.claim_id=c.id "
                "LEFT JOIN source_segments s ON s.id=v.segment_id "
                "WHERE c.topic_id=? AND c.status='accepted' GROUP BY c.subject_id",
                ("topic:" + slug,),
            )
        }
        summaries, searchable = {}, defaultdict(list)
        for r in db.execute(
            "SELECT c.subject_id,f.label,f.id,v.value_json FROM claims c "
            "JOIN field_values v ON v.claim_id=c.id JOIN field_definitions f ON f.id=v.field_id "
            "WHERE c.topic_id=? AND c.status='accepted' ORDER BY c.recorded_at DESC",
            ("topic:" + slug,),
        ):
            value = json.loads(r["value_json"])
            searchable[r["subject_id"]].append(str(value))
            if isinstance(value, str) and any(
                w in r["label"] for w in ("定位", "定义", "原理", "概述")
            ):
                summaries.setdefault(r["subject_id"], value)
        aliases = defaultdict(list)
        for r in db.execute("SELECT entity_id,name FROM entity_names"):
            aliases[r["entity_id"]].append(r["name"])
        links = defaultdict(set)
        incoming = defaultdict(set)
        connection_times = defaultdict(str)
        source_sets = {
            identity: set((s["source_ids"] or "").split(",")) - {""}
            for identity, s in stats.items()
        }
        claim_sources = defaultdict(set)
        for r in db.execute(
            "SELECT v.claim_id,s.revision_id FROM claim_evidence v "
            "JOIN source_segments s ON s.id=v.segment_id JOIN claims c ON c.id=v.claim_id "
            "WHERE c.topic_id=? AND c.status='accepted'",
            ("topic:" + slug,),
        ):
            claim_sources[r["claim_id"]].add(r["revision_id"])
        for r in _connections(db, slug):
            if r["source_id"] not in entities or r["target_id"] not in entities:
                continue
            signature = (r["source_id"], r["target_id"], r["predicate"])
            links[r["source_id"]].add(signature)
            links[r["target_id"]].add(signature)
            searchable[r["source_id"]].extend(
                [r["detail"], entities[r["target_id"]]["canonical_name"]]
            )
            source_sets.setdefault(r["target_id"], set()).update(claim_sources[r["claim_id"]])
            for entity_id in (r["source_id"], r["target_id"]):
                connection_times[entity_id] = max(connection_times[entity_id], r["recorded_at"])
            if r["source_id"] != r["target_id"] and r["predicate"] in INVERSE:
                incoming[r["target_id"]].add(r["claim_id"])
        counts = {k: sum(e["kind"] == k for e in entities.values()) for k in PRIMARY_KINDS}
        q = query.strip().casefold()
        items = []
        for row in entities.values():
            if row["kind"] != kind:
                continue
            haystack = [row["canonical_name"], *aliases[row["id"]], *searchable[row["id"]]]
            if q and not any(q in text.casefold() for text in haystack):
                continue
            stat = stats.get(row["id"], {})
            items.append(
                {
                    **_node(row),
                    "summary": summaries.get(row["id"], ""),
                    "field_count": stat.get("field_count", 0) + len(incoming[row["id"]]),
                    "source_count": len(source_sets.get(row["id"], set())),
                    "updated_at": max(stat.get("updated_at") or "", connection_times[row["id"]])
                    or None,
                    "relationship_count": len(links[row["id"]]),
                }
            )
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "counts": counts,
            "offset": offset,
            "limit": limit,
            "kind": kind,
        }


def _element(record, mapping):
    if record["field_id"] in mapping:
        return mapping[record["field_id"]]
    return {
        "identity": "7.products" if record["entity_kind"] == "product" else "1.identity",
        "classification": "1.classification",
        "metric": "5.values",
        "capability": "5.system",
        "event": "2.events",
        "assessment": "5.level",
        "maturity": "5.maturity",
        "composition": "4.components",
        "interface": "4.interfaces",
        "route_step": "3.composition",
    }.get(
        record["kind"],
        {2: "2.relations", 3: "3.relations", 4: "4.support", 6: "6.affiliation", 7: "7.actors"}.get(
            record["dimension_id"], "1.definition"
        ),
    )


def asset_detail(store, slug, entity_id):
    with store.connect() as db:
        entities = _entities(db, slug)
        if entity_id not in entities:
            raise KeyError("对象不属于当前专题")
        root = entities[entity_id]
        definitions = [
            dict(r)
            for r in db.execute(
                "SELECT f.id,f.label,f.value_type,f.repeat_group,m.element_id,d.dimension_id "
                "FROM field_definitions f JOIN field_storage_mappings m ON m.field_id=f.id "
                "JOIN dimension_elements d ON d.id=m.element_id "
                "WHERE f.subject_kind=? AND f.is_derived=0 ORDER BY f.id",
                (root["kind"],),
            )
        ]
        mapping = {r["id"]: r["element_id"] for r in definitions}
        elements = [
            dict(r)
            for r in db.execute("SELECT * FROM dimension_elements ORDER BY dimension_id,ordinal")
        ]
        connections = [
            r
            for r in _connections(db, slug)
            if entity_id in (r["source_id"], r["target_id"])
            and r["source_id"] in entities
            and r["target_id"] in entities
        ]
        evidence = _evidence(db, [r["claim_id"] for r in connections])
        event_dates = defaultdict(list)
        for event in db.execute(
            "SELECT c.subject_id,t.event_type,d.occurred_at FROM claims c "
            "JOIN event_details d ON d.claim_id=c.id "
            "JOIN technology_events t ON t.entity_id=c.subject_id "
            "WHERE c.topic_id=? AND c.status='accepted'",
            ("topic:" + slug,),
        ):
            event_dates[event["subject_id"]].append(dict(event))
    view = snapshot(
        store, slug, entity_id=entity_id, limit=100000, accepted_only=True, subject_only=True
    )
    records = [r for r in view["records"] if r["status"] == "accepted"]
    by_claim = defaultdict(list)
    for link in connections:
        other = link["target_id"] if link["source_id"] == entity_id else link["source_id"]
        by_claim[link["claim_id"]].append(_node(entities[other]))
    for record in records:
        record["element_id"] = _element(record, mapping)
        predicates = {r["predicate"] for r in connections if r["claim_id"] == record["id"]}
        if "variant_of" in predicates:
            record.update(element_id="7.models", dimension_id=7)
        elif predicates.intersection({"evolves_from", "replaces"}):
            record.update(element_id="2.route_evolution", dimension_id=2)
        elif "subtechnology_of" in predicates:
            record.update(element_id="4.parents", dimension_id=4)
        record["linked_entities"] = by_claim[record["id"]]
        event_links = [
            r
            for r in connections
            if r["claim_id"] == record["id"] and r["predicate"] == "role:event_subject"
        ]
        if event_links:
            dates = event_dates[event_links[0]["target_id"]]
            if dates and len({r["occurred_at"] for r in dates}) == 1:
                field = next((f for f in definitions if f["id"] == dates[0]["event_type"]), None)
                if field:
                    record.update(
                        field_id=field["id"],
                        label=field["label"],
                        value=dates[0]["occurred_at"],
                        element_id=field["element_id"],
                        dimension_id=field["dimension_id"],
                    )
        # Fields stored as observations/capabilities still use their catalog labels.
        if not record["field_id"]:
            match = next((f for f in definitions if f["label"] == record["label"]), None)
            if match:
                record["field_id"] = match["id"]
    for link in connections:
        if link["target_id"] != entity_id or link["source_id"] == entity_id:
            continue
        if link["predicate"] not in INVERSE:
            continue
        dimension, element, label = INVERSE[link["predicate"]]
        other = _node(entities[link["source_id"]])
        records.append(
            {
                "id": "incoming:" + link["claim_id"],
                "origin_claim_id": link["claim_id"],
                "entity_id": entity_id,
                "entity_name": root["canonical_name"],
                "entity_kind": root["kind"],
                "kind": "relation",
                "field_id": None,
                "label": label,
                "value": other["name"],
                "dimension_id": dimension,
                "element_id": element,
                "status": "accepted",
                "linked_entities": [other],
                "qualifiers": json.loads(link["scope_note"] or "{}"),
                "evidence": evidence[link["claim_id"]],
            }
        )
    related = {}
    for row in connections:
        other = row["target_id"] if row["source_id"] == entity_id else row["source_id"]
        related.setdefault(other, {**_node(entities[other]), "relations": set()})["relations"].add(
            row["label"]
        )
    sources = {}
    for ev in [e for r in records for e in r["evidence"]] + [
        e for values in evidence.values() for e in values
    ]:
        sources.setdefault(ev["revision_id"], {**ev, "quotes": set()})["quotes"].add(ev["quote"])
    return {
        "asset": _node(root),
        "records": records,
        "fields": definitions,
        "dimensions": [
            {
                "id": i,
                "name": name,
                "description": description,
                "records": sum(r["dimension_id"] == i for r in records),
                "elements": [e for e in elements if e["dimension_id"] == i],
            }
            for i, name, description in DIMENSIONS
        ],
        "related": [{**r, "relations": sorted(r["relations"])} for r in related.values()],
        "sources": [{**s, "quotes": sorted(s["quotes"])} for s in sources.values()],
    }


def asset_graph(store, slug, root_id, mode="overview", depth=1, limit=50, scope="asset"):
    with store.connect() as db:
        entities = _entities(db, slug)
        if root_id not in entities or entities[root_id]["kind"] not in PRIMARY_KINDS:
            raise KeyError("请选择当前专题的产品或技术")
        rows = _connections(db, slug)
        eligible = []
        for r in rows:
            if r["source_id"] not in entities or r["target_id"] not in entities:
                continue
            kinds = {entities[r["source_id"]]["kind"], entities[r["target_id"]]["kind"]}
            if mode == "lineage" and r["predicate"] not in LINEAGE:
                continue
            if mode == "technology" and (
                not kinds.intersection(TECHNICAL) or kinds - (TECHNICAL | {"product"})
            ):
                continue
            if mode == "applications" and (
                "product" not in kinds or not kinds.intersection({"technology", "route"})
            ):
                continue
            if mode != "capabilities" and r["predicate"] in {"has_metric", "has_capability"}:
                continue
            if mode == "capabilities" and r["predicate"] not in {"has_metric", "has_capability"}:
                continue
            eligible.append(r)
        adjacency = defaultdict(set)
        for r in eligible:
            adjacency[r["source_id"]].add(r["target_id"])
            adjacency[r["target_id"]].add(r["source_id"])
        # Statistics use the complete scoped projection, before viewport truncation.
        statistics = {
            r["subject_id"]: dict(r)
            for r in db.execute(
                "SELECT c.subject_id,count(DISTINCT c.id) record_count,"
                "count(DISTINCT s.revision_id) source_count FROM claims c "
                "LEFT JOIN claim_evidence e ON e.claim_id=c.id "
                "LEFT JOIN source_segments s ON s.id=e.segment_id "
                "WHERE c.topic_id=? AND c.status='accepted' GROUP BY c.subject_id",
                ("topic:" + slug,),
            )
        }
        # Stable ordering avoids layout jumps between refreshes. Traversal is scoped and bounded.
        distances, layers, frontier = {root_id: 0}, {root_id: 0}, deque([root_id])
        truncated = False
        while frontier:
            current = frontier.popleft()
            if distances[current] >= depth:
                continue
            neighbors = sorted(
                adjacency[current],
                key=lambda i: (entities[i]["kind"], entities[i]["canonical_name"]),
            )
            for other in neighbors:
                if other in distances:
                    continue
                if len(distances) >= limit:
                    truncated = True
                    continue
                distances[other] = distances[current] + 1
                layers[other] = distances[other]
                if mode == "lineage":
                    parent = any(
                        r["source_id"] == current and r["target_id"] == other for r in eligible
                    )
                    layers[other] = layers[current] + (-1 if parent else 1)
                frontier.append(other)
        if scope == "topic":
            candidates = sorted(
                (set(adjacency) | {
                    i for i, e in entities.items()
                    if mode == "overview" and e["kind"] in PRIMARY_KINDS
                })
                - {root_id},
                key=lambda i: (-len(adjacency[i]), entities[i]["canonical_name"], i),
            )
            chosen_ids = [root_id, *candidates[: limit - 1]]
            truncated = len(candidates) + 1 > limit
            distances = {i: distances.get(i) for i in chosen_ids}
            layers = {i: layers.get(i, 0) for i in chosen_ids}
        chosen = [
            r for r in eligible if r["source_id"] in distances and r["target_id"] in distances
        ]
        evidence = _evidence(db, [r["claim_id"] for r in chosen])
        grouped = {}
        for row in chosen:
            signature = (row["source_id"], row["target_id"], row["predicate"])
            edge = grouped.setdefault(
                signature,
                {
                    "id": "edge:" + ":".join(signature),
                    "source": row["source_id"],
                    "target": row["target_id"],
                    "predicate": row["predicate"],
                    "label": row["label"],
                    "claims": [],
                    "source_tables": set(),
                },
            )
            edge["source_tables"].add(row["table"])
            edge["claims"].append(
                {
                    "id": row["claim_id"],
                    "detail": row["detail"],
                    "qualifiers": json.loads(row["scope_note"] or "{}"),
                    "evidence": evidence[row["claim_id"]],
                }
            )
        for edge in grouped.values():
            if edge["predicate"] == "has_capability":
                states = {c["detail"] for c in edge["claims"]}
                edge["label"] = (
                    {
                        "supported": "支持",
                        "unsupported": "不支持",
                        "partial": "部分支持",
                        "planned": "计划支持",
                        "claimed_unverified": "来源声称",
                    }.get(next(iter(states)), "能力状态")
                    if len(states) == 1
                    else "能力状态不一致"
                )
        return {
            "root_id": root_id,
            "mode": mode,
            "depth": depth,
            "scope": scope,
            "truncated": truncated,
            "nodes": [
                {
                    **_node(entities[i]),
                    "distance": distance,
                    "layer": layers[i],
                    "neighbor_count": len(adjacency[i]),
                    "source_count": statistics.get(i, {}).get("source_count", 0),
                    "record_count": statistics.get(i, {}).get("record_count", 0),
                }
                for i, distance in distances.items()
            ],
            "edges": [{**e, "source_tables": sorted(e["source_tables"])} for e in grouped.values()],
            "available_neighbors": len(adjacency[root_id]),
        }


def material_inventory(store, slug, documents):
    """Relate the archived library to SQL facts by source URI, including saved revisions."""
    with store.connect() as db:
        linked = defaultdict(dict)
        stats = {}
        for row in db.execute(
            "SELECT d.canonical_uri,r.id revision_id,r.captured_at,c.id claim_id,c.claim_kind,"
            "e.id entity_id,e.canonical_name,e.kind,e.namespace FROM source_documents d "
            "JOIN source_revisions r ON r.document_id=d.id "
            "JOIN source_segments s ON s.revision_id=r.id "
            "JOIN claim_evidence v ON v.segment_id=s.id JOIN claims c ON c.id=v.claim_id "
            "JOIN entities e ON e.id=c.subject_id WHERE c.topic_id=? AND c.status='accepted'",
            ("topic:" + slug,),
        ):
            uri = row["canonical_uri"]
            stat = stats.setdefault(uri, {"claims": set(), "revisions": set(), "updated_at": ""})
            if row["claim_kind"] != "identity":
                stat["claims"].add(row["claim_id"])
            stat["revisions"].add(row["revision_id"])
            stat["updated_at"] = max(stat["updated_at"], row["captured_at"])
            if row["kind"] in PRIMARY_KINDS:
                linked[uri][row["entity_id"]] = _node(
                    {
                        "id": row["entity_id"],
                        "kind": row["kind"],
                        "canonical_name": row["canonical_name"],
                        "namespace": row["namespace"],
                    }
                )
        output = []
        for doc in documents:
            stat = stats.get(doc["url"], {})
            output.append(
                {
                    **doc,
                    "field_count": len(stat.get("claims", [])),
                    "revision_count": len(stat.get("revisions", [])),
                    "recorded_at": stat.get("updated_at"),
                    "assets": list(linked[doc["url"]].values()),
                }
            )
        return {
            "documents": output,
            "total": len(output),
            "mapped": sum(d["field_count"] > 0 for d in output),
        }
