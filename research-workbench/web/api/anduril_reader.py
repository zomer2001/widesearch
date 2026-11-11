"""Read the evidence-bound Anduril reader artifact without invoking a model.

Local references are re-resolved on every load. Deleted or changed source
documents cannot keep supplying claims through an old reader artifact.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from searchos.local.library import LocalLibraryService

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_READER_ROOT = ROOT / "data/collections/anduril-products-technology/reader"
FAILURE_MARKERS = (
    "[fetch_error]",
    "[note] live fetch failed",
    "search-engine snippet",
    "search engine snippet",
)


def normalized_text(value: str) -> str:
    """Only whitespace is normalized; punctuation and case retain their meaning."""
    return re.sub(r"\s+", " ", value).strip()


def is_failed_source(text: str) -> bool:
    return any(marker in text.casefold() for marker in FAILURE_MARKERS)


def quote_matches(quote: str, text: str) -> bool:
    return (
        bool(quote.strip())
        and not is_failed_source(text)
        and (quote in text or normalized_text(quote) in normalized_text(text))
    )


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _http_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return url.scheme in {"http", "https"} and bool(url.netloc)
    except (ValueError, TypeError):
        return False


def empty_knowledge() -> dict[str, Any]:
    return {
        "title": "Anduril 知识",
        "updated_at": "",
        "status": "empty",
        "summary": "尚未生成由归档原文支持的可读知识。",
        "counts": {
            "products": 0,
            "technologies": 0,
            "routes": 0,
            "maturity": 0,
            "reproduction_units": 0,
        },
        "products": [],
        "technologies": [],
        "routes": [],
        "reproduction_units": [],
        "sources": [],
        "gaps": ["需要先将真实原文整理为有引句和本地引用的知识条目。"],
        "audit": {},
    }


def load_anduril_knowledge(
    data_root: str | Path,
    reader_root: str | Path | None = None,
    *,
    service: LocalLibraryService | None = None,
) -> dict[str, Any]:
    directory = Path(reader_root) if reader_root is not None else DEFAULT_READER_ROOT
    saved = _read(directory / "knowledge.json")
    external = _read(directory / "external-reference-units.json")
    if not saved and not external:
        return empty_knowledge()
    result = empty_knowledge()
    result.update(
        {key: copy.deepcopy(saved[key]) for key in ("title", "updated_at", "audit") if key in saved}
    )
    result["gaps"] = list(saved.get("gaps", []))
    library = service or LocalLibraryService(data_root)
    sources = {
        source["id"]: copy.deepcopy(source)
        for source in [*saved.get("sources", []), *external.get("sources", [])]
        if isinstance(source, dict) and source.get("id")
    }
    unavailable = 0
    for source in sources.values():
        reference = source.get("ref_id", "")
        if reference:
            try:
                citation = library.resolve_citation(reference)
                if source.get("library_id") and citation["library_id"] != source["library_id"]:
                    raise ValueError("source identity mismatch")
                if source.get("document_id") and citation["document_id"] != source["document_id"]:
                    raise ValueError("source identity mismatch")
                if not quote_matches(source.get("quote", ""), citation["content"]):
                    raise ValueError("source quote unavailable")
                source["available"] = True
            except Exception:
                source["available"] = False
                source["unavailable_reason"] = "原文已删除、版本变化或引用不可解析。"
                unavailable += 1
        else:
            # External repository metadata is a reference, not proof of local execution.
            source["available"] = _http_url(source.get("url", ""))

    def available_ids(item: dict) -> list[str]:
        return [key for key in item.get("source_ids", []) if sources.get(key, {}).get("available")]

    def evidence_item(item: dict) -> dict | None:
        ids = available_ids(item)
        return {**copy.deepcopy(item), "source_ids": ids} if ids else None

    for product in saved.get("products", []):
        details = [
            current
            for detail in product.get("technical_details", [])
            if (current := evidence_item(detail))
        ]
        if not details:
            continue
        current = copy.deepcopy(product)
        current["technical_details"] = details
        current["overview"] = " ".join(detail["text"] for detail in details[:3])
        current["source_ids"] = list(
            dict.fromkeys(key for detail in details for key in detail["source_ids"])
        )
        maturity = product.get("maturity", {})
        current["maturity"] = evidence_item(maturity) or {
            "stage": "未知",
            "basis": "尚无可用的原文阶段依据。",
            "source_ids": [],
        }
        result["products"].append(current)

    product_ids = {item["id"] for item in result["products"]}
    for category in ("technologies", "routes"):
        for item in saved.get(category, []):
            current = evidence_item(item)
            if current:
                current["product_ids"] = [
                    key for key in current.get("product_ids", []) if key in product_ids
                ]
                result[category].append(current)
    technology_ids = {item["id"] for item in result["technologies"]}
    route_ids = {item["id"] for item in result["routes"]}
    for product in result["products"]:
        product["technology_ids"] = [
            key for key in product.get("technology_ids", []) if key in technology_ids
        ]
    for technology in result["technologies"]:
        technology["route_ids"] = [
            key for key in technology.get("route_ids", []) if key in route_ids
        ]
    units = {
        item["id"]: item
        for item in [*saved.get("reproduction_units", []), *external.get("reproduction_units", [])]
        if item.get("id")
    }
    for item in units.values():
        current = evidence_item(item)
        if current:
            current.update(status="not_run", execution_status="not_run")
            current["related_technology_ids"] = [
                key for key in current.get("related_technology_ids", []) if key in technology_ids
            ]
            result["reproduction_units"].append(current)
    result["sources"] = list(sources.values())
    if unavailable:
        result["gaps"].append(f"{unavailable} 条原文依据已不可用，相关知识已隐藏或移除阶段判断。")
    result["counts"] = {
        "products": len(result["products"]),
        "technologies": len(result["technologies"]),
        "routes": len(result["routes"]),
        "maturity": sum(bool(item["maturity"]["source_ids"]) for item in result["products"]),
        "reproduction_units": len(result["reproduction_units"]),
    }
    if result["products"]:
        result["summary"] = (
            f"已根据归档原文整理 {len(result['products'])} 个产品或平台条目，"
            "覆盖其公开描述的技术机制和进展；可在条目中展开引句并查看原文。"
        )
        result["status"] = (
            "ready" if saved.get("status") == "ready" and not unavailable else "partial"
        )
    elif result["reproduction_units"]:
        result["summary"] = "已整理可学习的外部开源参考，产品与技术知识尚待归档原文支持。"
        result["status"] = "partial"
    else:
        result["gaps"].append("尚无可发布的产品知识；任务执行完成数量不代表知识核验完成。")
    result["gaps"] = list(dict.fromkeys(result["gaps"]))
    return result
