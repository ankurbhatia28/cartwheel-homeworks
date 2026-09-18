"""Draw review batches into ``analysis/state/sample_manifest.json``.

The unit is a conversation (one ``cartwheel.session_id``). Every draw uses a
recorded seed so it can be reproduced, excludes conversations already in the
manifest (no conversation counts toward two batches), and shuffles the review
order so the reviewer can't tell which method picked a conversation.

    uv run python analysis/review_app/sample.py batch1            # 15 uniform + 15 cluster reps
    uv run python analysis/review_app/sample.py stratified --batch batch2 --dimension intent --per 3 --seed 2
    uv run python analysis/review_app/sample.py uniform --batch batch4 --k 15 --seed 4

Batch 3 (depth searches for candidate modes and close negatives) is chosen
by the reviewer, so it's added with ``add --batch ... --ids ...``.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "analysis" / "state" / "sample_manifest.json"
GRAPH = REPO / "analysis" / "state" / "graph.json"
DATA = Path(__file__).resolve().parent / "data" / "conversations.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_manifest() -> dict[str, Any]:
    if MANIFEST.exists() and MANIFEST.read_text().strip():
        manifest = json.loads(MANIFEST.read_text())
        if "conversations" in manifest:
            return manifest
    return {"unit": "conversation (cartwheel.session_id)", "population": None, "batches": [], "conversations": []}


def _conversations() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(DATA.read_text())["conversations"]}


def _entry(conv: dict[str, Any], batch: str, method: str, reason: str) -> dict[str, Any]:
    return {"id": conv["id"], "scenario_id": conv["scenario_id"], "trace_ids": conv["trace_ids"],
            "batch": batch, "method": method, "reason": reason, "added_at": _now()}


def _save(manifest: dict[str, Any], batch: dict[str, Any], entries: list[dict[str, Any]], seed: int) -> None:
    random.Random(seed + 1).shuffle(entries)  # review order hides which method picked each one
    manifest["batches"].append(batch)
    manifest["conversations"].extend(entries)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def batch1(k_uniform: int, k_cluster: int, seed: int) -> None:
    manifest, convs = _load_manifest(), _conversations()
    if any(b["id"] == "batch1" for b in manifest["batches"]):
        raise SystemExit("batch1 already exists in the manifest; not redrawing")
    manifest["population"] = {"conversations": len(convs), "traces": sum(len(c["trace_ids"]) for c in convs.values()),
                              "source": "HW3 final run, Langfuse 2026-09-16 00:40-01:45 UTC"}
    taken = {c["id"] for c in manifest["conversations"]}

    pool = sorted(i for i in convs if i not in taken)
    uniform = random.Random(seed).sample(pool, k_uniform)
    entries = [_entry(convs[i], "batch1", "uniform", f"uniform random draw, seed {seed}") for i in uniform]
    taken |= set(uniform)

    # Cluster representatives: closest to each centroid first, round-robin
    # across clusters (largest first) so every cluster is represented before
    # any gets a second pick. Skips anything the uniform draw already took.
    clusters = sorted(json.loads(GRAPH.read_text())["clusters"], key=lambda c: -c["size"])
    queues = {c["id"]: [i for i in c["representatives"] if i not in taken] for c in clusters}
    rank = {c["id"]: 0 for c in clusters}
    picks: list[tuple[str, int, int]] = []
    while len(picks) < k_cluster and any(queues.values()):
        for c in clusters:
            if len(picks) == k_cluster:
                break
            if queues[c["id"]]:
                picks.append((queues[c["id"]].pop(0), c["id"], rank[c["id"]]))
                rank[c["id"]] += 1
    for conv_id, cluster, r in picks:
        entries.append(_entry(convs[conv_id], "batch1", "cluster_representative",
                              f"cluster c{cluster}, #{r + 1} closest to centroid"))

    batch = {"id": "batch1", "method": f"{k_uniform} uniform random + {len(picks)} cluster representatives",
             "seed": seed, "created_at": _now(), "size": len(entries),
             "cluster_config": "analysis/state/graph.json (KMeans k=8, seed 7, on role, intent, tools, flags, turns, latency)"}
    _save(manifest, batch, entries, seed)
    print(f"batch1: {k_uniform} uniform + {len(picks)} cluster reps -> {MANIFEST.relative_to(REPO)}")


def uniform(batch_id: str, k: int, seed: int) -> None:
    manifest, convs = _load_manifest(), _conversations()
    if any(b["id"] == batch_id for b in manifest["batches"]):
        raise SystemExit(f"{batch_id} already exists in the manifest; not redrawing")
    taken = {c["id"] for c in manifest["conversations"]}
    picks = random.Random(seed).sample(sorted(i for i in convs if i not in taken), k)
    entries = [_entry(convs[i], batch_id, "uniform", f"uniform random draw from unreviewed, seed {seed}") for i in picks]
    _save(manifest, {"id": batch_id, "method": f"{k} uniform random from conversations not yet sampled",
                     "seed": seed, "created_at": _now(), "size": k}, entries, seed)
    print(f"{batch_id}: {k} uniform -> {MANIFEST.relative_to(REPO)}")


def _dimension_value(conv: dict[str, Any], dimension: str) -> str:
    if dimension == "role":
        return conv["role"]
    if dimension == "group":
        return conv["scenario"]["group"]
    return str(conv["scenario"]["tuple"].get(dimension))


def stratified(batch_id: str, dimension: str, per_value: int, seed: int) -> None:
    """Batch 2: a dimension chosen before looking at outcomes, spread evenly
    across its values, with a seeded uniform draw inside each value."""
    manifest, convs = _load_manifest(), _conversations()
    if any(b["id"] == batch_id for b in manifest["batches"]):
        raise SystemExit(f"{batch_id} already exists in the manifest; not redrawing")
    taken = {c["id"] for c in manifest["conversations"]}
    strata: dict[str, list[str]] = {}
    for conv_id in sorted(convs):
        if conv_id not in taken:
            strata.setdefault(_dimension_value(convs[conv_id], dimension), []).append(conv_id)
    rng = random.Random(seed)
    entries = []
    for value in sorted(strata):
        pool = strata[value]
        for conv_id in rng.sample(pool, min(per_value, len(pool))):
            entries.append(_entry(convs[conv_id], batch_id, f"stratified:{dimension}",
                                  f"{dimension}={value}, uniform within stratum, seed {seed}"))
    batch = {"id": batch_id, "method": f"{per_value} per {dimension} value, uniform within each value",
             "dimension": dimension, "values": sorted(strata), "seed": seed, "created_at": _now(),
             "size": len(entries)}
    _save(manifest, batch, entries, seed)
    print(f"{batch_id}: {len(entries)} across {len(strata)} {dimension} values -> {MANIFEST.relative_to(REPO)}")


def add(batch_id: str, method: str, scenario_ids: list[str], reason: str) -> None:
    manifest, convs = _load_manifest(), _conversations()
    by_scenario = {c["scenario_id"]: c for c in convs.values()}
    taken = {c["id"] for c in manifest["conversations"]}
    entries = []
    for sid in scenario_ids:
        conv = by_scenario[sid]
        if conv["id"] in taken:
            raise SystemExit(f"{sid} is already in the sample; a conversation can't count toward two batches")
        entries.append(_entry(conv, batch_id, method, reason))
    batch = next((b for b in manifest["batches"] if b["id"] == batch_id), None)
    if batch is None:
        manifest["batches"].append({"id": batch_id, "method": method, "created_at": _now(), "size": 0})
        batch = manifest["batches"][-1]
    batch["size"] += len(entries)
    manifest["conversations"].extend(entries)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{batch_id}: added {len(entries)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b1 = sub.add_parser("batch1")
    b1.add_argument("--uniform", type=int, default=15)
    b1.add_argument("--cluster", type=int, default=15)
    b1.add_argument("--seed", type=int, default=20260918)
    u = sub.add_parser("uniform")
    u.add_argument("--batch", required=True)
    u.add_argument("--k", type=int, default=15)
    u.add_argument("--seed", type=int, required=True)
    st = sub.add_parser("stratified")
    st.add_argument("--batch", required=True)
    st.add_argument("--dimension", required=True, help="role, group, or a tuple field such as intent")
    st.add_argument("--per", type=int, required=True)
    st.add_argument("--seed", type=int, required=True)
    a = sub.add_parser("add")
    a.add_argument("--batch", required=True)
    a.add_argument("--method", required=True)
    a.add_argument("--reason", required=True)
    a.add_argument("--ids", nargs="+", required=True, help="scenario ids, e.g. support-0012")
    args = p.parse_args()
    if args.cmd == "batch1":
        batch1(args.uniform, args.cluster, args.seed)
    elif args.cmd == "stratified":
        stratified(args.batch, args.dimension, args.per, args.seed)
    elif args.cmd == "uniform":
        uniform(args.batch, args.k, args.seed)
    else:
        add(args.batch, args.method, args.ids, args.reason)


if __name__ == "__main__":
    main()
