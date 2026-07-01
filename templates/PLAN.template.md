# Project Plan

## Metadata

- workflow_id: {{workflow_id}}
- workflow_title: Semantic Workflow Title
- plan_version: {{plan_version}}
- generated_from: {{brief_file}}
- generated_at: {{generated_at}}
- active: {{active}}

## Configured Paths

- brief_file: {{brief_file}}
- plan_file: {{plan_file}}
- shared_context_file: {{shared_context_file}}
- results_dir: {{results_dir}}
- runtime_dir: {{runtime_dir}}
- read_models_dir: {{read_models_dir}}
- requests_dir: {{requests_dir}}
- planning_dir: {{planning_dir}}
- version_control_config_file: {{version_control_config_file}}

All stored paths should be project-root-relative POSIX-style paths. Use the
configured variables above rather than hard-coded workflow paths.

## Authority And Completion Rules

`{{plan_file}}` is the authoritative active execution plan after activation.
Prompts, generated queues, read models, dashboards, and worker self-claims are
derived and non-authoritative.

Completion requires:

- no unresolved `[ ]`, `[~]`, or `[!]` tasks in active scope;
- each phase objective checklist is closed by agentic objective verification
  after that phase's tasks reach terminal state;
- the final objective checklist is closed by fresh agentic objective
  verification after all phases reach terminal state;
- every `[x]` task has authoritative validation;
- every `[-]` skipped task has explicit skip reason and authorization;
- all required final deliverables exist;
- no unrecovered failures remain;
- no active background jobs or leases remain;
- final verification gates pass, including semantic final reviewer judgment
  when configured.

## Task Checkbox Semantics

```text
[x] done with authoritative evidence and validation
[ ] not done
[~] partial and still unfinished unless explicitly accepted
[!] blocked and unresolved
[-] skipped only if explicitly approved or out of scope by contract
```

## Objective Checklist Semantics

Objectives are high-level dynamic acceptance gates, not executable tasks. Put
phase objectives after the tasks in that phase, under
`### Phase Objective Checklist`. Put workflow-level objectives near the end,
under `## Final Objective Checklist`.

```text
[x] satisfied by objective verifier
[ ] not yet verified or not yet satisfied
[~] self-expansion follow-up is in progress
[!] unresolved after bounded self-expansion
[-] waived only with explicit policy reason
```

Each objective should describe a high-level delivery outcome. Do not encode
every implementation detail as an objective. Include `evidence_scope`,
`judgment_guidance`, `verifier: objective_verifier`, `unmet_action:
self_expand`, and `max_expansions`.

## Required Task Fields

Each active task must include:

- task ID and title;
- checkbox status;
- acceptance criteria;
- evidence root;
- latest pointer path;
- dependencies;
- risk level;
- validation strategy;
- retry budget.

Skipped tasks additionally require `skip_reason` and `skip_authorization` or
`approval_id`. Blocked tasks additionally require `blocked_reason`,
`blocked_since` or `detected_at`, and `unblock_condition`.

## Phase {{phase_id}}: {{phase_title}}

- [ ] {{task_id}}: {{task_title}}
  - acceptance: {{task_acceptance}}
  - evidence: {{results_dir}}/{{task_id}}/
  - latest: {{results_dir}}/{{task_id}}/latest.json
  - depends_on: {{task_dependencies}}
  - risk: {{task_risk}}
  - validation: {{task_validation_strategy}}
  - max_attempts: {{task_max_attempts}}

### Phase Objective Checklist

- [ ] `{{phase_objective_id}}` {{phase_objective_high_level_outcome}}
  - evidence_scope: {{results_dir}}/
  - judgment_guidance: {{phase_objective_judgment_guidance}}
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## Final Objective Checklist

- [ ] `{{final_objective_id}}` {{final_objective_high_level_outcome}}
  - evidence_scope: {{results_dir}}/
  - judgment_guidance: {{final_objective_judgment_guidance}}
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## State Metadata Patterns

Use these fields only when the corresponding state applies.

Completed task:

```markdown
- [x] {{completed_task_id}}: {{completed_task_title}}
  - acceptance: {{completed_task_acceptance}}
  - evidence: {{results_dir}}/{{completed_task_id}}/
  - latest: {{results_dir}}/{{completed_task_id}}/latest.json
  - depends_on: {{completed_task_dependencies}}
  - risk: {{completed_task_risk}}
  - validation: {{completed_task_validation_strategy}}
  - max_attempts: {{completed_task_max_attempts}}
  - completed_by: {{completed_task_run_id}}
```

Partial task:

```markdown
- [~] {{partial_task_id}}: {{partial_task_title}}
  - acceptance: {{partial_task_acceptance}}
  - evidence: {{results_dir}}/{{partial_task_id}}/
  - latest: {{results_dir}}/{{partial_task_id}}/latest.json
  - depends_on: {{partial_task_dependencies}}
  - risk: {{partial_task_risk}}
  - validation: {{partial_task_validation_strategy}}
  - max_attempts: {{partial_task_max_attempts}}
  - partial_reason: {{partial_reason}}
  - unresolved: true
```

Blocked task:

```markdown
- [!] {{blocked_task_id}}: {{blocked_task_title}}
  - acceptance: {{blocked_task_acceptance}}
  - evidence: {{results_dir}}/{{blocked_task_id}}/
  - latest: {{results_dir}}/{{blocked_task_id}}/latest.json
  - depends_on: {{blocked_task_dependencies}}
  - risk: {{blocked_task_risk}}
  - validation: {{blocked_task_validation_strategy}}
  - max_attempts: {{blocked_task_max_attempts}}
  - blocked_reason: {{blocked_reason}}
  - blocked_since: {{blocked_since}}
  - unblock_condition: {{unblock_condition}}
```

Skipped task:

```markdown
- [-] {{skipped_task_id}}: {{skipped_task_title}}
  - acceptance: {{skipped_task_acceptance}}
  - evidence: {{results_dir}}/{{skipped_task_id}}/
  - latest: {{results_dir}}/{{skipped_task_id}}/latest.json
  - depends_on: {{skipped_task_dependencies}}
  - risk: {{skipped_task_risk}}
  - validation: {{skipped_task_validation_strategy}}
  - max_attempts: {{skipped_task_max_attempts}}
  - skip_reason: {{skip_reason}}
  - skip_authorization: {{skip_authorization}}
```
