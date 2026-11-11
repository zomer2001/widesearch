"""Rebuild pinned vendor repos, Python environments and official dataset."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="Install pinned runtime dependencies")
    parser.add_argument("--download", action="store_true", help="Fetch pinned official repositories and dataset")
    args = parser.parse_args()
    if args.download:
        repos = json.loads((ROOT / "configs/repositories.json").read_text())
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        for name, pin in repos.items():
            folder = ROOT / "vendor" / name
            if not folder.exists():
                run("git", "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
                    "clone", "--no-checkout", pin["remote"], folder, env=env)
                run("git", "-C", folder, "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
                    "checkout", pin["commit"], env=env)
            actual = subprocess.check_output(["git", "-C", str(folder), "rev-parse", "HEAD"], text=True).strip()
            if actual != pin["commit"]:
                raise RuntimeError(f"Existing {name} differs from pin; refusing to overwrite")
    if args.install:
        for environment, lock in ((".venv", "requirements-runtime.lock"), (".venv-mindsearch", "requirements-mindsearch.lock")):
            target = ROOT / environment
            if not target.exists():
                run(sys.executable, "-m", "venv", target)
            python = target / "bin/python"
            run(python, "-m", "pip", "install", "-r", ROOT / lock)
        run(ROOT / ".venv/bin/python", "-m", "pip", "install", "--no-deps", "-e", ROOT / "vendor/scrapy")
        run(ROOT / ".venv/bin/python", "-m", "playwright", "install", "chromium")
    if args.download:
        revision = (ROOT / "datasets/revision.txt").read_text().strip()
        code = "from huggingface_hub import snapshot_download; import sys; snapshot_download('ByteDance-Seed/WideSearch', repo_type='dataset', revision=sys.argv[1], local_dir=sys.argv[2], allow_patterns=['widesearch.jsonl','widesearch_gold/*','LICENSE','README.md'])"
        run(ROOT / ".venv/bin/python", "-c", code, revision, ROOT / "datasets/private")
    print("Bootstrap finished. Run python -m widesearch_experiment prepare, then selftest.")


if __name__ == "__main__":
    main()
