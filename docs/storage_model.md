# Storage subsystem: file-prefix-v1

The storage model implements the Systems / Storage Engineer's contract from the
CrashCheck team plan. It is a **conservative, explicitly bounded model**, not a
claim to exhaustively model every Linux filesystem, SSD or power failure.

## Installation and supported profile

```bash
python -m pip install -r requirements-storage.txt
python -m crashcheck test examples/booking.py \
  --mode modeled --model file-prefix-v1 --seed 42 \
  --max-candidates 100 --out storage-run
```

Supported profile:

- One `app.sqlite` database, one writer, APSW-backed recording.
- Rollback journal `DELETE`, `PRAGMA synchronous=EXTRA`, `PRAGMA mmap_size=0`.
- All database connections opened with `sqlite3.connect` via the patched module
  or via `ctx.connect()` during the recording worker. A `from sqlite3 import
  connect` reference may bypass this bridge; the required all-kept replay
  normally catches the resulting missing writes.
- A limited compatibility bridge handles `execute`, `executemany`, `cursor`,
  implicit sqlite3 DML transactions, `commit`, `rollback`, `close` and context
  managers. This is **not** a complete sqlite3 implementation. Scenarios using
  uncommon SQLite APIs must be converted to APSW or handled by a future adapter.
- The existing process mode and all existing scenarios continue to use stdlib
  `sqlite3` without APSW.

Unsupported profiles, including WAL mode and writable mmap, are rejected with
an inconclusive outcome. New database files, independent attached databases,
external filesystem writes, concurrent transactions, and alternate SQLite
connections are outside the initial model.

## Trace contract (v1)

The backend stores `base` separately. The recorder emits:

```
trace/
  manifest.json       # schema, profile, file fingerprints, model limits
  events.jsonl        # monotonically increasing seq starting at zero
  blobs/<sha256>      # exact bytes written by xWrite, content-addressed
```

File events include `open` (with `created` and creation `generation`), `write`
(offset, length, blob), `truncate` (new size), `sync` (flags), `delete`
(`syncdir`) and `close`. Application events include `begin`, `ack`, and `marker`.
All events share the same sequence; all paths are restricted to the database
and its journal or WAL sidecars. The writer records only completed delegated VFS
operations. A malformed or changed blob invalidates the trace.

The recorder compares the completed workload's entire raw file inventory with
the result of applying **all** captured effects to the initial fixture. It
raises an error on any mismatch instead of silently testing an incomplete
trace. Backend failures remain inconclusive (exit 2), never a false pass.

## State model

At a cut `c`, let `E_f(c)` be all effects seen on file `f`. Effects are file
creation, writes, truncation and deletion. Let `s_f(c)` be the number of effects
on file `f` at its last completed `xSync` at or before `c`. A successful
`xDelete(syncdir=True)` is also a deletion durability barrier in this model.

For each file, the possible retained effects form an ordered prefix of length
`k_f`, constrained by:

```
s_f(c) <= k_f <= len(E_f(c))
```

The state generator considers the Cartesian product of these per-file prefix
lengths. This is intentionally not arbitrary unsynced-write reordering. Cuts
are prioritized at application markers and acknowledgements, then syncs, then
individual file effects. An all-kept final-state recipe is yielded first.

The generator never silently drops possible prefixes to fit a budget. The
backend's `--max-candidates` limit reports truncation as *inconclusive*.
Exact duplicate recovered file bytes are deduplicated by the backend, but
distinct acknowledgement histories are checked independently.

### Inspect a trace before choosing a search budget

```bash
python -m crashcheck inspect-trace storage-run/trace
```

This read-only command validates each event and write payload, then reports
event counts by kind, retained-effect counts per SQLite file, last completed
durability floors, the number of crash boundaries, and a
`candidate_upper_bound`. The upper bound includes every legal per-file prefix
combination at each cut plus the all-kept reference. It is intentionally an
*overestimate*: identical file states at different cuts and histories are not
deduplicated. It can be computed without APSW or candidate materialization.

### Bounded exploration and reproducibility

Within each selected crash boundary, the `--seed` value determines a
constant-memory permutation of the Cartesian product of legal per-file
prefixes. This spreads a small candidate budget across the possible prefix
lengths instead of always starting at the shortest ones. With an unlimited
budget, every legal combination is still generated exactly once for that
boundary. The all-kept control remains first, independent of seed.

The generator additionally skips boundaries with identical per-file effect
counts, durability floors, and begun/acknowledged operation histories. Such
boundaries have the same crash-state choices and application observation
context in `file-prefix-v1`. The trace inspector reports a conservative
pre-pruning upper bound, so its value can exceed the actual emitted recipes.

The model assumes that individual file effects are applied in recorded order,
the completed per-file `xSync` is a persistence barrier, and the modeled
`syncdir=True` deletion has reached stable directory state. It does not model
512 B / 4 KB torn sectors, block-device cache lies, unsynced file renames,
directory creation durability, or arbitrary reordered writes. Therefore a
finding must be described as a **file-prefix-v1 model finding**, not proof of
failure on a particular real filesystem.

## Materializer and minimization

The adapter consumes a JSON recipe:

```json
{"cut": 17, "prefixes": {"app.sqlite": 2, "app.sqlite-journal": 11}}
```

It validates that all prefix lengths satisfy the current per-file sync floors,
copies the base into a new empty directory, replays only retained effects in
chronological order, and returns the `cut`, `begun` and `acknowledged` histories
plus per-file effect counts. Invalid recipes or missing payloads fail safely.
Neither the base nor the trace is ever mutated by materialization.

`reductions()` yields legal smaller prefixes for the backend's existing
failure minimizer. Backend bundle and replay behavior is reused unchanged.

## Storage qualification checks

The independent tests cover:

1. Per-file sync floors with a small, hand-calculated trace.
2. Journal delete/recreate and file generations.
3. Blob hashing, malformed inputs and invalid recipes.
4. End-to-end booking failure and fresh `--regenerate` replay.
5. Explicit rejection of WAL mode in this initial profile.

Run all tests with:

```bash
python -m unittest discover -s tests -v
```

Next stages should validate real filesystem semantics, support native APSW
workloads without the sqlite3 bridge, implement WAL and journal checkpoint
handling, and add a versioned model for sector-level or directory crash states.
