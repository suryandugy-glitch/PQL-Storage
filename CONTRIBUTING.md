# Contributing to CrashCheck

Thank you for helping make CrashCheck's failure reports reproducible and trustworthy.

## Development setup

- Python 3.11 or later; Linux is the reference platform.
- The core test harness uses Python's standard library.
- For storage-model work, use the `feat/storage-engineering` branch and read its model limitations before interpreting results.

Run the test suite from the repository root:

```sh
python -m unittest discover -s tests -v
python -m compileall -q crashcheck examples tests
```

## Before opening a pull request

1. Keep changes focused and explain the failure mode they address.
2. Add a regression test for a bug fix or behavior change.
3. Preserve deterministic seeds and replayable failure bundles.
4. Run the unit tests and include the command and result in your PR.
5. Describe the platform, Python and SQLite versions and whether the run used process or modeled mode.

## Reporting bugs

Please include the CLI command, expected and observed exit codes, a minimal trusted scenario, the seed and relevant output. Remove credentials and private database contents before uploading bundles. **Do not run a failure bundle from an untrusted source:** replay executes the scenario and possibly adapter code inside it.

## Correctness expectations

Distinguish an application invariant violation from a harness error. A budget-limited or sampled exploration is inconclusive and should not be presented as proof that all crash states are safe. Process termination does not simulate sudden power loss; modeled storage results apply only within the stated model assumptions.

## Review guidelines

Favor straightforward code and explicit invariants over clever behavior. Keep compatibility with Python 3.11 where possible, avoid introducing dependencies without a reason, and document any new result format or CLI option. Please do not commit generated run directories, private databases, or API keys.
