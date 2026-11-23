import org.jenkinsci.plugins.workflow.steps.FlowInterruptedException
import org.jenkinsci.plugins.workflow.steps.TimeoutStepExecution


// BEGIN GENERATED STAGE WIRING
// Rendered from _stage_registry.py. After editing the registry, run this
// from the repository root: uv run --project integration sdi-integration jenkinsfile --write --jenkinsfile Jenkinsfile
def integrationLabel = 'integration'
def runLimitSeconds = 5400
def stages = [
    [name: 'Composition', stage: 'composition', label: 'composition',
     safetyTimeoutSeconds: 660,
     credentials: [[id: 'sdi-openai-api-key', variable: 'OPENAI_API_KEY']]],
    [name: 'Image build', stage: 'image_build', label: 'image-build',
     safetyTimeoutSeconds: 1860,
     credentials: []],
    [name: 'Continuous Validation', stage: 'cv', label: 'cv',
     safetyTimeoutSeconds: 2760,
     credentials: []],
    [name: 'Continuous Deployment', stage: 'cd', label: 'cd',
     safetyTimeoutSeconds: 960,
     credentials: []],
]
// END GENERATED STAGE WIRING

def isTimeoutInterruption = { interruption ->
    interruption.causes.any { cause ->
        cause instanceof TimeoutStepExecution.ExceededTimeout
    }
}

// Each job clones its own repository, so the protected-main ancestry check runs
// against the repository that submitted the run. The job names match
// deployment/jenkins/jobs/pipeline-integration.groovy; this installation does
// not bind the scm variable, so the URL is selected by job name.
//
// The clone keeps full protected-main commit history for the ancestry check but
// fetches only the submitted commit's blobs. The CLI reads committed blobs with
// lazy fetching disabled, so it fails closed outside that set. The git plugin's
// checkout step cannot filter blobs, so plain git is used. Only the transfer is
// retried, with stalled transfers aborted; no Stage attempt is.
def checkoutSubmittedCommit = {
    def repositoryUrl = [
        'pipeline-integration': env.SDI_REPOSITORY_URL,
        'pipeline-integration-fork': env.SDI_FORK_REPOSITORY_URL,
    ][env.JOB_NAME]
    if (!repositoryUrl) {
        error("Job ${env.JOB_NAME} has no configured repository")
    }
    withEnv(["SDI_CHECKOUT_REPOSITORY_URL=${repositoryUrl}"]) {
        sh '''set -eu
# Runs one git transfer up to three times. Each attempt is killed after 300
# seconds and aborts sooner if it moves under 1000 bytes/s for 60 seconds;
# reset_attempt undoes a failed attempt's partial state before the next.
git_transfer_with_retries() {
  description=$1
  reset_attempt=$2
  shift 2
  attempt=1
  until timeout 300 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "$@"; do
    if [ "$attempt" -ge 3 ]; then
      echo "$description failed after $attempt attempts" >&2
      exit 1
    fi
    attempt=$((attempt + 1))
    "$reset_attempt"
  done
}
reset_clone() {
  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +
}
reset_checkout() {
  rm -f .git/index.lock
}
git_transfer_with_retries 'protected-main clone' reset_clone clone --filter=blob:none --no-checkout --no-tags --single-branch --branch main "$SDI_CHECKOUT_REPOSITORY_URL" .
git update-ref refs/remotes/origin/sdi-protected-main refs/remotes/origin/main
git merge-base --is-ancestor "$RESOLVED_COMMIT_SHA" refs/remotes/origin/sdi-protected-main
git_transfer_with_retries 'submitted-commit checkout' reset_checkout checkout --quiet --detach "$RESOLVED_COMMIT_SHA"
test "$(git rev-parse HEAD)" = "$RESOLVED_COMMIT_SHA"
git update-ref refs/heads/main "$RESOLVED_COMMIT_SHA"
'''
    }
}

def commonArguments = '''\
  --repository . \
  --requested-ref "$REQUESTED_GIT_REF" \
  --resolved-commit "$RESOLVED_COMMIT_SHA" \
  --run-request-path "$RUN_REQUEST_PATH" \
  --execution-id "$EXECUTION_ID"'''

// Each agent image records a digest of the integration code it bakes in. An
// image whose record is absent or empty reports `missing`. Exit status 4 means
// an agent image bakes integration code other than the submitted commit's; the
// build description lets the handoff report that as a typed failure. The check
// runs through integration-code-digest, which every image runs at build time,
// so a stale image fails typed even when it cannot parse this file's flags.
def bakedCodeDigestCommand = 'digest=$(cat /etc/sdi/integration-code-digest 2>/dev/null || true); printf %s "${digest:-missing}"'
def requireFreshAgentImage = """test "\$(${bakedCodeDigestCommand})" = "\$(sdi-integration integration-code-digest --root integration)" || {
  printf '%s\\n' 'sdi.agent.stale-image: this agent image bakes integration code other than the submitted commit' >&2
  exit 4
}"""
def failStaleAgentImages = { stageName ->
    currentBuild.description = 'sdi.agent.stale-image'
    error("${stageName} found stale agent images; rebuild them from a clean checkout")
}

def resourceNodes = [:]
def bakedCodeDigests = [:]
def acceptedStages = []
def continueChain = true
def runDeadlineExpired = false
def buildStartedAtMillis = "${currentBuild.startTimeInMillis}"
def runDeadlineEpochMillis = currentBuild.startTimeInMillis + (runLimitSeconds - 120) * 1000
def schedulingLimitSeconds = runLimitSeconds - 105
def schedulingDeadlineEpochMillis = currentBuild.startTimeInMillis + schedulingLimitSeconds * 1000

try {
timeout(time: runLimitSeconds, unit: 'SECONDS') {
    try {
        timeout(time: schedulingLimitSeconds, unit: 'SECONDS') {
    stage('Resource preflight') {
        stages.each { definition ->
            node(definition.label) {
                try {
                    deleteDir()
                    resourceNodes[definition.stage] = env.NODE_NAME
                    bakedCodeDigests[definition.stage] = sh(
                        returnStdout: true,
                        script: bakedCodeDigestCommand,
                    ).trim()
                } finally {
                    deleteDir()
                }
            }
        }
    }

    stage('Run preflight') {
        node(integrationLabel) {
            try {
                deleteDir()
                checkoutSubmittedCommit()
                def preflightEnvironment = [
                    "SDI_BUILD_STARTED_AT_MILLIS=${buildStartedAtMillis}",
                    "SDI_INTEGRATION_NODE=${env.NODE_NAME}",
                ]
                def stageArguments = []
                stages.each { definition ->
                    preflightEnvironment.add("SDI_NODE_${definition.stage}=${resourceNodes[definition.stage]}")
                    preflightEnvironment.add("SDI_DIGEST_${definition.stage}=${bakedCodeDigests[definition.stage]}")
                    stageArguments.add("--stage-node \"${definition.stage}=\$SDI_NODE_${definition.stage}\"")
                    stageArguments.add("--stage-baked-code-digest \"${definition.stage}=\$SDI_DIGEST_${definition.stage}\"")
                }
                withEnv(preflightEnvironment) {
                    def preflightStatus = sh(returnStatus: true, script: """set -eu
${requireFreshAgentImage}
sdi-integration preflight-jenkins-run \\
${commonArguments} \\
  --handoff-contract-version \"\$HANDOFF_CONTRACT_VERSION\" \\
  --github-run-id \"\$GITHUB_RUN_ID\" \\
  --github-run-attempt \"\$GITHUB_RUN_ATTEMPT\" \\
  --build-started-at-millis \"\$SDI_BUILD_STARTED_AT_MILLIS\" \\
  --integration-node \"\$SDI_INTEGRATION_NODE\" \\
  --integration-baked-code-digest \"\$(${bakedCodeDigestCommand})\" \\
  ${stageArguments.join(' ')} >/dev/null
""")
                    if (preflightStatus == 4) {
                        failStaleAgentImages('Run preflight')
                    } else if (preflightStatus != 0) {
                        error('Run preflight failed')
                    }
                }
            } finally {
                deleteDir()
            }
        }
    }

    for (definition in stages) {
        if (!continueChain) {
            break
        }
        if (System.currentTimeMillis() >= runDeadlineEpochMillis) {
            runDeadlineExpired = true
            break
        }
        stage(definition.name) {
            node(definition.label) {
                try {
                    deleteDir()
                    checkoutSubmittedCommit()
                    acceptedStages.each { acceptedStage ->
                        unstash("${EXECUTION_ID}-${acceptedStage}")
                    }
                    def priorArguments = acceptedStages.collect { acceptedStage ->
                        "--prior-attempt-root 'transfers/${acceptedStage}'"
                    }.join(' ')
                    def stageExecutionStatus = 0
                    // Jenkins selects no generator. The Domain agents reach only
                    // the OpenAI endpoint, so every Stage runs with the committed
                    // composition config's default generator.
                    def executeStage = {
                        sh(returnStatus: true, script: """set -eu
${requireFreshAgentImage}
sdi-integration execute-jenkins-stage \\
${commonArguments} \\
  --stage '${definition.stage}' \\
  --attempt-root 'transfers/${definition.stage}' \\
  ${priorArguments} \\
  --baked-code-digest \"\$(${bakedCodeDigestCommand})\" \\
  --run-deadline-epoch-millis \"\$SDI_RUN_DEADLINE_EPOCH_MILLIS\" >/dev/null
""")
                    }
                    withEnv([
                        "SDI_RUN_DEADLINE_EPOCH_MILLIS=${runDeadlineEpochMillis}",
                    ]) {
                        try {
                            timeout(time: definition.safetyTimeoutSeconds, unit: 'SECONDS') {
                                if (definition.credentials) {
                                    withCredentials(definition.credentials.collect { credential ->
                                        string(credentialsId: credential.id, variable: credential.variable)
                                    }) {
                                        stageExecutionStatus = executeStage()
                                    }
                                } else {
                                    stageExecutionStatus = executeStage()
                                }
                            }
                        } catch (FlowInterruptedException interruption) {
                            if (isTimeoutInterruption(interruption)
                                && System.currentTimeMillis() < schedulingDeadlineEpochMillis) {
                                error("${definition.name} Jenkins safety timeout expired")
                            }
                            throw interruption
                        }
                    }
                    if (stageExecutionStatus == 3) {
                        runDeadlineExpired = true
                        continueChain = false
                    } else if (stageExecutionStatus == 4) {
                        failStaleAgentImages(definition.name)
                    } else if (stageExecutionStatus != 0) {
                        error("${definition.name} Stage execution failed before acceptance")
                    } else {
                        stash(
                            name: "${EXECUTION_ID}-${definition.stage}",
                            includes: "transfers/${definition.stage}/**",
                            useDefaultExcludes: false,
                        )
                        acceptedStages.add(definition.stage)
                        def continuation = sh(
                            returnStatus: true,
                            script: """set -eu
sdi-integration attempt-allows-continuation \\
  --attempt-root 'transfers/${definition.stage}' \\
  --execution-id \"\$EXECUTION_ID\" \\
  --stage '${definition.stage}'
""",
                        )
                        if (continuation == 1) {
                            continueChain = false
                        } else if (continuation != 0) {
                            error("${definition.name} accepted-attempt transfer is invalid")
                        }
                    }
                } finally {
                    deleteDir()
                }
            }
        }
    }
        }
    } catch (FlowInterruptedException interruption) {
        if (isTimeoutInterruption(interruption)) {
            runDeadlineExpired = true
        } else {
            throw interruption
        }
    }

    stage('Finalize and archive') {
        node(integrationLabel) {
            try {
                deleteDir()
                checkoutSubmittedCommit()
                acceptedStages.each { acceptedStage ->
                    unstash("${EXECUTION_ID}-${acceptedStage}")
                }
                def attemptArguments = acceptedStages.collect { acceptedStage ->
                    "--attempt-root 'transfers/${acceptedStage}'"
                }.join(' ')
                def deadlineArguments = runDeadlineExpired ? '--run-deadline-expired' : ''
                withEnv(["SDI_BUILD_STARTED_AT_MILLIS=${buildStartedAtMillis}"]) {
                    timeout(time: 120, unit: 'SECONDS') {
                        sh """set -eu
sdi-integration finalize-jenkins-run \\
${commonArguments} \\
  --build-started-at-millis \"\$SDI_BUILD_STARTED_AT_MILLIS\" \\
  ${attemptArguments} \\
  ${deadlineArguments} \\
  --bundle-root bundle >/dev/null
"""
                        def conclusion = sh(
                            returnStatus: true,
                            script: '''set -eu
sdi-integration bundle-conclusion \
  --bundle-root bundle \
  --execution-id "$EXECUTION_ID"
''',
                        )
                        if (conclusion == 2) {
                            error('Pipeline integration bundle validation failed')
                        }
                        dir('bundle') {
                            archiveArtifacts(
                                artifacts: '**/*',
                                allowEmptyArchive: false,
                                defaultExcludes: false,
                            )
                        }
                        sh '''set -eu
sdi-integration deployment-manifest \
  --bundle-root bundle \
  --output deployment-manifest.yaml
'''
                        archiveArtifacts(
                            artifacts: 'deployment-manifest.yaml',
                            allowEmptyArchive: true,
                            defaultExcludes: false,
                        )
                        if (conclusion == 1) {
                            error('Pipeline integration machinery or validation verdict failed; structured result archived')
                        }
                    }
                }
            } finally {
                deleteDir()
            }
        }
    }
}
} catch (FlowInterruptedException interruption) {
    if (isTimeoutInterruption(interruption)) {
        error('Jenkins pipeline safety timeout expired')
    }
    throw interruption
}
