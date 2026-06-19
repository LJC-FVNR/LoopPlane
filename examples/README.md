# LoopPlane Examples

The examples are local smoke fixtures for the standalone CLI runtime.

- `minimal_project/` contains a complete local smoke path. It uses the noop
  adapter for planner/auditor checks, then a tiny shell worker for an executable
  scheduler run, validation, reconciliation, dashboard generation, health, Git
  doctor, and final verification.
- `python_project/` and `research_project/` are scenario notes for adapting the
  same runtime flow to larger projects.

Run the minimal example from the repository root; its README contains the exact
commands.
