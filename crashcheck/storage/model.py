"""Deterministic file-prefix-v1 crash states and byte-exact materialization.

For each file, effects preceding its last completed xSync are mandatory. Effects
after that barrier may survive as an ordered *prefix* only. This is intentionally
more restrictive than an arbitrary write-reordering model; it is not a complete
POSIX, ext4 or physical power-loss model. Directory and sector faults are out of
scope. xDelete(syncdir=True) is a durability barrier for deletion in this model.
"""

from __future__ import annotations

from math import gcd, prod
from pathlib import Path
from random import Random
import shutil

from .trace import read_trace, safe_file


def effect(event):
    kind = event["kind"]
    return kind in ("write", "truncate", "delete") or (kind == "open" and event["created"])


def boundaries(events):
    """Material changes, barriers, and application receipts are crash cuts."""
    return [e["seq"] for e in events if effect(e) or e["kind"] in ("sync", "begin", "ack", "marker")]


def at_cut(events, cut):
    if type(cut) is not int or cut < -1 or cut >= len(events):
        raise ValueError("invalid crash cut")
    counts, floors = {}, {}
    begun, acked = [], []
    for e in events[:cut+1]:
        file = e.get("file")
        if file is not None and effect(e):
            counts[file] = counts.get(file, 0) + 1
        if e["kind"] == "sync":
            floors[file] = counts.get(file, 0)
        if e["kind"] == "delete" and e["syncdir"]:
            floors[file] = counts.get(file, 0)
        if e["kind"] == "begin":
            begun.append(e["ordinal"])
        if e["kind"] == "ack":
            acked.append(e["ordinal"])
    return counts, floors, {"begun": begun, "acknowledged": acked}


def all_kept_recipe(trace: Path):
    _, events = read_trace(trace)
    cut = len(events) - 1
    counts, _, _ = at_cut(events, cut)
    return {"cut": cut, "prefixes": dict(sorted(counts.items()))}


def _permuted_indices(widths, seed, cut):
    """Visit a Cartesian space exactly once, using O(1) enumeration memory.

    A seeded modular stride permutes integer indices; a mixed-radix decoder
    below maps each index back to its per-file retained-effect counts. This
    avoids building or shuffling a potentially enormous Cartesian product.
    """
    total = prod(widths)
    rng = Random(seed ^ ((cut + 1) * 0x9E3779B97F4A7C15))
    start = rng.randrange(total)
    stride = 1
    if total > 1:
        stride = rng.randrange(1, total)
        while gcd(stride, total) != 1:
            stride = stride + 1 if stride + 1 < total else 1
    for number in range(total):
        index = (start + number * stride) % total
        digits = []
        for width in reversed(widths):
            index, digit = divmod(index, width)
            digits.append(digit)
        yield tuple(reversed(digits))


def recipes(trace: Path, seed: int):
    """Yield all legal per-file prefixes in seeded order (never silently sample).

    The backend's candidate budget takes a bounded prefix of this generator and
    records truncation as inconclusive, rather than calling sampling a pass.
    Repeated cuts with identical effect/floor counts and receipt histories are
    skipped, since they cannot produce a distinct modeled recovered state.
    """
    _, events = read_trace(trace)
    if not events:
        return
    all_kept = all_kept_recipe(trace)
    # First candidate doubles as a useful replay reference.
    yield all_kept
    seen = {(all_kept["cut"], tuple(sorted(all_kept["prefixes"].items())))}
    cuts = boundaries(events)
    # Application markers, acknowledgements and syncs are the most useful
    # crash boundaries. Explore them before low-level intermediate writes.
    priority = {"marker": 0, "ack": 0, "begin": 1, "sync": 2}
    cuts.sort(key=lambda cut: (priority.get(events[cut]["kind"], 3), cut))
    seen_frontiers = set()
    for cut in cuts:
        counts, floors, history = at_cut(events, cut)
        paths = sorted(counts)
        frontier = (tuple(sorted(counts.items())),
                    tuple((path, floors.get(path, 0)) for path in paths),
                    tuple(history["begun"]), tuple(history["acknowledged"]))
        if frontier in seen_frontiers:
            continue
        seen_frontiers.add(frontier)
        lower = [floors.get(path, 0) for path in paths]
        widths = [counts[path] - floor + 1 for path, floor in zip(paths, lower)]
        for offsets in _permuted_indices(widths, seed, cut):
            prefixes = dict(zip(paths, (floor + offset for floor, offset in zip(lower, offsets))))
            key = (cut, tuple(sorted(prefixes.items())))
            if key not in seen:
                seen.add(key)
                yield {"cut": cut, "prefixes": prefixes}


def materialize(base: Path, trace: Path, recipe, dest: Path):
    """Construct a new raw file set without touching base or trace."""
    _, events = read_trace(trace)
    if type(recipe) is not dict or set(recipe) != {"cut", "prefixes"}:
        raise ValueError("expected a cut and per-file prefix counts")
    cut, prefixes = recipe["cut"], recipe["prefixes"]
    counts, floors, history = at_cut(events, cut)
    if type(prefixes) is not dict or set(prefixes) != set(counts):
        raise ValueError("recipe files do not match the trace")
    for file, length in prefixes.items():
        safe_file(file)
        if type(length) is not int or not floors.get(file, 0) <= length <= counts[file]:
            raise ValueError(f"invalid prefix for {file}")
    if not dest.is_dir() or any(dest.iterdir()):
        raise ValueError("materialization destination must be a new empty directory")
    shutil.copytree(base, dest, dirs_exist_ok=True, symlinks=False)
    used = {file: 0 for file in counts}
    for e in events[:cut+1]:
        if not effect(e):
            continue
        file = e["file"]
        used[file] += 1
        if used[file] > prefixes[file]:
            continue
        path = dest / safe_file(file)
        kind = e["kind"]
        if kind == "open":
            if path.exists():
                raise ValueError("trace creates a file that already exists")
            path.touch(exist_ok=False)
        elif kind == "delete":
            if not path.is_file():
                raise ValueError("trace deletes a missing file")
            path.unlink()
        elif kind == "truncate":
            if not path.is_file():
                raise ValueError("trace truncates a missing file")
            with path.open("r+b") as stream:
                stream.truncate(e["size"])
        elif kind == "write":
            if not path.is_file():
                raise ValueError("trace writes to a missing file")
            with path.open("r+b") as stream:
                stream.seek(e["offset"])
                stream.write((trace / "blobs" / e["blob"]).read_bytes())
    return {"cut": cut, "history": history, "effects": used}


def reductions(trace: Path, recipe):
    """Legal, strictly smaller surviving-effect prefixes for delta debugging."""
    _, events = read_trace(trace)
    cut, prefixes = recipe["cut"], recipe["prefixes"]
    counts, floors, _ = at_cut(events, cut)
    if set(prefixes) != set(counts):
        raise ValueError("invalid minimization recipe")
    for file in sorted(prefixes):
        current = prefixes[file]
        low = floors.get(file, 0)
        if type(current) is not int or current < low or current > counts[file]:
            raise ValueError("invalid current prefix")
        for replacement in sorted({low, current - 1}):
            if low <= replacement < current:
                smaller = dict(prefixes, **{file: replacement})
                yield {"cut": cut, "prefixes": smaller}
