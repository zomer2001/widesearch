"""Lexical relevance, a sparse source graph, community discovery and MMR.

The graph is rebuilt per selection batch from at most 300 recent candidates.
Chinese character bigrams and English words share one lexical representation.
These are lexical signals, not an embedding model or semantic equivalence test.
"""

import math
import re
from collections import Counter
from urllib.parse import urldefrag

import networkx as nx
from rank_bm25 import BM25Plus

from .models import Candidate

_STOP = set(
    "the a an of and or in on to for with is are by from this that www com https http".split()
)


def tokens(text: str) -> list[str]:
    out = []
    for part in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.casefold()):
        if part in _STOP:
            continue
        if "\u3400" <= part[0] <= "\u9fff":
            out.extend(part[i : i + 2] for i in range(max(1, len(part) - 1)))
        else:
            out.append(part)
    return out


def canonical_url(url: str) -> str:
    # Preserve query parameters: version, pagination and document IDs are meaningful.
    return urldefrag(url.strip())[0]


def similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 0.0


def normalize(values: list[float]) -> list[float]:
    peak = max(values, default=0)
    return [max(0.0, float(v)) / peak for v in values] if peak > 0 else [0.0] * len(values)


def relevance(query: str, texts: list[str]) -> list[float]:
    corpus = [tokens(text) or ["__empty__"] for text in texts]
    if not corpus or not tokens(query):
        return [0.0] * len(texts)
    # delta=0 preserves zero relevance for documents with no matched query terms.
    return normalize(list(BM25Plus(corpus, delta=0).get_scores(tokens(query))))


def select(
    candidates: list[Candidate], query: str, weights: dict[str, float], wanted: set[str]
) -> tuple[list[str], list[dict], list[dict]]:
    if not candidates:
        return [], [], []
    ids = [c.url for c in candidates]
    lexical = [set(tokens(c.title + " " + c.text)) for c in candidates]
    rel = relevance(query, [c.title + " " + c.text for c in candidates])
    graph = nx.Graph()
    graph.add_nodes_from(ids)
    # Build once: graph edges and incremental MMR share the same O(n²) similarities.
    pair_scores = [[0.0] * len(ids) for _ in ids]
    for i in range(len(ids)):
        pair_scores[i][i] = 1.0
        for j in range(i):
            pair_scores[i][j] = pair_scores[j][i] = similarity(lexical[i], lexical[j])
    for i, candidate in enumerate(candidates):
        neighbors = sorted(
            ((pair_scores[i][j], j) for j in range(len(ids)) if j != i),
            reverse=True,
        )[:5]
        for strength, j in neighbors:
            if strength > 0:
                previous = graph.get_edge_data(candidate.url, ids[j], {}).get("weight", 0)
                graph.add_edge(candidate.url, ids[j], weight=max(strength, previous))
        for link in candidate.links:
            if link in graph and link != candidate.url:
                graph.add_edge(candidate.url, link, weight=1.0)
    seed = {url: rel[i] * weights.get(url, 1.0) for i, url in enumerate(ids)}
    if not any(seed.values()):
        seed = dict.fromkeys(ids, 1.0)
    centrality = nx.pagerank(graph, personalization=seed, max_iter=200)
    ranks = normalize([centrality[url] for url in ids])
    groups = (
        nx.community.louvain_communities(graph, seed=0, weight="weight")
        if graph.number_of_edges()
        else [{url} for url in ids]
    )
    cluster = {url: i for i, group in enumerate(groups) for url in group}
    summaries = []
    for i, group in enumerate(groups):
        words = Counter(word for j, url in enumerate(ids) if url in group for word in lexical[j])
        summaries.append(
            {
                "id": i,
                "terms": [word for word, _ in words.most_common(5)],
                "size": len(group),
                "read": sum(c.read for c in candidates if c.url in group),
            }
        )
    by_id = {url: i for i, url in enumerate(ids)}
    utility_scores = normalize(
        [(rel[i] + ranks[i]) / 2 * weights.get(url, 1.0) for i, url in enumerate(ids)]
    )
    remaining = [url for url in ids if url in wanted]
    chosen: list[str] = []
    decisions = []
    cluster_selected: Counter = Counter()
    previously_read = [j for j, c in enumerate(candidates) if c.read]
    redundancies = {
        url: max((pair_scores[by_id[url]][j] for j in previously_read), default=0.0)
        for url in remaining
    }
    while remaining:
        scored = []
        for url in remaining:
            i = by_id[url]
            redundancy = redundancies[url]
            utility = utility_scores[i] / math.sqrt(1 + cluster_selected[cluster[url]])
            # Relevance and novelty must share a scale; route rewards must not
            # overwhelm the redundancy penalty on an already-read copy.
            score = 0.5 * utility - 0.5 * redundancy
            scored.append((score, -i, url, redundancy))
        score, _, url, redundancy = max(scored)
        chosen.append(url)
        remaining.remove(url)
        for other in remaining:
            redundancies[other] = max(redundancies[other], pair_scores[by_id[other]][by_id[url]])
        cluster_selected[cluster[url]] += 1
        decisions.append(
            {
                "id": url,
                "kind": "page",
                "score": round(score, 4),
                "relevance": round(rel[by_id[url]], 4),
                "redundancy": round(redundancy, 4),
                "cluster": cluster[url],
            }
        )
    return chosen, decisions, summaries
