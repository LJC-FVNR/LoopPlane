# Project Plan

<!-- Generated from LoopPlane workflow template research-topic-exploration@0.1.0. -->

## Metadata

- workflow_id: {{workflow_id}}
- workflow_title: Research {{topic}}
- plan_version: 1
- generated_from: {{brief_file}}
- generated_at: {{generated_at}}
- active: false

## Configured Paths

{{workflow_path_lines}}

## Authority And Completion Rules

`{{plan_file}}` is authoritative only after activation. Completion requires all
tasks to be validated, all objective gates to be satisfied, and the final report
to exist at `{{final_report_path}}`.

## Phase P0: Scope And Acceptance Contract

- [ ] P0.T001: Define research scope for {{topic}}
  - acceptance: Scope, assumptions, success criteria, and non-goals are documented.
  - evidence: {{results_dir}}/P0.T001/
  - latest: {{results_dir}}/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: {{results_dir}}/P0.T001/report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Scope note with target standard {{target_standard}}.

### Phase Objective Checklist

- [ ] `P0.O001` Research scope is explicit enough to guide bounded exploration.
  - evidence_scope: {{results_dir}}/P0.T001/
  - judgment_guidance: Confirm the scope names what will and will not be claimed.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## Phase P1: Baseline And Prior Context

- [ ] P1.T001: Establish baseline context for {{topic}}
  - acceptance: Baselines, related approaches, and known limitations are summarized.
  - evidence: {{results_dir}}/P1.T001/
  - latest: {{results_dir}}/P1.T001/latest.json
  - depends_on: [P0.T001]
  - risk: medium
  - validation: file_exists: {{results_dir}}/P1.T001/report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Baseline context summary.

<!-- loopplane:if enable_literature_review -->
- [ ] P1.T002: Perform focused literature and reference review
  - acceptance: Relevant references are collected with short notes on applicability.
  - evidence: {{results_dir}}/P1.T002/
  - latest: {{results_dir}}/P1.T002/latest.json
  - depends_on: [P1.T001]
  - risk: medium
  - validation: file_exists: {{results_dir}}/P1.T002/report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Reference review notes.
<!-- loopplane:endif -->

### Phase Objective Checklist

- [ ] `P1.O001` Baseline context is sufficient to interpret later evidence.
  - evidence_scope: {{results_dir}}/
  - judgment_guidance: Confirm baseline claims are sourced and limitations are visible.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## Phase P2: Experiment Matrix

- [ ] P2.T001: Design experiment matrix for {{topic}}
  - acceptance: Experiment dimensions, success metrics, and stopping criteria are documented.
  - evidence: {{results_dir}}/P2.T001/
  - latest: {{results_dir}}/P2.T001/latest.json
  - depends_on: [P1.T001]
  - risk: medium
  - validation: file_exists: {{results_dir}}/P2.T001/report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Experiment matrix and execution checklist.

### Phase Objective Checklist

- [ ] `P2.O001` Experiment plan can produce interpretable evidence within the run budget.
  - evidence_scope: {{results_dir}}/P2.T001/
  - judgment_guidance: Confirm metrics and stopping criteria are unambiguous.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## Phase P3: Execution And Evidence Collection

- [ ] P3.T001: Execute bounded experiments for {{topic}}
  - acceptance: Experiment results are captured with commands, artifacts, and observed outcomes.
  - evidence: {{results_dir}}/P3.T001/
  - latest: {{results_dir}}/P3.T001/latest.json
  - depends_on: [P2.T001]
  - risk: medium
  - validation: file_exists: {{results_dir}}/P3.T001/report.md
  - max_attempts: 5
  - approval: not_required
  - deliverables: Experiment evidence package.

### Phase Objective Checklist

- [ ] `P3.O001` Evidence is sufficient to support or reject the main claims.
  - evidence_scope: {{results_dir}}/P3.T001/
  - judgment_guidance: Confirm results are reproducible enough for {{target_standard}}.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

<!-- loopplane:if enable_ablation -->
## Phase P4: Ablation And Robustness

- [ ] P4.T001: Run ablation and robustness checks
  - acceptance: Ablations identify which factors materially affect the conclusions.
  - evidence: {{results_dir}}/P4.T001/
  - latest: {{results_dir}}/P4.T001/latest.json
  - depends_on: [P3.T001]
  - risk: medium
  - validation: file_exists: {{results_dir}}/P4.T001/report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Ablation summary.

### Phase Objective Checklist

- [ ] `P4.O001` Robustness checks reduce obvious alternative explanations.
  - evidence_scope: {{results_dir}}/P4.T001/
  - judgment_guidance: Confirm ablation evidence changes or strengthens the claims.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100
<!-- loopplane:endif -->

## Phase P5: Report And Final Gate

- [ ] P5.T001: Write final research report
  - acceptance: Final report integrates scope, baseline, experiments, analysis, limitations, and next steps.
  - evidence: {{results_dir}}/P5.T001/
  - latest: {{results_dir}}/P5.T001/latest.json
  - depends_on: [P3.T001]
  - risk: medium
  - validation: file_exists: {{final_report_path}}
  - max_attempts: 3
  - approval: not_required
  - deliverables: Final report at {{final_report_path}}.

### Phase Objective Checklist

- [ ] `P5.O001` Final report meets the requested target standard.
  - evidence_scope: {{final_report_path}}
  - judgment_guidance: Judge substance, evidence support, caveats, and reproducibility.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100

## Final Objective Checklist

- [ ] `F.O001` {{topic}} has been explored to {{target_standard}} quality with evidence-backed conclusions.
  - evidence_scope: {{results_dir}}/
  - judgment_guidance: Confirm no major claim lacks supporting evidence and the final report exists.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 100
