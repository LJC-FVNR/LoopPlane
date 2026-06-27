# Minimal Project Example

This example exercises LoopPlane without requiring an external coding-agent CLI.
The planner and auditor use the noop adapter. The executable task uses the
shell adapter and `worker.py`, which writes deterministic evidence for one
task. The example then writes deterministic objective verification reports and
disables the optional semantic final reviewer so the final deterministic gates
can pass without external agent credentials.

Run from the repository root:

```bash
PROJECT="$(mktemp -d /tmp/loopplane-minimal-example.XXXXXX)"
export LOOPPLANE_HOME="$(mktemp -d /tmp/loopplane-home.XXXXXX)"
python3 scripts/loopplane init --project "$PROJECT" --brief "Run the minimal LoopPlane example."
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner planner --role planner --adapter noop --command noop
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner auditor --role auditor --adapter noop --command noop
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner planner
python3 scripts/loopplane plan --project "$PROJECT"
python3 scripts/loopplane audit-plan --project "$PROJECT"
python3 scripts/loopplane activate-plan --project "$PROJECT"
python3 examples/minimal_project/write_smoke_plan.py "$PROJECT"
WORKER="$(pwd)/examples/minimal_project/worker.py"
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner worker --role worker --adapter shell --command "python3 $WORKER"
python3 scripts/loopplane preview --project "$PROJECT"
python3 scripts/loopplane run --project "$PROJECT" --max-ticks 1
RUN_DIR=$(find "$PROJECT/.loopplane" -path '*/results/T001/runs/run_*' -type d -name 'run_*' | sort | tail -n 1)
python3 scripts/loopplane validate --project "$PROJECT" --task T001 --run-dir "$RUN_DIR"
python3 scripts/loopplane reconcile --project "$PROJECT" --task T001 --run-dir "$RUN_DIR"
python3 examples/minimal_project/write_smoke_objective_reports.py "$PROJECT"
python3 scripts/loopplane rebuild-read-models --project "$PROJECT"
python3 scripts/loopplane dashboard --project "$PROJECT" --rebuild-read-models
python3 scripts/loopplane health --project "$PROJECT"
python3 scripts/loopplane vc doctor --project "$PROJECT"
python3 scripts/loopplane final-verify --project "$PROJECT"
```

Expected final result:

- `validate` reports `pass`.
- `reconcile` marks `T001` complete in `PLAN.md`.
- `health` reports `healthy`.
- `final-verify` reports `pass` and writes
  `.loopplane/runtime/plan_loop_complete.json`.
