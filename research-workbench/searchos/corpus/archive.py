"""Append-only source versions. Frozen bundles never follow mutable page aliases.

Hashes detect accidental modification; they do not authenticate a publisher or
prove a claim true. Parsed text and downloaded bytes are distinct artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def now() -> str:
    return datetime.now(UTC).isoformat()


def immutable_write(path: Path, body: bytes) -> None:
    """Publish a complete file without overwriting an existing version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise ValueError(f"Immutable artifact differs: {path}") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not relative:
        raise ValueError("Artifact path must remain inside the bundle")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Artifact symlink leaves the bundle")
    return resolved


class CorpusArchive:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _blob(self, data: bytes, suffix: str) -> dict:
        sha = digest(data)
        relative = f"objects/{sha}{suffix}"
        immutable_write(self.root / relative, data)
        return {"path": relative, "sha256": sha, "bytes": len(data)}

    def add(
        self,
        *,
        source_url: str,
        text: str,
        title: str = "",
        fetched_at: str | None = None,
        representation: str = "page_markdown",
        original: bytes | None = None,
        original_media_type: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        if not source_url or not text.strip() or text.startswith("[FETCH_ERROR]"):
            raise ValueError("A source URL and nonempty successfully read text are required")
        payload = {
            "schema_version": 1,
            "source_url": source_url,
            "title": title,
            "fetched_at": fetched_at,
            "representation": representation,
            "text": self._blob(text.encode("utf-8"), ".txt"),
            "original": self._blob(original, ".bin") if original is not None else None,
            "original_media_type": original_media_type,
            "metadata": metadata or {},
        }
        # Same URL with changed content is a new document version. Multiple
        # origins with identical bytes remain separate provenance records.
        doc_id = "d_" + digest(canonical(payload))
        record = {"document_id": doc_id, **payload}
        immutable_write(self.root / "records" / f"{doc_id}.json", canonical(record))
        return record

    def records(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "records").glob("d_*.json")):
            record = json.loads(path.read_bytes())
            _validate_record(self.root, record)
            result.append(record)
        return result

    def freeze(
        self,
        destination: str | Path,
        *,
        dataset_id: str,
        document_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> Path:
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(f"Choose a new snapshot directory: {destination}")
        records = self.records()
        if document_ids is not None:
            wanted = set(document_ids)
            if not wanted <= {r["document_id"] for r in records}:
                raise ValueError("Unknown document version in selection")
            records = [r for r in records if r["document_id"] in wanted]
        if not records:
            raise ValueError("Cannot freeze an empty corpus")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".corpus-", dir=destination.parent))
        try:
            for record in records:
                for entry in (record["text"], record["original"]):
                    if entry:
                        target = staging / entry["path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            shutil.copyfile(safe_file(self.root, entry["path"]), target)
            payload = {
                "schema_version": 1,
                "dataset_id": dataset_id,
                "created_at": now(),
                "documents": records,
                "metadata": metadata or {},
            }
            manifest = {**payload, "manifest_sha256": digest(canonical(payload))}
            (staging / "manifest.json").write_bytes(canonical(manifest))
            load_bundle(staging)
            # No overwrite: an existing nonempty bundle makes rename fail.
            if destination.exists():
                raise FileExistsError(destination)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination / "manifest.json"


def _validate_record(root: Path, record: dict) -> None:
    payload = {k: v for k, v in record.items() if k != "document_id"}
    if record.get("schema_version") != 1:
        raise ValueError("Unsupported corpus version")
    if record.get("document_id") != "d_" + digest(canonical(payload)):
        raise ValueError("Document metadata checksum mismatch")
    for entry in (record["text"], record.get("original")):
        if entry:
            body = safe_file(root, entry["path"]).read_bytes()
            if digest(body) != entry["sha256"] or len(body) != entry["bytes"]:
                raise ValueError("Source content checksum mismatch")


def load_bundle(root: str | Path) -> tuple[dict, dict[str, str]]:
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_bytes())
    payload = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest version")
    if digest(canonical(payload)) != manifest.get("manifest_sha256"):
        raise ValueError("Manifest checksum mismatch")
    texts = {}
    for record in manifest["documents"]:
        _validate_record(root, record)
        doc_id = record["document_id"]
        if doc_id in texts:
            raise ValueError("Duplicate document ID")
        texts[doc_id] = safe_file(root, record["text"]["path"]).read_text(encoding="utf-8")
    if not texts:
        raise ValueError("Empty corpus")
    return manifest, texts


def import_workspace(workspace: str | Path, archive: CorpusArchive) -> int:
    """Import old sessions or all preserved versions from new sessions."""
    workspace = Path(workspace)
    if not (workspace / "pages").is_dir():
        raise ValueError("Workspace has no saved pages directory")
    versions = CorpusArchive(workspace / "pages" / "archive")
    records = versions.records()
    if records:
        for r in records:
            for entry in (r["text"], r.get("original")):
                if entry:
                    immutable_write(
                        archive.root / entry["path"],
                        safe_file(versions.root, entry["path"]).read_bytes(),
                    )
            immutable_write(archive.root / "records" / f"{r['document_id']}.json", canonical(r))
    # Also capture legacy aliases not in the new archive. No claim is made
    # about earlier overwritten versions of legacy sessions.
    known = {(r["source_url"], r["text"]["sha256"]) for r in records}
    for path in sorted((workspace / "pages").glob("*.meta.json")):
        meta = json.loads(path.read_bytes())
        body_path = safe_file(
            workspace / "pages", meta.get("content_path", path.name.replace(".meta.json", ".md"))
        )
        text = body_path.read_text(encoding="utf-8")
        url = meta.get("source_url") or meta["url"]
        if (url, digest(text.encode())) not in known:
            archive.add(
                source_url=url,
                text=text,
                title=meta.get("title", ""),
                fetched_at=meta.get("fetched_at"),
                metadata={"legacy_page_id": meta.get("page_id")},
            )
    return len(archive.records())


def import_library(data_root: str | Path, library_id: str, archive: CorpusArchive) -> int:
    """Copy authorized originals and verified parsed blocks, without model calls."""
    from searchos.local.library import LocalLibraryService

    service = LocalLibraryService(data_root)
    for doc in service.list_documents(library_id):
        original = service.original_path(library_id, doc["document_id"]).read_bytes()
        if doc["searchable"]:
            _, record, blocks = service._document_blocks(library_id, doc["document_id"])
            text = "\n\n".join(b.text for b in blocks if b.is_primary)
        elif doc["kind"] in {"markdown", "text", "json", "csv"}:
            text = original.decode("utf-8-sig")
        else:
            raise ValueError(f"Parse document before exporting: {doc['document_id']}")
        origin = doc.get("origin") or {}
        archive.add(
            source_url=origin.get("final_url")
            or origin.get("request_url")
            or f"local://{library_id}/{doc['document_id']}",
            text=text,
            title=doc["name"],
            original=original,
            fetched_at=origin.get("fetched_at"),
            representation="library_parsed_text",
            metadata={
                "library_id": library_id,
                "document_id": doc["document_id"],
                "origin": origin,
            },
        )
    return len(archive.records())
