import json
from pathlib import Path

from widesearch_experiment.common import atomic_json
from widesearch_recovery.recover import choose_table, recover_batch, render_html, saved_table


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_timeout_recovers_last_checkpoint_without_cherry_picking(tmp_path):
    write(tmp_path / "prediction/prediction.md", "")
    write(tmp_path / "snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|\n|B|2|")
    latest = write(tmp_path / "snapshots/000002.md", "|name|value|\n|---|---|\n|A||")
    table, kind = choose_table(tmp_path, "timeout")
    assert table["path"] == latest and table["rows"] == [["A", ""]]
    assert kind == "recovered_checkpoint"
    assert (tmp_path / "prediction/prediction.md").read_text() == ""


def test_incomplete_snapshot_write_does_not_erase_saved_rows(tmp_path):
    valid = write(tmp_path / "snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|")
    broken = write(tmp_path / "snapshots/000002.md", "|name|value|\n|---|---|\n|A|1|\n|B|")
    assert saved_table(broken) is None
    assert choose_table(tmp_path, "running")[0]["path"] == valid
    assert choose_table(tmp_path, "running")[1] == "live_checkpoint"


def test_successful_header_only_final_is_authoritative(tmp_path):
    final = write(tmp_path / "prediction/prediction.md", "|name|value|\n|---|---|")
    write(tmp_path / "snapshots/000001.md", "|name|value|\n|---|---|\n|A|superseded|")
    table, kind = choose_table(tmp_path, "success")
    assert table["path"] == final and table["rows"] == [] and kind == "final"


def test_valid_final_wins_over_snapshot_even_after_execution_error(tmp_path):
    final = write(tmp_path / "prediction/prediction.md", "|name|value|\n|---|---|\n|A|final|")
    write(tmp_path / "snapshots/000999.md", "|name|value|\n|---|---|\n|A|different|")
    assert choose_table(tmp_path, "failed")[0]["path"] == final


def test_export_preserves_originals_and_keeps_attempts_separate(tmp_path):
    root = tmp_path / "source"
    for number, value in [(1, "15206所"), (2, "5.79万所")]:
        d = root / "raw/ours/ws_zh_055/trial_1" / f"attempt_{number}"
        atomic_json(d / "request.json", {"task": {"query": "Collect values"}})
        atomic_json(d / "result.json", {"status": "timeout", "response": ""})
        atomic_json(d / "pages/test.json", {"url": "https://example.com", "content": "<script>bad()</script> original"})
        write(d / "prediction/prediction.md", "")
        write(d / "snapshots/000005.md", f"|year|schools|\n|---|---|\n|2008|{value}|")
    originals = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    destination = tmp_path / "view"
    manifest = recover_batch(root, destination)
    assert len(manifest["entries"]) == 2
    assert [e["rows"][0][1] for e in manifest["entries"]] == ["15206所", "5.79万所"]
    assert manifest["original_scores_unchanged"] is True
    assert all(e["execution_status"] == "timeout" for e in manifest["entries"])
    for e in manifest["entries"]:
        assert (destination / e["csv_file"]).read_text().startswith("\ufeff")
        assert "&lt;script&gt;" in (destination / e["pages"][0]["file"]).read_text()
    assert originals == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert "已经采集到的资料" in (destination / "index.html").read_text()


def test_html_does_not_execute_table_content():
    output = render_html({"entries": [{"rows": [["</script><script>bad()</script>"]]}]})
    assert "</script><script>bad()" not in output
    assert "\\u003c/script>" in output
