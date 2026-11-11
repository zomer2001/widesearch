"""One bounded GPT-4o structured extraction check; credentials never enter logs."""
from __future__ import annotations

import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]


def main() -> int:
    from api import workbench

    key = getpass.getpass("OpenAI API key (hidden): ").strip()
    if not key:
        print("No credential supplied; no request made.")
        return 1
    document = ROOT / "examples/anduril/05-06-Anduril Announces $5B Series H Raise.md"
    source = document.read_text(encoding="utf-8").split("When we founded", 1)[0]
    manifest = json.loads((document.parent / "manifest.json").read_text())
    origin = next(item["origin"] for item in manifest["files"] if item["file"] == document.name)
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"facts": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string", "enum": ["financing_amount", "valuation", "lead_investor"]},
                    "value": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["subject", "predicate", "value", "evidence_quote"],
            },
        }},
        "required": ["facts"],
    }
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requested_model": "gpt-4o", "request_count": 1,
        "source_url": origin["final_url"], "source_file": str(document.relative_to(ROOT)),
        "source_form": "previously_archived_article_copy",
        "ok": False, "credential_saved": False,
    }
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": "gpt-4o", "temperature": 0, "max_tokens": 700,
                    "messages": [
                        {"role": "system", "content": "Extract only explicitly stated corporate financing facts from the provided public article excerpt. Copy an exact supporting quote for every fact. Do not add a date or any knowledge from memory."},
                        {"role": "user", "content": source},
                    ],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "anduril_financing_check", "strict": True, "schema": schema,
                    }},
                },
            )
        report["http_status"] = response.status_code
        if response.status_code != 200:
            try:
                error = response.json().get("error", {})
                for field in ("type", "code"):
                    value = error.get(field)
                    if isinstance(value, str) and len(value) < 100 and key not in value:
                        report["error_" + field] = value
            except (ValueError, AttributeError):
                pass
        else:
            body = response.json()
            report["actual_model"] = body.get("model")
            report["usage"] = body.get("usage", {})
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                report["error_type"] = "incomplete_or_refused"
            else:
                result = json.loads(choice["message"]["content"])
                facts = result.get("facts", [])
                for fact in facts:
                    quote = fact.get("evidence_quote", "")
                    fact["quote_verified"] = bool(quote and quote in source)
                    fact["source_char_start"] = source.find(quote) if quote else -1
                report["facts"] = facts
                report["ok"] = bool(facts) and all(f["quote_verified"] for f in facts)
                if report["ok"]:
                    config = workbench.load_config()
                    config.openai = workbench.OpenAIEndpoint(api_key=key)
                    config.engine = "openai"
                    workbench.save_config(config)
                    report["credential_saved"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
    report_path = ROOT / "data/openai-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
