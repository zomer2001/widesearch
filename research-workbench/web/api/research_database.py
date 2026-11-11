"""Read projections for field data, evidence and seven-dimension navigation."""

from __future__ import annotations

import json

from searchos.knowledge_store.catalog import DIMENSIONS, ENTITY_KINDS

KIND_DIMENSIONS = {
    "technology": [1, 4],
    "route": [3],
    "generation": [2],
    "event": [2],
    "component": [4],
    "material": [4],
    "process": [4],
    "interface": [4],
    "reference": [4],
    "metric": [5],
    "capability": [5],
    "person": [6],
    "team": [6],
    "organization": [6],
    "achievement": [6],
    "product": [7],
    "scenario": [7],
    "application": [7],
}


def snapshot(
    store,
    slug="anduril",
    query="",
    dimension=0,
    entity_id="",
    limit=150,
    *,
    accepted_only=False,
    subject_only=False,
):
    topic_id = "topic:" + slug
    with store.connect() as db:
        topic = db.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        entities = [
            dict(r)
            for r in db.execute(
                "SELECT e.* FROM entities e JOIN topic_entities t ON t.entity_id=e.id "
                "WHERE t.topic_id=? AND e.state='active' ORDER BY e.canonical_name",
                (topic_id,),
            )
        ]
        definition_map = {
            r["id"]: dict(r)
            for r in db.execute(
                "SELECT f.id,f.label,m.element_id,d.dimension_id "
                "FROM field_definitions f JOIN field_storage_mappings m ON m.field_id=f.id "
                "JOIN dimension_elements d ON d.id=m.element_id"
            )
        }
        items = []
        claim_scope = " AND c.status='accepted'" if accepted_only else ""
        parameters = [topic_id]
        if subject_only and entity_id:
            claim_scope += " AND c.subject_id=?"
            parameters.append(entity_id)
        claims = list(
            db.execute(
                "SELECT c.*,e.canonical_name,e.kind subject_kind FROM claims c "
                "JOIN entities e ON e.id=c.subject_id WHERE c.topic_id=? "
                + claim_scope
                + " ORDER BY c.recorded_at DESC,c.id",
                parameters,
            )
        )
        for row in claims:
            c = dict(row)
            kind, identity = c["claim_kind"], c["id"]
            label, value, dimension_id = kind, None, KIND_DIMENSIONS.get(c["subject_kind"], [7])[0]
            field_id = None
            if kind == "identity":
                label, value = "名称与身份", c["canonical_name"]
                identity_row = db.execute(
                    "SELECT n.name,n.name_type,i.scheme,i.value FROM identity_assertions a "
                    "LEFT JOIN entity_names n ON n.id=a.name_id "
                    "LEFT JOIN entity_identifiers i ON i.id=a.identifier_id WHERE a.claim_id=?",
                    (c["id"],),
                ).fetchone()
                if identity_row and identity_row["name"]:
                    value = identity_row["name"]
                    label = {
                        "alias": "别名",
                        "abbreviation": "简称",
                        "former": "曾用名",
                        "translation": "译名",
                        "code": "代号",
                    }.get(identity_row["name_type"], "名称与身份")
                    if value == c["canonical_name"]:
                        label = "名称与身份"
                elif identity_row and identity_row["value"]:
                    label, value = identity_row["scheme"], identity_row["value"]
            elif kind == "field":
                detail = db.execute(
                    "SELECT * FROM field_values WHERE claim_id=?", (identity,)
                ).fetchone()
                if not detail:
                    continue
                field_id = detail["field_id"]
                definition = definition_map.get(field_id, {})
                label = definition.get("label", field_id)
                dimension_id = definition.get("dimension_id", dimension_id)
                value = json.loads(detail["value_json"])
            elif kind == "metric":
                detail = db.execute(
                    "SELECT m.*,e.canonical_name metric_name FROM metric_observations m "
                    "JOIN entities e ON e.id=m.metric_id WHERE m.claim_id=?",
                    (identity,),
                ).fetchone()
                label, value, dimension_id = detail["metric_name"], dict(detail), 5
            elif kind == "capability":
                detail = db.execute(
                    "SELECT p.*,e.canonical_name capability_name FROM product_capabilities p "
                    "JOIN entities e ON e.id=p.capability_id WHERE claim_id=?",
                    (identity,),
                ).fetchone()
                label, value, dimension_id = detail["capability_name"], detail["support_status"], 5
                field_id = detail["function_field_id"]
            elif kind == "relation":
                detail = db.execute(
                    "SELECT r.*,t.label,e.canonical_name target_name FROM entity_relations r "
                    "JOIN relation_types t ON t.id=r.relation_type_id "
                    "JOIN entities e ON e.id=r.object_id "
                    "WHERE claim_id=?",
                    (identity,),
                ).fetchone()
                label, value = detail["label"], detail["target_name"]
                dimension_id = 3 if detail["relation_type_id"] == "has_route" else 4
            elif kind == "participation":
                detail = db.execute(
                    "SELECT p.*,r.label,e.canonical_name target_name,e.kind target_kind "
                    "FROM participations p JOIN participation_roles r ON r.id=p.role_id "
                    "JOIN entities e ON e.id=p.object_id WHERE claim_id=?",
                    (identity,),
                ).fetchone()
                label, value = detail["label"], detail["target_name"]
                dimension_id = (
                    2
                    if detail["target_kind"] == "event"
                    else 7
                    if detail["role_id"]
                    in {"user", "operator", "supplier", "integrator", "applied_product"}
                    else 6
                )
            else:
                tables = {
                    "event": "event_details",
                    "assessment": "assessments",
                    "maturity": "maturity_assessments",
                    "composition": "composition_items",
                    "interface": "interface_connections",
                    "route_step": "route_steps",
                    "classification": "classifications",
                }
                table = tables.get(kind)
                detail = (
                    db.execute(f'SELECT * FROM "{table}" WHERE claim_id=?', (identity,)).fetchone()
                    if table
                    else None
                )
                if not detail:
                    continue
                value = dict(detail)
                if kind == "composition":
                    value["component_name"] = db.execute(
                        "SELECT canonical_name FROM entities WHERE id=?", (detail["component_id"],)
                    ).fetchone()[0]
                if kind == "classification":
                    value["term_name"] = db.execute(
                        "SELECT label FROM terms WHERE id=?", (detail["term_id"],)
                    ).fetchone()[0]
                labels = {
                    "event": "事件时间",
                    "assessment": "评价与特点",
                    "maturity": "成熟阶段",
                    "composition": "组成与载荷",
                    "interface": "接口",
                    "route_step": "路线步骤",
                    "classification": "分类",
                }
                label = labels.get(kind, kind)
                dimension_id = {
                    "event": 2,
                    "assessment": 5,
                    "maturity": 5,
                    "composition": 4,
                    "interface": 4,
                    "route_step": 3,
                    "classification": 1,
                }[kind]
            evidence = [
                dict(r)
                for r in db.execute(
                    "SELECT e.quote,e.stance,d.title,d.canonical_uri url,"
                    "s.revision_id FROM claim_evidence e "
                    "JOIN source_segments s ON s.id=e.segment_id "
                    "JOIN source_revisions r ON r.id=s.revision_id "
                    "JOIN source_documents d ON d.id=r.document_id "
                    "WHERE e.claim_id=?",
                    (identity,),
                )
            ]
            items.append(
                {
                    "id": identity,
                    "entity_id": c["subject_id"],
                    "entity_name": c["canonical_name"],
                    "entity_kind": c["subject_kind"],
                    "kind": kind,
                    "field_id": field_id,
                    "label": label,
                    "value": value,
                    "dimension_id": dimension_id,
                    "status": c["status"],
                    "qualifiers": json.loads(c["scope_note"] or "{}"),
                    "evidence": evidence,
                }
            )
        dims = [
            {
                "id": i,
                "name": name,
                "description": description,
                "records": sum(x["dimension_id"] == i and x["kind"] != "identity" for x in items),
            }
            for i, name, description in DIMENSIONS
        ]
        counts = {
            "entities": len(entities),
            "records": len(items),
            "fields": sum(x["kind"] != "identity" for x in items),
            "sources": len({e["revision_id"] for x in items for e in x["evidence"]}),
        }
        search = query.strip().casefold()
        filtered = [
            x
            for x in items
            if (not dimension or x["dimension_id"] == dimension)
            and (not entity_id or x["entity_id"] == entity_id)
            and (
                not search
                or search
                in (
                    x["entity_name"]
                    + " "
                    + x["label"]
                    + " "
                    + json.dumps(x["value"], ensure_ascii=False)
                ).casefold()
            )
        ]
        return {
            "topic": dict(topic) if topic else {"id": topic_id, "slug": slug, "title": slug},
            "dimensions": dims,
            "counts": counts,
            "entities": [
                {**e, "kind_label": ENTITY_KINDS[e["kind"]][0]}
                for e in entities
                if e["kind"] not in {"metric", "capability"}
            ],
            "records": filtered[:limit],
            "matching_records": len(filtered),
            "limit": limit,
            "schema_version": "research_framework_v1",
        }


def source_text(store, revision_id):
    with store.connect() as db:
        row = db.execute(
            "SELECT d.title,d.canonical_uri url,s.text_content FROM source_revisions r "
            "JOIN source_documents d ON d.id=r.document_id "
            "JOIN source_segments s ON s.revision_id=r.id "
            "WHERE r.id=? ORDER BY s.locator LIMIT 1",
            (revision_id,),
        ).fetchone()
        if not row:
            raise KeyError("原文不存在")
        return dict(row)
