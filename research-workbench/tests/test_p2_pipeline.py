"""Offline P2 through production extraction, completion and compaction boundaries."""

import json
import socket
from pathlib import Path

import pytest

from scripts.try_p2 import CORRECTION, SUMMARY, run_demo
from searchos.socm.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("P2 fixed-fixture demo must not contact models or network services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.asyncio
async def test_actual_p2_pipeline_survives_goal_omission_and_p1_revision(tmp_path):
    result = await run_demo(tmp_path)
    assert result["passed"], result["checks"]
    assert len(result["checks"]) == 17
    assert json.loads(Path(result["result_file"]).read_text()) == result
    path = Path(result["workspace"])
    final = WorkspaceManager(path.parent, path.name).load_state()
    assert final.p2.experiences["experience:p2-archive"].status == "revalidation_required"
    assert final.p2.experiences["experience:p2-current"].status == "active"
    assert final.p2.compaction_metrics[-1]["rebuilt"] is True
    assert final.p2.compaction_metrics[-1]["summary_model_calls"] == 0
    assert CORRECTION in (path / "output/p2-context-before.txt").read_text()
    after = (path / "output/p2-context-after.txt").read_text()
    assert CORRECTION in after and SUMMARY not in after
    assert result["selected_after"] == ["experience:p2-current"]
    assert final.p1.executions["p2-main-after"].read_artifacts == {"task:current": 1}


@pytest.mark.asyncio
async def test_repeated_demo_runs_preserve_existing_artifacts(tmp_path):
    marker = tmp_path / "existing-research.txt"
    marker.write_text("Preserve this.")
    first = await run_demo(tmp_path)
    original = Path(first["state_file"]).read_bytes()
    second = await run_demo(tmp_path)
    assert first["passed"] and second["passed"]
    assert first["workspace"] != second["workspace"]
    assert Path(first["state_file"]).read_bytes() == original
    assert marker.read_text() == "Preserve this."
