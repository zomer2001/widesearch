"""Narrow serialization compatibility for Lagent's existing browser tools."""
import json
import re

BEGIN = "<|action_start|>"
END = "<|action_end|>"
TOOL = re.compile(r"<\|action_start\|><\|(WebBrowser\.(?:search|select|open_url))\|>(.*?)<\|action_end\|>", re.S)


def valid_parameters(name, values):
    if not isinstance(values, dict):
        return False
    if name == "WebBrowser.search" and set(values) == {"query"}:
        query = values["query"]
        return isinstance(query, str) or (isinstance(query, list) and all(isinstance(x, str) for x in query))
    if name == "WebBrowser.select" and set(values) == {"select_ids"}:
        return isinstance(values["select_ids"], list) and all(type(x) is int for x in values["select_ids"])
    return name == "WebBrowser.open_url" and set(values) == {"url"} and isinstance(values["url"], str)


def normalize_plugin(data):
    """Only repair an explicit known tool tag containing complete valid args.

    Never infer tools from prose, invent arguments or turn planner output into
    Python. Unknown/malformed/incomplete calls remain available for diagnosis.
    """
    def replace(match):
        name, raw = match.groups()
        try:
            parameters = json.loads(raw)
        except ValueError:
            return match.group(0)
        if not valid_parameters(name, parameters):
            return match.group(0)
        return BEGIN + "<|plugin|>" + json.dumps({"name": name, "parameters": parameters}, ensure_ascii=False) + END
    return TOOL.sub(replace, data)


def plugin_parser_class():
    from lagent.prompts import PluginParser

    class CompatiblePluginParser(PluginParser):
        def __init__(self, **kwargs):
            super().__init__(begin=BEGIN + "<|plugin|>", end=END, **kwargs)

        def parse_response(self, data):
            return super().parse_response(normalize_plugin(data))
    return CompatiblePluginParser


def final_text(message):
    text = message.get("content", "")
    formatted = message.get("formatted") or {}
    if (message.get("stream_state") != 0 or not isinstance(text, str)
            or not text.strip() or BEGIN in text or formatted.get("tool_type")):
        return ""
    return text


def completed_node_text(message):
    nodes = (message.get("formatted") or {}).get("node", {})
    parts = []
    for name, node in nodes.items():
        if name in {"root", "response"}:
            continue
        response = node.get("response") or {}
        text = final_text(response)
        if text:
            parts.append(f"## {name}\n\n{text}")
    return "\n\n".join(parts)
