"""Export existing predictions/checkpoints without changing a running experiment.

The source attempt, execution status and original scores stay intact. Selection
uses final output or chronological checkpoints, never gold or judge scores.
This sidecar lives outside widesearch_experiment so its frozen code stays intact.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from widesearch_experiment.common import ROOT, atomic_json, now
from widesearch_experiment.conversion import table_markdown


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def saved_table(path):
    """Reject incomplete writes instead of silently dropping malformed rows."""
    try:
        before = path.stat()
        text = path.read_text()
        after = path.stat()
    except OSError:
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or any(not line.startswith("|") or not line.endswith("|") for line in lines):
        return None
    # Remove exactly the outer pipes: strip('|') would discard empty end cells.
    cells = [[value.strip() for value in re.split(r"(?<!\\)\|", line[1:-1])] for line in lines]
    header, separator, *rows = cells
    if (not header or len(set(header)) != len(header)
            or any(len(row) != len(header) for row in cells)
            or not all(re.fullmatch(r":?-+:?", value) for value in separator)):
        return None
    return {"header": header, "rows": rows, "text": text, "path": path,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


def choose_table(directory, status):
    """Keep a submitted final table; otherwise take the last valid checkpoint.

    An authoritative header-only final result from a successful run is retained.
    A newer valid checkpoint can have fewer rows than an older one: do not revive
    superseded facts by maximizing completeness or combining different attempts.
    """
    final = saved_table(directory / "prediction/prediction.md")
    if final and (final["rows"] or status == "success"):
        return final, "final"
    for path in sorted((directory / "snapshots").glob("*.md"), reverse=True):
        table = saved_table(path)
        if table:
            return table, "live_checkpoint" if status == "running" else "recovered_checkpoint"
    return final, "final" if final else "unavailable"


def safe_url(url):
    try:
        parts = urlsplit(url)
        return url if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password else ""
    except ValueError:
        return ""


def export_pages(directory, destination, attempt_id):
    links = []
    for path in sorted((directory / "pages").glob("*.json")):
        record = read_json(path)
        content, url = record.get("content", ""), record.get("url", "")
        if not isinstance(content, str) or not content.strip():
            continue
        relative = f"pages/{attempt_id}/{path.stem}.html"
        page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>已采集原文</title>"
        page += "<style>body{font:16px/1.75 system-ui;margin:40px auto;padding:0 24px;max-width:1000px;color:#213047}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#176b61}</style>"
        page += f"<h1>已采集原文</h1><p><a href='{html.escape(safe_url(url), quote=True)}' rel='noreferrer'>{html.escape(url)}</a></p><pre>{html.escape(content)}</pre></html>"
        atomic_text(destination / relative, page)
        links.append({"url": url, "file": relative, "characters": len(content)})
    return links


def recover_batch(root, destination):
    root, destination = Path(root), Path(destination)
    entries = []
    for request_file in sorted((root / "raw").glob("*/*/trial_*/attempt_*/request.json")):
        directory = request_file.parent
        request = read_json(request_file)
        record = read_json(directory / "result.json")
        system, instance, trial, attempt = directory.relative_to(root / "raw").parts
        status = record.get("status", "running")
        table, kind = choose_table(directory, status)
        attempt_id = "__".join([system, instance, trial, attempt])
        selected = read_json(directory.parent / "selected.json")
        selected_result = selected.get("recovery", {}).get("source_result", selected.get("result"))
        is_selected = selected_result == str(directory.relative_to(root) / "result.json")
        grade = read_json(root / selected["score"]) if is_selected and selected.get("score") else {}
        entry = {"id": attempt_id, "system": system, "instance": instance, "trial": trial, "attempt": attempt,
                 "execution_status": status, "kind": kind, "query": request.get("task", {}).get("query", ""),
                 "original_result": str(directory.relative_to(root) / "result.json"),
                 "is_selected_attempt": is_selected, "score_status": grade.get("status"),
                 "metrics": grade.get("metrics"), "recovery_used_for_scoring": is_selected and bool(selected.get("recovery")),
                 "rows_count": 0, "header": [], "rows": [], "filled_cells": 0, "total_cells": 0,
                 "pages": export_pages(directory, destination, attempt_id)}
        if table:
            markdown_file, csv_file = f"tables/{attempt_id}.md", f"tables/{attempt_id}.csv"
            atomic_text(destination / markdown_file, table_markdown(table["header"], table["rows"]) + "\n")
            output = io.StringIO()
            csv.writer(output).writerows([table["header"], *table["rows"]])
            atomic_text(destination / csv_file, "\ufeff" + output.getvalue())
            entry.update(header=table["header"], rows=table["rows"], rows_count=len(table["rows"]),
                         filled_cells=sum(bool(str(c).strip()) for row in table["rows"] for c in row),
                         total_cells=len(table["rows"]) * len(table["header"]),
                         source=str(table["path"].relative_to(root)), source_sha256=table["sha256"],
                         markdown_file=markdown_file, csv_file=csv_file)
        entries.append(entry)
    manifest = {"version": 1, "updated_at": now(), "batch": root.name, "source_root": str(root.resolve()),
                "policy": "final_or_latest_valid_checkpoint_per_attempt; no_cross_attempt_merge; no_new_research",
                "original_scores_unchanged": True,
                "recovered_predictions_regraded": any(e["recovery_used_for_scoring"] and e["score_status"] == "ok" for e in entries),
                "progress": read_json(root / "progress.json"), "entries": entries}
    atomic_json(destination / "recovery.json", manifest)
    atomic_text(destination / "index.html", render_html(manifest))
    return manifest


def render_html(manifest):
    # All source content enters the DOM through textContent. Prevent script-tag
    # termination when embedding JSON containing untrusted page/table strings.
    data = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    return HTML.replace("__DATA__", data)


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>采集结果 · 已保存资料</title><style>
:root{--ink:#172e36;--muted:#687d80;--green:#176f61;--line:#dce5df;--paper:#fff;--bg:#f3f6f1}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px}header{max-width:1450px;margin:auto;padding:38px 36px 24px;display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.eyebrow{font-size:12px;color:var(--green);letter-spacing:2px;font-weight:650}h1{font-size:30px;letter-spacing:-1px;margin:10px 0}p{line-height:1.7;color:var(--muted);margin:6px 0}button,a.button{border:1px solid var(--line);background:white;color:var(--ink);border-radius:8px;padding:10px 14px;font:inherit;cursor:pointer;text-decoration:none}button:hover,a.button:hover{border-color:var(--green)}main{max-width:1450px;margin:auto;padding:0 36px 40px}.overview{display:flex;gap:24px;margin-bottom:24px;color:var(--muted);flex-wrap:wrap}.overview strong{font-size:22px;color:var(--ink);margin-right:6px}.layout{display:grid;grid-template-columns:270px minmax(0,1fr);gap:22px}nav{display:flex;flex-direction:column;gap:8px}nav button{text-align:left;padding:15px;background:transparent;line-height:1.65}nav button.active{background:#e3efe5;border-color:#93b7a7;box-shadow:inset 3px 0 var(--green)}nav .small{display:block;font-size:12px;color:var(--muted);margin-top:3px}.panel{background:white;border:1px solid var(--line);border-radius:14px;overflow:hidden;min-width:0}.panel-head{padding:23px 25px;border-bottom:1px solid var(--line)}h2{margin:0 0 9px;font-size:21px}.meta{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px}.pill{padding:3px 9px;background:#edf3ee;border-radius:5px}.notice{padding:11px 16px;border-left:3px solid #bd9047;background:#faf5e8;margin:18px 0 0;color:#7c6038;line-height:1.6;font-size:13px}.toolbar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:16px 25px}input{border:1px solid var(--line);border-radius:8px;padding:11px;font:inherit;flex:1;min-width:160px;outline-color:var(--green)}.table-wrap{overflow:auto;max-height:62vh;border-top:1px solid var(--line)}table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}th{position:sticky;top:0;background:#eaf1ec;z-index:1;color:#36574f;font-weight:600;text-align:left;padding:13px 14px;white-space:nowrap;border-bottom:1px solid #cfddd3}td{padding:13px 14px;border-bottom:1px solid #edf0ec;line-height:1.6;min-width:105px;max-width:360px;vertical-align:top;overflow-wrap:anywhere}tr:nth-child(even) td{background:#fafcf9}td.empty{color:#afb8b1}.index{min-width:40px;color:#90a094;font-size:11px}.foot{padding:15px 25px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}details{margin:12px 25px 20px;line-height:1.65}summary{cursor:pointer;color:var(--green);padding:8px 0}.query{white-space:pre-wrap;background:#f6f8f4;padding:14px;max-height:230px;overflow:auto;color:var(--muted);font-size:13px}.sources{list-style:none;padding:0;max-height:280px;overflow:auto}.sources li{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}.sources a{color:var(--green)}.url{white-space:nowrap;text-overflow:ellipsis;overflow:hidden;flex:1}.empty-state{padding:36px;color:var(--muted)}@media(max-width:900px){header,main{padding-left:18px;padding-right:18px}.layout{grid-template-columns:1fr}nav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:25px}.overview{gap:14px}td,th{padding:10px}}
</style></head><body><header><div><div class="eyebrow">WIDESEARCH · SAVED DATA</div><h1>已经采集到的资料</h1><p>中断前的表格和网页原文都在这里。各次尝试独立保留。</p></div><button onclick="location.reload()">刷新</button></header><main><div class="overview" id="overview"></div><div class="layout"><nav id="nav" aria-label="选择任务与尝试"></nav><section class="panel"><div class="panel-head"><h2 id="title"></h2><div class="meta" id="meta"></div><div class="notice" id="notice"></div></div><div class="toolbar"><input id="filter" aria-label="筛选当前表格" placeholder="搜索年份、名称或任意字段…"><a class="button" id="csv" download>下载 CSV</a><a class="button" id="md" target="_blank">查看原始表格</a></div><div class="table-wrap" id="table"></div><div class="foot" id="foot"></div><details><summary>查看任务原文</summary><div class="query" id="query"></div></details><details><summary id="source-label">查看已保存网页</summary><ul class="sources" id="sources"></ul></details></section></div></main><script>
const DATA=__DATA__;
const $=id=>document.getElementById(id), titles={ws_zh_055:'全国教育统计',ws_zh_075:'剧集资料',ws_en_033:'公司年度财务指标'};
const kinds={final:'原有最终表',recovered_checkpoint:'中断前恢复的表',live_checkpoint:'运行中的过程表',unavailable:'尚无结构化结果'};
const statuses={success:'完成',failed:'执行异常',timeout:'超时',running:'运行中',parse_error:'格式异常',conversion_error:'转换异常',worker_error:'进程异常'};
const entries=[...DATA.entries].sort((a,b)=>Number(b.rows_count>0)-Number(a.rows_count>0)||a.instance.localeCompare(b.instance)||a.system.localeCompare(b.system)||Number(b.attempt.split('_')[1])-Number(a.attempt.split('_')[1]));
const have=entries.filter(e=>e.rows_count>0), tasks=new Set(have.map(e=>e.instance));
for(const [value,label] of [[tasks.size,'个任务已有资料'],[have.length,'份独立结果表'],[new Set(entries.flatMap(e=>e.pages.map(p=>p.url))).size,'个不同来源网页']]){const s=document.createElement('span'),n=document.createElement('strong');n.textContent=value;s.append(n,label);$('overview').append(s)}
const time=document.createElement('span');time.textContent='更新于 '+new Date(DATA.updated_at).toLocaleString('zh-CN');$('overview').append(time);
let selected;
function showTable(){const e=selected,q=$('filter').value.trim().toLowerCase(),rows=e.rows.map((r,i)=>[r,i]).filter(([r])=>r.some(v=>String(v).toLowerCase().includes(q)));$('table').replaceChildren();if(!e.header.length){const p=document.createElement('p');p.className='empty-state';p.textContent='这次尝试尚未留下表格。已有的网页仍可在下方查看。';$('table').append(p)}else{const table=document.createElement('table'),head=document.createElement('thead'),tr=document.createElement('tr');for(const value of ['#',...e.header]){const th=document.createElement('th');th.textContent=value;tr.append(th)}head.append(tr);table.append(head);const body=document.createElement('tbody');for(const [r,i]of rows){const row=document.createElement('tr');for(const [j,v]of [i+1,...r].entries()){const td=document.createElement('td');td.textContent=String(v).trim()?v:'—';if(j===0)td.className='index';else if(!String(v).trim())td.className='empty';row.append(td)}body.append(row)}table.append(body);$('table').append(table)}$('foot').textContent=`显示 ${rows.length} / ${e.rows_count} 行 · ${e.filled_cells} / ${e.total_cells} 个单元格有内容（含年份、名称等标识；不是准确率） · 空白以 — 显示`;}
function scoreLine(e){if(e.is_selected_attempt&&e.score_status==='ok'&&e.metrics)return ` 本轮评分：字段 F1 ${(e.metrics.f1_by_item*100).toFixed(2)}%，整行 F1 ${(e.metrics.f1_by_row*100).toFixed(2)}%。${e.recovery_used_for_scoring?' 已纳入恢复后的统计，原评分留档。':''}`;if(e.is_selected_attempt&&e.score_status)return ' 评分暂未成功，统计中记为未评分。';return ' 该次尝试单独保留，尚未选入主评分。';}
function select(e){selected=e;document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.id===e.id));$('title').textContent=(titles[e.instance]||e.instance)+' · '+(e.system==='ours'?'Ours':'SearchOS');$('meta').replaceChildren();for(const value of [e.instance,'第 '+e.attempt.split('_')[1]+' 次尝试',statuses[e.execution_status]||e.execution_status,kinds[e.kind],e.is_selected_attempt?'本轮计分所用的尝试':'单独保留']){const s=document.createElement('span');s.className='pill';s.textContent=value;$('meta').append(s)}$('notice').textContent=(e.kind==='live_checkpoint'?'任务仍在运行，这是一份当前保存的过程表。':'恢复只导出已经存在的内容，没有重新搜索或补填事实。')+(e.instance==='ws_zh_055'?' 教育统计中存在“所／万所、人／万人”混用，下面保留原值，暂不能直接据此汇总。':e.instance==='ws_zh_075'?' 尚未统一核验日期、名称和获奖记录。':' 原始数值仍可能存在单位、口径或字段对齐问题。')+scoreLine(e);$('query').textContent=e.query;$('csv').hidden=!e.csv_file;$('md').hidden=!e.markdown_file;$('csv').href=e.csv_file||'#';$('md').href=e.markdown_file||'#';$('filter').value='';$('source-label').textContent=`查看已保存网页（${e.pages.length}）`;$('sources').replaceChildren();for(const page of e.pages){const li=document.createElement('li'),a=document.createElement('a'),url=document.createElement('span');a.textContent='打开已存原文';a.href=page.file;a.target='_blank';url.className='url';url.textContent=page.url;li.append(a,url);$('sources').append(li)}showTable()}
for(const e of entries){const b=document.createElement('button');b.dataset.id=e.id;const name=document.createElement('strong');name.textContent=(e.system==='ours'?'Ours':'SearchOS')+' · '+(titles[e.instance]||e.instance);const sub=document.createElement('span');sub.className='small';sub.textContent=`第 ${e.attempt.split('_')[1]} 次 · ${e.rows_count} 行 · ${statuses[e.execution_status]||e.execution_status}`;b.append(name,sub);b.onclick=()=>select(e);$('nav').append(b)}
$('filter').addEventListener('input',showTable);if(entries.length)select(entries.find(e=>e.kind==='recovered_checkpoint'&&e.system==='ours'&&e.rows_count>0)||entries[0]);
</script></body></html>'''


def batch_running(root):
    try:
        with (root / ".lock").open() as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--watch", action="store_true", help="Refresh exports until the running batch releases its lock")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("Invalid batch label")
    root = ROOT / "results" / args.label
    if not (root / "frozen.json").exists():
        parser.error("No frozen batch exists for this label")
    destination = ROOT / "reports" / args.label / "recovered"
    previous = None
    while True:
        manifest = recover_batch(root, destination)
        summary = [(e["id"], e["execution_status"], e["rows_count"], e.get("source_sha256")) for e in manifest["entries"]]
        if summary != previous:
            print(json.dumps({"updated_at": manifest["updated_at"], "tables": sum(e["rows_count"] > 0 for e in manifest["entries"]),
                              "viewer": str(destination / "index.html")}, ensure_ascii=False), flush=True)
            previous = summary
        if not args.watch or not batch_running(root):
            # One final export captures outputs published immediately before unlock.
            recover_batch(root, destination)
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
