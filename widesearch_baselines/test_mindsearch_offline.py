"""Run with the pinned MindSearch interpreter; no model or search invocation."""
import socket
import sys
import unittest
from unittest.mock import patch

from widesearch_experiment.common import ROOT
from widesearch_baselines.mindsearch_protocol import plugin_parser_class


class NativeCompatibility(unittest.TestCase):
    def test_native_parser_accepts_normalized_tool(self):
        from lagent.prompts.parsers.tool_parser import ToolStatusCode
        parser = plugin_parser_class()()
        for raw in ('<|action_start|><|WebBrowser.search|>{"query":"fixture"}<|action_end|>',
                    '<|action_start|><|plugin|>\n{"name":"WebBrowser.search","parameters":{"query":"fixture"}}<|action_end|>'):
            result = parser.parse_response(raw)
            self.assertEqual(result["status"], ToolStatusCode.VALID_TOOL)
            self.assertEqual(result["tool_type"], "plugin")
            self.assertEqual(result["action"]["parameters"], {"query": "fixture"})

    def test_existing_interpreter_and_native_graph_are_available(self):
        sys.path.insert(0, str(ROOT / "vendor/mindsearch"))
        with patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("offline only")):
            from lagent.prompts import InterpreterParser
            from mindsearch.agent import init_agent
            from widesearch_baselines import mindsearch
            self.assertTrue(callable(init_agent) and callable(mindsearch.run))
            parser = InterpreterParser(begin="<|action_start|><|interpreter|>", end="<|action_end|>")
            result = parser.parse_response('<|action_start|><|interpreter|>graph.node("fixture")<|action_end|>')
            self.assertEqual(result["action"], 'graph.node("fixture")')


if __name__ == "__main__":
    unittest.main()
