"""Version locks and public/private data separation. Never imported by an adapter."""
import collections
import json
import shutil
import subprocess
from pathlib import Path

from .common import APP, ROOT, atomic_json, digest, now, read_json, sha_file


def source_files(root):
    excluded = {"__pycache__", ".git", ".venv", "node_modules", "data", ".workbench", "workspace", ".pytest_cache"}
    return {str(p.relative_to(root)): sha_file(p) for p in sorted(root.rglob("*"))
            if p.is_file() and not excluded.intersection(p.relative_to(root).parts)
            and p.suffix in {".py", ".md", ".yaml", ".yml", ".toml", ".json"}}


def prepare():
    private = ROOT / "datasets/private"
    rows = [json.loads(x) for x in (private / "widesearch.jsonl").read_text().splitlines() if x.strip()]
    for row in rows:
        if isinstance(row["evaluation"], str):
            row["evaluation"] = json.loads(row["evaluation"])
        assert (private / "widesearch_gold" / f"{row['instance_id']}.csv").exists()
    # HF JSONL stores evaluation as JSON strings; the official local loader
    # expects dictionaries. Decode transport format without changing any rule.
    (ROOT / "datasets/scoring_tasks.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    assert len(rows) == 200 and len({x["instance_id"] for x in rows}) == 200
    public = ROOT / "datasets/public"
    public.mkdir(parents=True, exist_ok=True)
    tasks = [{k: r[k] for k in ("instance_id", "query", "language")} for r in rows]
    (public / "tasks.jsonl").write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n")
    # Fixed hash ordering, independent of answers, table size and scores.
    languages = sorted({r["language"] for r in tasks})
    assert len(languages) == 2
    ordered = {lang: sorted([r["instance_id"] for r in tasks if r["language"] == lang],
                           key=lambda x: digest([20261104, x])) for lang in languages}
    splits = {"smoke2": sum((ordered[l][:1] for l in languages), []),
              "pilot10": sum((ordered[l][:5] for l in languages), []),
              "batch30": sum((ordered[l][:15] for l in languages), []),
              "full200": sorted(r["instance_id"] for r in tasks)}
    atomic_json(public / "splits.json", splits)
    pins = {}
    for name in ("widesearch", "searchos", "mindsearch", "open_deep_research", "scrapy", "crawl4ai"):
        repo = ROOT / "vendor" / name
        pins[name] = {
            "commit": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
            "remote": subprocess.check_output(["git", "-C", str(repo), "remote", "get-url", "origin"], text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()),
            "licenses": {str(p.relative_to(repo)): sha_file(p) for p in repo.glob("LICENSE*") if p.is_file()},
        }
        assert not pins[name]["dirty"], f"Vendor source modified: {name}"
    # Copy actual current collection implementation, with no private config or user data.
    ours_files = source_files(APP / "searchos")
    ours_id = digest(ours_files)
    ours_root = ROOT / "snapshots" / ours_id[:16]
    if not ours_root.exists():
        for sub in ("searchos", "skill_library"):
            src = APP / sub
            if src.exists():
                shutil.copytree(src, ours_root / sub,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".env"))
        shutil.copy2(APP / "pyproject.toml", ours_root / "pyproject.toml")
    atomic_json(ours_root / "source_hashes.json", ours_files)
    manifest = {
        "created_at": now(), "repositories": pins, "ours_source_sha256": ours_id,
        "ours_snapshot": str(ours_root.relative_to(ROOT)),
        "dataset_revision": (ROOT / "datasets/revision.txt").read_text().strip(),
        "dataset_files": {str(p.relative_to(private)): sha_file(p) for p in sorted(private.rglob("*"))
                          if p.is_file() and ".cache" not in p.parts},
        "tasks": len(tasks), "languages": dict(collections.Counter(t["language"] for t in tasks)),
        "public_tasks_sha256": sha_file(public / "tasks.jsonl"),
        "scoring_tasks_sha256": sha_file(ROOT / "datasets/scoring_tasks.jsonl"),
        "official_scorer_hashes": source_files(ROOT / "vendor/widesearch/src/evaluation"),
        "baseline_adaptation": "Official SearchOS unmodified; common external search provider injected through its public provider interface. LFS demo.gif kept as pointer.",
        "e4": {"searchos": "official upstream", "ours_off": "same current fork, adaptive collection disabled",
               "ours": "same current fork, adaptive collection enabled; P1/P2 disabled"},
    }
    atomic_json(ROOT / "manifest.json", manifest)
    upstream = source_files(ROOT / "vendor/searchos/searchos")
    changed = sorted(k for k in set(upstream) | set(ours_files) if upstream.get(k) != ours_files.get(k))
    atomic_json(ROOT / "reports/method_audit.json", {"changed_files": changed,
        "ours_sha256": ours_id, "original_commit": pins["searchos"]["commit"],
        "active_in_experiment": ["collection_policy BM25, lexical/link graph PageRank, Louvain, MMR, goal-gap and gain feedback"],
        "disabled": ["P1 evidence revision", "P2 context memory", "knowledge-base ingestion"],
        "not_implemented_as_independent_modules": ["query rewrite", "joint intent routing", "new verification module"],
        "attribution": "Upstream vs Ours includes other fork changes. Same-fork ours_off vs ours isolates the enabled adaptive module."})
    print(json.dumps({"tasks": len(tasks), "languages": manifest["languages"], "ours_snapshot": manifest["ours_snapshot"]}))


if __name__ == "__main__":
    prepare()
