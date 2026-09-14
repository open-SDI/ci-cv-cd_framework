![header](https://capsule-render.vercel.app/api?type=waving&height=170&color=gradient&text=SDI%20CI/CV/CD%20Pipeline%20Framework&textBg=false&fontSize=40&fontColor=000000&descAlignY=50&fontAlignY=30)


**Table of contents**
- [1. Introduction](#1-introduction)
- [2. Design](#2-design)
- [3. Scenario](#3-scenario)
- [4. Manual](#4-manual)
- [5. Pipeline Integration Scaffold](#5-pipeline-integration-scaffold)

> [!IMPORTANT]
> Sections 1-4 preserve the original project description and references. The
> currently supported implementation is described in section 5. Its four Stages
> run deterministic Fixtures; they do not implement or prove real service
> integration, image publication, simulation Validation, deployment, or KPI
> results.


## 1. Introduction
This is a CI/CV/CD pipeline framework for software-defined future mobility.

Main features of this pipeline framework is:
* **Continuous Integration (CI)**: Automated service integration of AI-enabled mobility service (e.g., ROS2, Autoware)
![CI Flow](assets/CI_main.png)
* **Continuous Validation (CV)**: Simulation(e.g., Gazebo, CARLA)-based virtual validation of autonomous driving software
![Gazebo Simulation](assets/gazebo_office_world.png)
* **Continuous Deployment (CD)**: Split deployment of moiblity software to mobility device and infrastrucutre (e.g., edge or cloud servers)

For more details please check the APSEC'25 Tools Paper - [OrchestML](/CI/APSEC_2025_Tools.pdf).

## 2. Design

[Software Requirement Specification](https://docs.google.com/spreadsheets/d/1P-EfpCEkrHRfhBJHL3unYKW5okFbLe2h5jsJ6gXnRrw/edit?usp=sharing)

[Software Designs (Models)](https://drive.google.com/drive/folders/1rNpvV7xWhPPySddRkV-D2rOdhiFWtSDM?usp=drive_link)
- Component diagram
- Sequence diagram

## 3. Scenario
![Year 2 Scenario](assets/year2-scenario.png)


## 4. Manual
For more details regarding the usage of the CI tool, please visit the original repo of OrchestML [here](https://github.com/gurkhaman/OrchestML).

## 5. Pipeline Integration Scaffold

This repository's supported entry point is the production integration scaffold
under `integration/`. It coordinates the CI, image-build, CV, and CD Stage
interfaces for Software-Defined Mobility while keeping those Domain capabilities
independently replaceable.

The current scaffold validates and identifies committed inputs and executes one
deterministic four-Stage Fixture run locally or through the protected GitHub
workflow and private five-agent Jenkins pipeline. GitHub is the external
trigger, progress, summary, and artifact-retrieval surface. Both execution paths
use the permanent Stage-adapter process interface and
validate the complete archive candidate. Fixture output is not Validation
evidence, Domain success, KPI evidence, or proof that a real composition, build,
simulation, or deployment capability exists.

The repository also reconstructs an on-demand, zero-executor Jenkins controller
and five isolated one-executor inbound agents from immutable image inputs,
Configuration as Code, and Job DSL. The live smoke check proves their WebSocket
connections, labels, isolation, disposable workspaces, and configuration
convergence. The root `Jenkinsfile` is a thin scheduler over public
`sdi-integration` operations; Python owns all contract construction and
interpretation.

For clean installation on an ETRI-managed host, follow the
[ETRI installation runbook](docs/etri-installation-runbook.md). Detailed trigger,
operation, maintenance, and readiness guidance is indexed under [`docs/`](docs/).

### Development

Install [`uv`](https://docs.astral.sh/uv/) and run the deterministic baseline
verification command from any directory:

```sh
integration/scripts/verify
```

The command installs the exact locked Python 3.12 environment; checks schemas,
the public CLI, Ruff, and Basedpyright; and runs the ephemeral Jenkins stack
smoke check on the supported Docker host. The installed command is:

```sh
uv run --project integration sdi-integration --help
```

### Repository Areas

- `integration/`: the independently owned Python package, contracts, Stage
  profiles, Fixtures, tests, and verification entry point.
- `requirements/`, `profiles/`, and `runs/`: version-controlled, dispatchable
  inputs introduced by the run-contract implementation.
- `deployment/jenkins/`: the pinned zero-executor Jenkins controller, five
  isolated inbound agents, exact plugin lock, configuration, fixed job, and
  lifecycle interface.
- `deployment/github-runner/`: the pinned low-privilege user-service interface
  for the handoff-only self-hosted runner.
- `.github/workflows/`: the protected path-only Pipeline integration workflow.
- `docs/`: role-neutral setup, operation, maintenance, and readiness guidance.
- `CV/gazebo/`: retained Gazebo source assets. They are not connected to the
  integration scaffold.
- `CI/`: an unsupported workspace reserved for a possible future composition
  adapter. It is not a supported entry point.

The Year-1 implementation and its artifact formats were retired rather than
carried forward. Git history remains the archive.
