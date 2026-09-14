# Repository Guide

## Boundaries

- `integration/` is the supported Python project and owns the repository verification command. Run its commands from the directory stated below.
- `CI/` is an explicitly unsupported workspace reserved for a future composition adapter. The architecture in `CI/APSEC_2025_Tools.pdf` is design context, not implemented code.
- The Year-1 plugin and prototype were retired. Git history is their archive; do not restore their interfaces or artifact formats.
- Most tracked files are Gazebo assets under `CV/gazebo/models/`; avoid repository-wide formatting or generated-file rewrites there.

## Engineering Choices

- Prefer mature, actively maintained, purpose-built libraries for standard capabilities such as parsing and validation when they satisfy the project contract. Assess compatibility, security posture, licensing, and maintenance, then lock the selected version exactly.
- Build custom infrastructure only when no suitable library exists or project-specific semantics require it, and record that rationale in the implementing issue or code review.

## Pipeline Integration Scaffold

- `integration/` requires Python `>=3.12,<3.13`, pins CPython 3.12.13 for development, and uses its own exact `uv.lock`.
- Run the deterministic repository checks from any directory with `integration/scripts/verify`. It performs frozen environment setup, generated-schema freshness, Ruff formatting and linting, Basedpyright, pytest, and the ephemeral six-container Jenkins stack smoke check on the supported Docker host.
- The public entry point is `uv run --project integration sdi-integration`. The CLI, committed file contracts, and language-neutral Stage-adapter process interface are the supported boundaries; internal Python modules are not compatibility contracts.
- The current scaffold validates committed inputs and executes the four-Stage Fixture locally or through the protected GitHub workflow and thin root Jenkins pipeline on five isolated inbound agents. Real Domain adapters remain deferred to dedicated implementation work.

## Change And Review Workflow

- Treat one implementation issue as one review unit and start it from the latest merged `main`.
- Implement and verify only that issue, review the complete diff, and open one pull request against `main`.
- Keep issue, commit, branch, and pull-request references consistent with the repository's configured tracker and review policy.
- Begin blocked work only after its blocker is merged.

## CV Assets

- Gazebo files are source assets for an external TurtleBot3 workspace. Copy the world, model directories, and launch file into `turtlebot3_gazebo`, then run `colcon build --symlink-install` from `turtlebot3_ws` before `ros2 launch turtlebot3_gazebo office.launch.py`.
- The Gazebo assets are not wired into the pipeline integration scaffold and provide no automated scenario, oracle, or Validation evidence.
- `CV/gazebo/world_parser.py` executes on import. Run it only from `CV/gazebo`; it copies model trees into the ignored `_models/` directory.

## Generated And Local Files

- `.gitignore` excludes Python caches and environments, generated artifacts, workspaces, recovery artifacts, local deployment configuration, runtime secrets, and Gazebo parser output without hiding committed schemas or Fixture cases.
- The active root `Jenkinsfile` schedules only public integration CLI operations across the five agents. `.github/workflows/pipeline-integration.yml` is the protected external trigger, progress, summary, and artifact-retrieval surface.
- Check `git status` after commands and keep machine-specific values, secrets, generated evidence, and recovery state out of commits.
