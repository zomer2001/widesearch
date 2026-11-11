"""Gold-derived fixtures strictly confined to evaluation/engineering_selftest."""
import csv
import json
import subprocess
import sys

from ..common import ROOT, atomic_json, config, credential_env
from ..conversion import table_markdown


def main():
    # Choose smallest official table for a cheap interface check, never an agent task.
    tables = []
    for p in (ROOT / "datasets/private/widesearch_gold").glob("*.csv"):
        with p.open() as f:
            rows = list(csv.reader(f))
        if rows and len(rows[0]) > 1 and len(rows) > 1:
            tables.append((len(rows), p.stem, rows))
    _, instance, rows = min(tables)
    directory = ROOT / "results/engineering_selftest"
    directory.mkdir(parents=True, exist_ok=True)
    bad = [list(r) for r in rows[1:]]
    for row in bad:
        row[-1] = "__DELIBERATELY_WRONG_ENGINEERING_FIXTURE__"
    cases = {"gold_as_prediction": table_markdown(rows[0], rows[1:]),
             "missing_column": table_markdown(rows[0][:-1], [r[:-1] for r in rows[1:]]),
             "wrong_cell": table_markdown(rows[0], bad), "empty": "",
             "no_matching_rows": table_markdown(rows[0], [["__UNRELATED_EMPTY_JOIN_TEST__"] * len(rows[0])]),
             "header_only": table_markdown(rows[0], [])}
    outcomes = {}
    for name, value in cases.items():
        prediction = directory / f"{name}.md"
        prediction.write_text(value)
        output = directory / f"{name}.json"
        subprocess.run([sys.executable, "-m", "widesearch_experiment.evaluation.official", "--instance", instance,
                        "--prediction", str(prediction), "--output", str(output),
                        "--config", str(ROOT / "configs/unified.json")], env=credential_env(config()), check=True)
        result = json.loads(output.read_text())
        outcomes[name] = {"status": result["status"], "metrics": result["metrics"]}
    passed = (all(r["status"] == "ok" for r in outcomes.values()) and
              outcomes["gold_as_prediction"]["metrics"]["f1_by_item"] == 1 and
              all(outcomes[x]["metrics"]["f1_by_item"] < 1 for x in ("missing_column", "wrong_cell", "empty", "no_matching_rows", "header_only")))
    atomic_json(directory / "summary.json", {"passed": passed, "instance_id": instance, "cases": outcomes,
                                              "excluded_from_experiments": True})
    if not passed:
        raise SystemExit("Official scorer self-check failed; inspect engineering_selftest logs")


if __name__ == "__main__":
    main()
