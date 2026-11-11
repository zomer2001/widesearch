"""Narrow offline relationship repair from explicit, archived English clauses.

No model, network request, entity creation, semantic similarity or co-occurrence
inference. A proposal is recomputed from fresh verified originals before write.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from api.knowledge_database import _json, _now, _relation_id
from searchos.local.library import LocalLibraryService

RULE_VERSION = "explicit-clauses-v1"
_SUPPORTED = {"part_of", "carries", "deploys"}
_NEGATIVE = re.compile(
    r"\b(?:not|never|cannot|can't|may|might|could|would|if|whether|unlikely|proposed|planned)\b",
    re.I,
)


def _text(value):
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = value.replace("**", "").replace("__", "")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _term(value):
    return _text(value).strip(" .;:").casefold()


def _clean_name(value):
    return re.sub(r"^(?:the|a|an)\s+", "", value.strip(), flags=re.I).strip()


class _Names:
    def __init__(self, entities):
        self.names, self.aliases = {}, {}
        for entity in entities:
            self.names.setdefault(_term(entity["name"]), set()).add(entity["id"])
            aliases = entity.get("attributes", {}).get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip():
                        self.aliases.setdefault(_term(alias), set()).add(entity["id"])

    def exact(self, value):
        key = _term(_clean_name(value))
        # A canonical exact spelling takes precedence over a shadowing alias.
        ids = self.names.get(key, self.aliases.get(key, set()))
        return next(iter(ids)) if len(ids) == 1 else None

    def prefix(self, value, quote):
        value = _clean_name(value)
        candidates = []
        for name in self.names.keys() | self.aliases.keys():
            forms = [name]
            # An inflected noun is accepted only when its singular proper name
            # is independently explicit in this same quote (never page context).
            if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", quote, re.I):
                if re.search(r"[bcdfghjklmnpqrstvwxyz]y$", name):
                    forms.append(name[:-1] + "ies")
                elif re.search(r"[a-z]$", name):
                    forms.append(name + "s")
            for form in forms:
                match = re.match(re.escape(form) + r"(?![\w-])", value, re.I)
                if match:
                    identity = self.exact(name)
                    if identity:
                        candidates.append((len(form), identity))
        if not candidates:
            return None
        longest = max(length for length, _ in candidates)
        ids = {identity for length, identity in candidates if length == longest}
        return next(iter(ids)) if len(ids) == 1 else None


def _clauses(quote, names):
    text = _text(quote)
    for sentence in re.split(r"[.!?;](?:\s+|$)", text):
        sentence = sentence.strip()
        if not sentence or _NEGATIVE.search(sentence):
            continue
        family = re.fullmatch(
            r"(.+?) is (?:organized|divided) into (?:\d+|[a-z]+) "
            r"(?:distinct )?product lines:\s*(.+)",
            sentence,
            re.I,
        )
        if family:
            parent = names.exact(family[1])
            if parent:
                for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", family[2], flags=re.I):
                    child = names.exact(part)
                    if child and child != parent:
                        yield child, "part_of", parent, "原文明示产品线归属"
            continue
        active = re.fullmatch(
            r"(.+?) (?:(?:can|is able to) )?"
            r"(carry|carries|deploys|stows and (?:autonomously )?deploys) (.+)",
            sentence,
            re.I,
        )
        if active:
            source = names.exact(active[1])
            rest = re.sub(
                r"^(?:(?:about )?(?:a dozen|dozens|multiple|several|\d+)(?: of)?\s+)?"
                r"(?:the\s+)?(?:smaller|larger)?\s*",
                "",
                active[3],
                flags=re.I,
            )
            target = names.prefix(rest, text)
            if source and target and source != target:
                predicate = "deploys" if "deploy" in active[2].casefold() else "carries"
                yield (
                    source,
                    predicate,
                    target,
                    "原文明示部署关系" if predicate == "deploys" else "原文明示搭载关系",
                )
            continue
        passive = re.fullmatch(
            r"(.+?) (?:is|are|can be|(?:munition )?variants are described as "
            r"[a-z -]+? designed to be) carried by (?:autonomous systems including )?(.+)",
            sentence,
            re.I,
        )
        if passive:
            target = names.exact(passive[1])
            if target:
                for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", passive[2], flags=re.I):
                    source = names.exact(part)
                    if source and source != target:
                        yield source, "carries", target, "原文明示载荷由这些平台搭载"


def _archived(item):
    return (
        item.get("available") is True
        and bool(item.get("quote"))
        and str(item.get("ref_id", "")).startswith("local://")
        and bool(item.get("library_id"))
        and bool(item.get("document_id"))
    )


def propose_repairs(snapshot):
    """Return missing product edges only; suggestions do not mutate any store."""
    knowledge = snapshot.get("knowledge", snapshot)
    products = [
        entity
        for entity in knowledge.get("entities", [])
        if entity.get("kind") == "product"
        and entity.get("status") == "source_backed"
        and entity.get("origin") == "archive"
    ]
    names = _Names(products)
    existing = {
        (r["source_id"], r["predicate"], r["target_id"]) for r in knowledge.get("relations", [])
    }
    proposals = {}
    for evidence in knowledge.get("evidence", []):
        if not _archived(evidence):
            continue
        for source, predicate, target, reason in _clauses(evidence["quote"], names):
            key = source, predicate, target
            if key in existing:
                continue
            record = proposals.setdefault(
                key,
                {
                    "source_id": source,
                    "target_id": target,
                    "predicate": predicate,
                    "evidence_ids": [],
                    "reason": reason,
                },
            )
            record["evidence_ids"].append(evidence["id"])
    for proposal in proposals.values():
        proposal["evidence_ids"] = sorted(set(proposal["evidence_ids"]))
    return [proposals[key] for key in sorted(proposals)]


def apply_repair(database, candidate):
    """Revalidate a proposal, insert one missing edge, return 1 (or 0 if skipped).

    Tombstones, inactive records and manual edges are never revived or replaced.
    Replays are idempotent, and caller-supplied evidence IDs cannot add citations.
    """
    source, target, predicate = (
        candidate.get(key) for key in ("source_id", "target_id", "predicate")
    )
    ids = candidate.get("evidence_ids", [])
    if (
        not isinstance(source, str)
        or not isinstance(target, str)
        or source == target
        or predicate not in _SUPPORTED
        or not isinstance(ids, list)
        or not ids
        or not all(isinstance(key, str) for key in ids)
    ):
        return 0
    snapshot = database.snapshot()
    proof = next(
        (
            item
            for item in propose_repairs(snapshot)
            if (item["source_id"], item["predicate"], item["target_id"])
            == (source, predicate, target)
        ),
        None,
    )
    if proof is None or not set(ids).issubset(proof["evidence_ids"]):
        return 0
    verified_nodes = {item["id"]: item for item in snapshot["entities"]}
    identity, timestamp = _relation_id(source, predicate, target), _now()
    library = LocalLibraryService(database.data_root)
    with database._connection(write=True) as connection:
        endpoints = list(
            connection.execute(
                "SELECT id,name,kind FROM entities WHERE id IN (?,?) AND origin='archive' "
                "AND active=1 AND deleted_at IS NULL",
                (source, target),
            )
        )
        if len(endpoints) != 2 or any(
            row["kind"] != "product" or row["name"] != verified_nodes[row["id"]]["name"]
            for row in endpoints
        ):
            return 0
        if connection.execute(
            "SELECT 1 FROM relations WHERE source_id=? AND predicate=? AND target_id=?",
            (source, predicate, target),
        ).fetchone():
            return 0
        # A source may have been deleted while planning; check its actual local
        # citation again inside the write transaction and recompute the grammar.
        evidence = []
        for key in sorted(set(ids)):
            row = connection.execute("SELECT payload FROM evidence WHERE id=?", (key,)).fetchone()
            if row is None:
                return 0
            record = database._verify_evidence(json.loads(row[0]), library, {})
            if not _archived(record):
                return 0
            evidence.append(record)
        rechecked = propose_repairs({**snapshot, "evidence": evidence})
        if not any(
            (item["source_id"], item["predicate"], item["target_id"]) == (source, predicate, target)
            and set(ids).issubset(item["evidence_ids"])
            for item in rechecked
        ):
            return 0
        connection.execute(
            "INSERT INTO relations(id,source_id,target_id,predicate,origin,created_at,updated_at) "
            "VALUES(?,?,?,?,'archive',?,?)",
            (identity, source, target, predicate, timestamp, timestamp),
        )
        for key in sorted(set(ids)):
            connection.execute("INSERT INTO relation_evidence VALUES(?,?)", (identity, key))
            observation = hashlib.sha256(_json([RULE_VERSION, identity, key]).encode()).hexdigest()
            connection.execute(
                "INSERT INTO relation_observations VALUES(?,?,?,?,?)",
                (
                    observation,
                    identity,
                    "offline-repair:" + RULE_VERSION,
                    _json([key]),
                    timestamp,
                ),
            )
        database._metadata(connection, "updated_at", timestamp)
    return 1
