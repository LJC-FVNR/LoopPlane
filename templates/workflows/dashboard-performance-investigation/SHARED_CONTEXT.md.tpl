# Shared Context

## Objective

Improve dashboard/read-model loading for `{{target_workflow}}` while preserving
read-only dashboard authority boundaries.

## Workflow Paths

{{workflow_path_lines}}

## Benchmark Command

```bash
{{benchmark_command}}
```

## Authority

`{{plan_file}}` is authoritative after activation. Dashboard GET paths must not
mutate workflow truth, runtime state, event logs, or read models except through
explicit control or rebuild requests.

