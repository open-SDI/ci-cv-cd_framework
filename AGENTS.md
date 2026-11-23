# Repository Guide

## Boundaries

- `integration/` is the supported Python project and owns the repository verification command. Run its commands from the directory stated below.
- `CI/` is an explicitly unsupported workspace. The implemented composition adapter lives in `integration/` as `sdi-composition-adapter`; the architecture in `CI/APSEC_2025_Tools.pdf` is design context, not implemented code.
- `service-repository/` is the SDI service repository: one `<service_id>/` service folder per mobility service, holding its `SDI.md` description and the recipes that build its artifacts. CI reads it as a supplied read-only path and never writes it.
- The Year-1 plugin and prototype were retired. Git history is their archive; do not restore their interfaces or artifact formats.

## Engineering Choices

- Prefer mature, actively maintained, purpose-built libraries for standard capabilities such as parsing and validation when they satisfy the project contract. Assess compatibility, security posture, licensing, and maintenance, then lock the selected version exactly.
- Build custom infrastructure only when no suitable library exists or project-specific semantics require it, and record that rationale in the implementing issue or code review.
- Contracts are unreleased during development; change schemas in place without bumping versions.

## Pipeline Integration Scaffold

- `integration/` requires Python `>=3.12,<3.13`, pins CPython 3.12.13 for development, and uses its own exact `uv.lock`.
- Run the deterministic repository checks from any directory with `integration/scripts/verify`. It performs frozen environment setup, generated-schema, Stage-profile, `Jenkinsfile`, and Fixture-case freshness, Ruff formatting and linting, Basedpyright, pytest, and the ephemeral Jenkins stack smoke check (controller, five agents, builder, pipeline registry, CV engine, and the handoff-runner image) on the supported Docker host.
- Full verify always runs the smoke check on `main` and on a branch with no commits beyond `origin/main`. On a branch with its own commits, it skips the smoke check when no changed, staged, or untracked file since the merge base is a smoke input. The `smoke_inputs` pattern in `integration/scripts/verify` owns that list, and verify prints which case applied.
- Run `integration/scripts/verify --fast` while you work. It skips the slow tests and the smoke check. Run the full `integration/scripts/verify` once before you open a pull request.
- Neither form of verify runs the `cv_engine` tests, which fly real S-04 compositions in a Docker-in-Docker engine pair, or the `k3s` tests, which run the CD adapter against a K3s stand-in cluster. `integration/README.md` says how to run them.
- Full verify takes the host lock `/tmp/sdi-host.lock` itself before the slow tests and the smoke check, so concurrent sessions on one host wait in turn. A `flock /tmp/sdi-host.lock` wrapper around verify is safe, because verify reuses the locked descriptor that `flock` passes down. A wrapper that closes inherited descriptors deadlocks. Examples are `flock -o` and a Python process that holds the lock and starts verify through `subprocess`, which closes inherited descriptors by default.
- The public entry point is `uv run --project integration sdi-integration`. The CLI, committed file contracts, and language-neutral Stage-adapter process interface are the supported boundaries; internal Python modules are not compatibility contracts.
- The current scaffold validates committed inputs and executes four Stages locally or through the protected GitHub workflow and thin root Jenkins pipeline on five isolated inbound agents. Composition is implemented: it calls the default generator, or the one `--generator` names, of the committed composition config (`integration/composition-config.yaml`) over the supplied service repository. The config's `qwen` generator is self-hosted for cheap live tests only. Image-build is implemented on Jenkins, where `sdi-image-build-adapter` builds one image per placement and pushes it to the stack-local pipeline registry. `dispatch-local` still runs the image-build Fixture. CV is implemented: `sdi-cv-adapter` runs each composition from its images in one headless Gazebo scenario in the stack's CV engine and publishes `validation-evidence.json`, plus `sim-recording.mp4`, an overview video of the run, when the recording succeeds. Locally, CV never runs, because its inputs come from the image-build Fixture. CD is implemented on Jenkins, where `sdi-cd-adapter` deploys one combination on the stack's K3s stand-in cluster and publishes `deployment-result.json`. `dispatch-local` still runs the CD Fixture, which publishes no evidence.
- The composition live tests call a real model. Keep keys in the environment or the ignored root `.env`; never print, write, or commit them.
- `integration/src/sdi_pipeline_integration/_stage_registry.py` owns Stage wiring. One `Stage` row per Stage holds its grant, display name, agent, Jenkins and local descriptors, and credentials, and `AGENTS` and `RUN_LIMITS` sit beside the rows. Change a Stage fact there first, then regenerate with `uv run --project integration sdi-integration jenkinsfile --write --jenkinsfile Jenkinsfile` and `uv run --project integration sdi-integration stage-profiles --write --directory integration/stage-profiles` from the repository root. JCasC, Compose, `deployment/jenkins/bin/smoke-check`, and `deployment/jenkins/bin/stack` keep hand-kept copies of some of those facts; update each to match. `integration/tests/test_stage_wiring.py` fails until they match.

## Rules And Their Checks

Each rule has a check that fails when a change breaks it. When the operator corrects a repeated mistake, add the rule and its check in the same change, and prefer structure, types, or a lint whose message names the fix over prose. Drop a rule once its mistake cannot happen.

| Rule | Check |
| --- | --- |
| Importing `_cli` loads no `openai`; import it only inside the function that builds a client. | `integration/tests/test_import_cost.py` |
| Importing `_cli` loads no `docker`; import it only inside the function that builds a client. | `integration/tests/test_import_cost.py` |
| Workflows under `.github/workflows/` pass actionlint. | `integration/tests/test_workflow_lint.py` |
| `Jenkinsfile` never uses `scm`; select the repository by `env.JOB_NAME` as `checkoutSubmittedCommit` does. | `test_committed_jenkinsfile_does_not_use_scm` in `integration/tests/test_jenkinsfile_lint.py` |
| `Jenkinsfile` calls only `sdi-integration` operations the CLI defines. | `test_committed_jenkinsfile_calls_only_defined_operations` in `integration/tests/test_jenkinsfile_lint.py` |
| Each Stage's descriptors name that Stage, and each Stage runs on its own agent. | `integration/tests/test_stage_registry.py` |
| The `Jenkinsfile` Stage block is generated from the Stage registry; never edit it by hand. | `sdi-integration jenkinsfile --check` in `integration/scripts/verify` |
| Hand-written Groovy outside the generated block names no Stage, agent label, or `adapters/` path. | `test_hand_written_groovy_names_no_stage_fact` in `integration/tests/test_jenkinsfile_lint.py` |
| JCasC nodes and credential ids, Compose agent services, smoke-check agents, the `bin/stack` adapter `--stage` order and agent image variables, and the workflow `timeout-minutes` match the Stage registry. Job DSL and smoke-check parameters match `HANDOFF_PARAMETERS` in `_handoff_contracts.py`. `Jenkinsfile` and smoke-check job names match the Job DSL. | `integration/tests/test_stage_wiring.py` |
| The `Jenkinsfile` binds a Stage's credentials only from its registry row. | `test_root_pipeline_passes_each_stage_its_credentials` in `integration/tests/test_jenkins_controller.py` |
| An agent image whose baked integration code differs from the checkout fails with the typed stale-image reason before any flag is parsed. | `test_stale_image_fails_typed_before_any_flag_is_parsed` and `test_root_pipeline_reports_stale_agent_images_as_their_typed_reason` in `integration/tests/test_jenkins_controller.py` |
| Run and Stage work limits come only from the Stage registry; `bin/stack` refuses a configuration that sets `JENKINS_*_LIMIT_SECONDS`. | `test_only_the_stack_refusal_names_a_limit_setting` and `test_stack_validate_uses_compose_and_rejects_insecure_secret_files` in `integration/tests/test_jenkins_controller.py` |
| `--work-limit-seconds` outside the Stage's minimum (1 s, or 220 s for CD) to its grant is refused before any attempt; without it, a Stage gets its full grant. | `test_a_work_limit_outside_the_grant_claims_no_attempt`, `test_a_cd_work_limit_too_short_for_its_node_wait_claims_no_attempt`, and `test_a_stage_without_a_work_limit_gets_its_full_grant` in `integration/tests/test_jenkins_pipeline.py` |
| `bin/stack --prebuilt` reuses only images that `bin/stack build` built from the current checkout. | `test_prebuilt_actions_require_a_build_of_this_checkout` in `integration/tests/test_jenkins_controller.py` |
| Full verify serializes host Docker work on `/tmp/sdi-host.lock` and does not deadlock under a `flock` wrapper. | `test_verification_waits_while_another_process_holds_the_host_lock` and `test_a_caller_holding_the_host_lock_does_not_deadlock_verification` in `integration/tests/test_repository_acceptance.py` |
| Each Domain adapter defines `main = serve(stage, body)` from `_adapter_kit` and refuses wrong-Stage, oversized, and malformed requests. | `integration/tests/test_adapter_entry_points.py` and `integration/tests/test_adapter_conformance.py` |
| Per-Stage document checks live in `CHECKS` in `_stage_documents.py`, one `match` case per Stage. | Basedpyright, through `assert_never` in `_stage_check` |
| CV's topic constant lives only in `TOPIC_CONSTANT` in `_cv_verdict.py`, and it equals the topics the sim's machinery nodes publish. | `integration/tests/test_cv_topic_constant.py` |
| Verify prints Stage status from `sdi-integration stage-status`, never from hand-written text. | `integration/tests/test_repository_acceptance.py` |
| Markdown names only repository paths that exist or that the repository's `.gitignore` files ignore. It checks every relative or `/`-rooted link target, and each code span holding a `/` whose first segment exists in the document's directory or a directory above it, or that ends in `.md`, after dropping a trailing `:N` or `:N-M`. It reads only link targets and code spans, so paths in fenced blocks go unchecked. | `integration/tests/test_doc_paths.py` |

## Change And Review Workflow

- Treat one implementation issue as one review unit and start it from the latest merged `main`.
- Implement and verify only that issue, review the complete diff, and open one pull request against `main`.
- Keep issue, commit, branch, and pull-request references consistent with the repository's configured tracker and review policy.
- Begin blocked work only after its blocker is merged.

## Generated And Local Files

- `.gitignore` excludes Python caches and environments, generated artifacts, workspaces, recovery artifacts, local deployment configuration, and runtime secrets without hiding committed schemas or Fixture cases.
- The active root `Jenkinsfile` schedules only public integration CLI operations across the five agents. `.github/workflows/pipeline-integration.yml` is the protected external trigger, progress, summary, and artifact-retrieval surface.
- Check `git status` after commands and keep machine-specific values, secrets, generated evidence, and recovery state out of commits.
