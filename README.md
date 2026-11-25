# CrashCheck

> **Development status:** The `main` branch provides the process-crash testing harness. APSW-based SQLite VFS tracing and file-prefix crash-state exploration are developed on [`feat/storage-engineering`](https://github.com/suryandugy-glitch/PQL-Storage/tree/feat/storage-engineering). Check out that branch before using its storage-model commands.

## Project overview

CrashCheck is a Python developer tool for investigating **application-level SQLite consistency after abrupt termination**. A scenario supplies a fresh database fixture, a reproducible workload, optional recovery logic, and invariant checks. The harness produces crash candidates, reopens them in isolated workers, and saves replayable evidence when an invariant fails.

| Layer | Responsibility |
| --- | --- |
| Scenario hooks | Define setup, operations, recovery and invariants. |
| Crash scheduler | Select process markers or delegate crash-state generation to a storage adapter. |
| Candidate verifier | Reopen SQLite, check integrity and invoke application recovery and invariants. |
| Evidence pipeline | Deduplicate outcomes, record run manifests, replay and minimize model failures. |

**Scope:** The default process mode terminates a worker while the host OS continues running. It does not simulate a power failure. See the [storage-engineering branch](https://github.com/suryandugy-glitch/PQL-Storage/tree/feat/storage-engineering) for its explicitly bounded disk-persistence model.

CrashCheck runs a SQLite application from a clean fixture, stops the process at a scenario marker and checks what a fresh process can recover. It requires Python 3.11+ and the core uses only the standard library. The reference environment is Linux.

## Repository relationship

This repository is a **standalone copy for storage-engineering development**. It is not a GitHub fork; the feature branch contains the latest seeded storage-model improvements.

## Run the booking example

From the checkout root, run the following commands. Choose a new output directory for each run.

```sh
python -m crashcheck test examples/booking.py --mode process --seed 42 --out booking-run
python -m crashcheck replay booking-run --candidate c_000
python -m unittest discover -s tests -v
```

`crashcheck run` is an alias of `crashcheck test`. The example intentionally commits a seat decrement before inserting its booking. Cuts after the seat update leave a missing seat, report `booking.capacity` and exit 1. Set `SPLIT_COMMIT = False` in a copy of the example to exercise the atomic control. All three cuts should pass.

## Commands

| Command | Purpose |
| --- | --- |
| `crashcheck test SCENARIO --out DIR [--mode process\|modeled] [--model NAME \| --adapter FILE] [--seed N]` | Explore cuts or modeled candidates. |
| `crashcheck replay BUNDLE [--candidate ID] [--regenerate] [--ignore-runtime]` | Re-verify a saved failure in a fresh worker. `BUNDLE` may be a run directory when `--candidate` names the failure. |
| `crashcheck minimize BUNDLE --out DIR` | Shrink a modeled failure through adapter reductions. |

Budgets: `--max-candidates N` (default 100) caps attempted candidates, `--max-checks N` caps fresh verifications, `--time-budget SECONDS` caps wall-clock time for the run, and `--worker-timeout SECONDS` (default 30) caps each worker invocation. `--seed N` defaults to 0. Without `--mode`, a run is modeled when `--model` or `--adapter` is given and process otherwise. `--model NAME` loads `crashcheck/models/NAME.py` (dashes become underscores). Invalid combinations exit 2.

## Scenario API

Supply one trusted, self-contained Python file. Workers run with their private fixture as the working directory. Use files under `ctx.root`, the SQLite path `ctx.db`, and the seeded generator `ctx.random`. Keep the scenario deterministic and close database connections in setup. Module globals and the generator start fresh in each worker.

| Hook | Responsibility |
| --- | --- |
| `setup(ctx)` | Create the base fixture, including `app.sqlite`. |
| `run(ctx)` | Perform operations and call `ctx.marker(name)` at interesting crash points. |
| `recover(ctx)` (optional) | Production-equivalent application recovery, run after SQLite opens the candidate and passes its integrity check. |
| `observe(ctx, history)` | Check application state against `history.begun` and `history.acknowledged`. Return normally to pass or raise `crashcheck.CheckFailure(check_id, failure_class, message)` for a verified violation. Must not modify the database. |

Context helpers: `ctx.connect(readonly=True)` opens the database with foreign keys enforced (use it in `observe`), `ctx.now()` is a deterministic clock and `ctx.new_id(prefix)` a deterministic ID generator, both seeded from `--seed`.

Module-level switches: `CHECK_FOREIGN_KEYS = True` adds `PRAGMA foreign_key_check` to every verification. `STRICT_ORDERING = False` allows overlapping operations.

Call `ctx.begin(ordinal)` before each operation and `ctx.acknowledge(ordinal)` when its receipt becomes externally visible. Ordinals are unique nonnegative integers. By default the next operation may begin only after the previous one is acknowledged, so at most one terminal operation is in flight at any cut.

## Verification stages

Every candidate is copied, then a fresh worker runs these stages in order and records a per-check entry (`stage`, `check_id`, `failure_class`, `status`, `detail`) for each:

1. `open`: open with creation disabled, then read so SQLite rolls back a hot journal.
2. `integrity`: `PRAGMA integrity_check`, plus `foreign_key_check` when declared. Results are recorded before the application hook runs.
3. `app-recovery`: the optional `recover` hook. A raised exception is a verified failure `app.recovery` of class `recovery-error`, kept distinct from harness errors. If the hook changed the files, integrity runs again.
4. `observe`: application invariants and acknowledgement checks. The files are fingerprinted before and after, and a change is a harness error (inconclusive).

An outcome carries `status`, and for failures `check_id`, `failure_class`, `stage`, `detail` and the full `checks` list.

## Exploration and evidence

Process mode discovers markers with an uninterrupted run, then repeats the workload on a fresh base clone for each selected marker. Repeated names are valid. A cumulative digest checks the marker and operation-history prefix at each cut. The selected marker atomically writes its receipt and calls `os._exit(77)`, skipping Python cleanup. The parent accepts a cut only when it has the receipt and the exit status 77. A missing receipt or any other exit is a harness error. Recovery runs on a copy so opening SQLite cannot change the saved candidate or its hot journal. Process mode exercises process termination while the host OS stays running. On a worker timeout, and after every worker, the whole process group is killed.

**Clean control.** The uninterrupted final state is verified first. If it is a verified failure, the fixture or checks are invalid: the run is inconclusive and no cuts are explored. An inconclusive control (for example a scenario that asserts on state only a graceful exit produces) is recorded under `control` and does not block exploration.

**Scheduling.** Cuts whose marker name mentions acknowledgement (`ack`), `commit`, `sync`, `journal`, `truncate`, `delete` or `rename` run first, then all others, each in encounter order. When the cuts exceed `--max-candidates`, each group is sampled with a generator seeded from `--seed`, and the chosen cuts keep encounter order. In modeled mode the adapter's recipe order is used.

**Counts.** `manifest.json` has `search`: `mode` (`exhaustive`, `sampled` or `adapter-ordered`), `generated`, `attempted`, `deduplicated` and `checked`. A truncated or budget-limited plan is inconclusive, never a pass. `budget_reason` says which budget ran out.

**Deduplication.** A SHA-256 fingerprint covers relative paths, file lengths and contents, including sidecars and empty directories. A verdict is reused only when the fingerprint, the begun and acknowledged histories and the checker configuration all match. Reused candidates record `reused_from` and `reused_from_id`, and keep their own cut.

**Candidate IDs.** Each candidate has an ID like `c_017-1a2b3c4d`: its plan position and a hash of the run identity and recipe. `--candidate` accepts the full ID or the `c_017` part.

| Exit | Meaning |
| --- | --- |
| 0 | Every planned candidate passed, within the reported plan and bounds. |
| 1 | At least one verified failure. Replay and minimize return 1 for a reproduced failure. |
| 2 | Inconclusive or invalid invocation. Examples include zero cuts, a truncated or sampled plan, a failed clean control, worker errors, a runtime mismatch, or inconsistent replay evidence. |

The CLI prints a JSON summary on standard output. A verified failure takes precedence over a partial exploration.

## Failure bundles and replay

Each failure bundle stores the untouched raw fixture, scenario source, seed, cut or model recipe, history, fingerprints, expected check ID and failure class, the CrashCheck, Python and SQLite versions, the effective configuration and the marker and operation context. Modeled bundles also store the adapter, the base files, the trace and blobs, and candidate effect counts when the adapter reports them. The format is version 1; fields are additive and older v1 bundles still replay.

Replay checks the bundle version, that the SQLite version matches the bundle (override with `--ignore-runtime`), the raw and scenario hashes, then verifies in a new worker. A mismatch is retried once and classified `flaky` (the two fresh outcomes differ) or `mismatch`; both outcomes are returned and the exit is 2. `--regenerate` rebuilds a modeled candidate from base, trace and recipe in a fresh worker and requires the same fingerprint before verifying.

Replay treats bundles as untrusted input: it rejects links and oversized files and never follows a path from inside a bundle. **Scenario and adapter code in a bundle is still executed**, so only replay bundles whose code you trust.

### Minimization

`crashcheck minimize` first reproduces the original, then asks the adapter for smaller recipes. A reduction is kept only if it materializes legally and reproduces the same check ID and failure class. The original and minimized bundles are saved side by side under `--out`. Process-mode bundles have no recipes to reduce.

## Storage adapter contract

Pass `--adapter path/to/adapter.py` (or install a model under `crashcheck/models`) to explore modeled storage candidates. The storage engineer supplies this module and its SQLite recorder and materializer. The backend calls these hooks in worker processes and uses the same verifier, reports and bundles as process mode.

| Hook | Responsibility |
| --- | --- |
| `record(scenario, ctx, trace)` | Record the scenario workload on a private clone. Store the trace and blobs in the run's trace directory. Reject unsupported settings and invalid captures with an exception. Use `ctx.begin` and `ctx.acknowledge` so the control run knows the final history. |
| `recipes(trace, seed)` | Yield JSON-serializable model recipes in deterministic order. The scheduler reads up to the candidate limit plus one item to detect truncation. |
| `materialize(base, trace, recipe, dest)` | Treat base and trace as read-only. Write the complete raw file set to the empty destination. Return a mapping with `cut` and `history`, and optionally `effects` (per-file effect counts), as below. |
| `reductions(trace, recipe)` (optional) | Yield strictly smaller legal recipes for minimization. |

```json
{"cut": 7, "history": {"begun": [0, 1], "acknowledged": [0]}, "effects": {"app.sqlite": 3}}
```

The backend computes the fingerprint from the files it actually verifies. Storage-model qualification and independent QA oracles belong to their respective modules.
