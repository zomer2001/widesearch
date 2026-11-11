"""Reversible, topic-scoped identity resolution; never destroys source records."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher


@contextmanager
def _connection(store):
    store.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store.database, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    connection.execute("""CREATE TABLE IF NOT EXISTS topic_entity_merges (
        topic_id TEXT NOT NULL, alias_id TEXT NOT NULL, canonical_id TEXT NOT NULL,
        alias_name TEXT NOT NULL, canonical_name TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(topic_id,alias_id)
    )""")
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _name(value):
    value = unicodedata.normalize("NFKC", value.replace("™", "").replace("®", "")).casefold()
    return re.sub(r"[\s\-‐‑–—_]+", "", value)


def _versions(value):
    return re.findall(r"[a-z]*\d+[a-z]*", _name(value))


def _identity_conflict(left, right):
    qualifiers = (
        r"\b(?:standard|extended|long|short|cold|extra|mini|micro|pro|max|plus|lite|"
        r"light|heavy|advanced|enhanced)\b"
    )
    if set(re.findall(qualifiers, left["name"].casefold())) != set(
        re.findall(qualifiers, right["name"].casefold())
    ):
        return True
    for keys in [
        ("manufacturer", "厂商", "制造商", "开发商", "vendor"),
        ("version", "版本", "适用版本", "release_version"),
        ("model", "型号", "产品型号", "model_number"),
    ]:
        values = []
        for entity in (left, right):
            attrs = entity.get("attributes", {})
            observed = [
                attrs,
                *(
                    o.get("attributes", {})
                    for o in attrs.get("document_observations", [])
                    if isinstance(o, dict)
                ),
            ]
            values.append(
                {
                    _name(str(row[key]))
                    for row in observed
                    for key in keys
                    if row.get(key)
                    and str(row[key]).strip().casefold()
                    not in {"未知", "未公开", "未采集", "unknown", "not public", "不适用"}
                }
            )
        if values[0] and values[1] and values[0] != values[1]:
            return True
    return False


def duplicate_candidates(entities, relations):
    """Suggest formatting aliases and close names, never infer same identity."""
    neighbors = {e["id"]: set() for e in entities}
    for edge in relations:
        neighbors.get(edge["source_id"], set()).add(edge["target_id"])
        neighbors.get(edge["target_id"], set()).add(edge["source_id"])
    ordered = sorted(
        entities,
        key=lambda e: (
            -len(neighbors[e["id"]]),
            -len(e.get("evidence_ids", [])),
            len(e["name"]),
            e["id"],
        ),
    )
    result = []
    assigned = set()
    for index, canonical in enumerate(ordered):
        if canonical["id"] in assigned:
            continue
        for alias in ordered[index + 1 :]:
            if alias["id"] in assigned or canonical["kind"] != alias["kind"]:
                continue
            left, right = _name(canonical["name"]), _name(alias["name"])
            if not left or not right or _versions(left) != _versions(right):
                continue
            if _identity_conflict(canonical, alias):
                continue
            equal = left == right
            # A common prefix alone (e.g. Lattice / Lattice Mesh) is not an alias.
            close = (
                min(len(left), len(right)) >= 8
                and left not in right
                and right not in left
                and SequenceMatcher(None, left, right).ratio() >= 0.88
            )
            if not equal and not close:
                continue
            shared = len(neighbors[canonical["id"]] & neighbors[alias["id"]])
            result.append(
                {
                    "canonical_id": canonical["id"],
                    "alias_id": alias["id"],
                    "canonical_name": canonical["name"],
                    "alias_name": alias["name"],
                    "kind": canonical["kind"],
                    "confidence": "high" if equal else "review",
                    "reason": "仅空格、连字符或商标写法不同；仍需核对对象"
                    if equal
                    else "名称相近，需核对型号和含义",
                    "shared_neighbors": shared,
                }
            )
            if equal:
                assigned.add(alias["id"])
    return result


def _read_merges(store, topic_id):
    with _connection(store) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM topic_entity_merges WHERE topic_id=? ORDER BY created_at,alias_id",
                (topic_id,),
            )
        ]


def apply_topic_merges(store, topic_id, entities, relations):
    merges = _read_merges(store, topic_id)
    nodes = {e["id"]: copy.deepcopy(e) for e in entities}
    mapping = {}
    for merge in merges:
        alias_id, canonical_id = merge["alias_id"], merge["canonical_id"]
        if alias_id not in nodes or canonical_id not in nodes:
            continue  # Removing an original/membership never resurrects it.
        alias, canonical = nodes[alias_id], nodes[canonical_id]
        mapping[alias_id] = canonical_id
        canonical.setdefault("merged_entities", []).append(
            {
                "id": alias_id,
                "name": alias["name"],
                "attributes": copy.deepcopy(alias["attributes"]),
                "evidence_ids": list(alias.get("evidence_ids", [])),
                "field_overrides": copy.deepcopy(alias.get("field_overrides", {})),
            }
        )
        canonical["evidence_ids"] = sorted(
            set(canonical.get("evidence_ids", [])) | set(alias.get("evidence_ids", []))
        )
        attributes = canonical["attributes"]
        alias_names = attributes.get("aliases", [])
        if isinstance(alias_names, str):
            alias_names = [alias_names]
        attributes["aliases"] = list(dict.fromkeys([*alias_names, alias["name"]]))
        observations = attributes.setdefault("document_observations", [])
        observations.extend(copy.deepcopy(alias["attributes"].get("document_observations", [])))
        observations.append(
            {
                "summary": alias["summary"],
                "attributes": {
                    k: copy.deepcopy(v)
                    for k, v in alias["attributes"].items()
                    if k != "document_observations"
                },
                "source_ids": list(alias.get("evidence_ids", [])),
                "field_evidence": copy.deepcopy(alias.get("field_evidence", {})),
                "field_overrides": copy.deepcopy(alias.get("field_overrides", {})),
                "origin": alias.get("origin", "archive"),
                "merged_from": alias_id,
            }
        )
        for key, value in alias["attributes"].items():
            if key in {"document_observations", "aliases"}:
                continue
            if key not in attributes or attributes[key] in ("", None, [], {}):
                attributes[key] = copy.deepcopy(value)
            if (
                attributes.get(key) == value
                and key not in canonical.get("field_overrides", {})
                and key not in alias.get("field_overrides", {})
            ):
                evidence = canonical.setdefault("field_evidence", {})
                evidence[key] = sorted(
                    set(evidence.get(key, [])) | set(alias.get("field_evidence", {}).get(key, []))
                )
    result_nodes = [node for key, node in nodes.items() if key not in mapping]
    edges = {}
    for original in relations:
        edge = copy.deepcopy(original)
        edge["source_id"] = mapping.get(edge["source_id"], edge["source_id"])
        edge["target_id"] = mapping.get(edge["target_id"], edge["target_id"])
        if edge["source_id"] == edge["target_id"]:
            continue
        key = (edge["source_id"], edge["predicate"], edge["target_id"], edge.get("status"))
        if key in edges:
            edges[key]["evidence_ids"] = sorted(
                set(edges[key]["evidence_ids"]) | set(edge["evidence_ids"])
            )
            edges[key]["original_relation_ids"].append(original["id"])
            continue
        if (edge["source_id"], edge["target_id"]) != (original["source_id"], original["target_id"]):
            edge["id"] = (
                "relation:merged:" + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
            )
        edge["original_relation_ids"] = [original["id"]]
        edges[key] = edge
    return result_nodes, list(edges.values()), merges


def merge_entities(store, topic_id, canonical_id, alias_id):
    snapshot = store.snapshot(topic_id)
    entities = {e["id"]: e for e in snapshot["knowledge"]["entities"]}
    if canonical_id == alias_id or canonical_id not in entities or alias_id not in entities:
        raise ValueError("请选择本专题中两个不同的可见对象")
    candidate = next(
        (
            item
            for item in duplicate_candidates(
                list(entities.values()), snapshot["knowledge"]["relations"]
            )
            if {item["canonical_id"], item["alias_id"]} == {canonical_id, alias_id}
        ),
        None,
    )
    if candidate is None:
        raise ValueError("这两个对象没有可核对的同类别名建议；不同型号、版本或类别应保留关系")
    with _connection(store) as connection:
        rows = list(
            connection.execute(
                "SELECT alias_id,canonical_id FROM topic_entity_merges WHERE topic_id=?",
                (topic_id,),
            )
        )
        if any(
            row["alias_id"] in {canonical_id, alias_id} or row["canonical_id"] == alias_id
            for row in rows
        ):
            raise ValueError("对象合并状态已变化，请刷新后操作；先撤销已有合并才能更换主对象")
        connection.execute(
            "INSERT INTO topic_entity_merges VALUES(?,?,?,?,?,?,?)",
            (
                topic_id,
                alias_id,
                canonical_id,
                entities[alias_id]["name"],
                entities[canonical_id]["name"],
                candidate["reason"],
                datetime.now(UTC).isoformat(),
            ),
        )
    return store.snapshot(topic_id)


def undo_merge(store, topic_id, alias_id):
    store.get_topic(topic_id)
    with _connection(store) as connection:
        connection.execute(
            "DELETE FROM topic_entity_merges WHERE topic_id=? AND alias_id=?", (topic_id, alias_id)
        )
    return store.snapshot(topic_id)
