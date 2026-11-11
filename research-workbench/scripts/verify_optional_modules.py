"""Run an actual model against the explicitly fictional, dated local examples.

Requires the workbench API to be running and seed_innovation_library.py to have
completed. This is an integration check, not a benchmark of factual accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
QUERY = (
    "请根据两份虚构公告，比较松林研究所（演示机构）在2024年和2025年的负责人，"
    "列出每条任职的完整有效期和原文引用，并说明两条记录是否因时间范围不同而可以同时成立。"
    "只依据所选资料。"
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("cli", "ollama", "deepseek"), default="cli")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--max-time", type=int, default=900)
    options = parser.parse_args()
    library = json.loads((ROOT / "data/innovation-library.json").read_text())
    request = {
        "query": QUERY,
        "engine": options.engine,
        "modules": ["evidence_revision", "context_memory"],
        "max_time": options.max_time,
        "effort": "medium",
        "sources": {
            "mode": "local",
            "local": {
                "library_id": library["library_id"],
                "selection": "documents",
                "document_ids": [item["document_id"] for item in library["documents"]],
                "model_access": True,
            },
        },
        "tables": [{
            "table_id": "leadership",
            "table_label": "虚构机构任职",
            "entities": ["松林研究所（演示机构）"],
            "attrs": ["机构", "负责人"],
            "primary_key": ["机构"],
        }],
    }
    async with httpx.AsyncClient(base_url=options.base_url, timeout=30) as client:
        response = await client.post("/api/search", json=request)
        response.raise_for_status()
        started = response.json()
        session_id = started["session_id"]
        print(json.dumps({"started": session_id, "module_config": started.get("module_config")},
                         ensure_ascii=False), flush=True)
        previous = None
        for _ in range((options.max_time + 240) // 5):
            response = await client.get(f"/api/search/{session_id}")
            response.raise_for_status()
            result = response.json()
            state = result.get("search_state") or {}
            graph = state.get("evidence_graph") or {}
            p2 = state.get("p2") or {}
            progress = {
                "status": result.get("status"),
                "evidence": len(graph.get("nodes") or []),
                "annotations": len(graph.get("claim_annotations") or {}),
                "comparisons": len((state.get("coverage_map") or {}).get("evidence_comparisons") or []),
                "experiences": len(p2.get("experiences") or {}),
                "verified_annotations": sum(
                    bool(item.get("verified"))
                    for item in (graph.get("claim_annotations") or {}).values()
                ),
                "grounded_experiences": sum(
                    item.get("status") == "active"
                    and bool((item.get("feedback") or {}).get("grounded_evidence_ids"))
                    for item in (p2.get("experiences") or {}).values()
                ),
            }
            if progress != previous:
                print(json.dumps(progress, ensure_ascii=False), flush=True)
                previous = progress
            if result.get("status") != "running":
                output = ROOT / "data" / f"innovation-verification-{session_id}.json"
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"record": str(output), "error": result.get("error"),
                                  "answer": result.get("answer", "")}, ensure_ascii=False), flush=True)
                answer = result.get("answer", "")
                answer_has_both_records = all(text in answer for text in ("林青", "陈远", "2024", "2025"))
                if (result.get("status") != "completed" or progress["evidence"] < 2
                        or progress["verified_annotations"] < 2
                        or not progress["grounded_experiences"]
                        or not answer_has_both_records):
                    print("本次尚未形成完整的证据、适用性标注和经验记录；不能计为增强验收通过。",
                          flush=True)
                    raise SystemExit(1)
                return
            await asyncio.sleep(5)
    raise SystemExit(f"检查超时；任务 {session_id} 的最新状态请在界面查看。")


if __name__ == "__main__":
    asyncio.run(main())
