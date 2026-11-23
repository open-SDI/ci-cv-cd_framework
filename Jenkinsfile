import org.jenkinsci.plugins.workflow.steps.FlowInterruptedException
import org.jenkinsci.plugins.workflow.steps.TimeoutStepExecution


def readPositiveSeconds = { name, raw ->
    if (raw == null || !(raw ==~ /[1-9][0-9]*/)) {
        error("${name} must be a positive integer")
    }
    def value = raw.toInteger()
    if (value > 86400) {
        error("${name} must not exceed 86400 seconds")
    }
    value
}

def runLimitSeconds = readPositiveSeconds(
    'JENKINS_PIPELINE_RUN_LIMIT_SECONDS',
    env.JENKINS_PIPELINE_RUN_LIMIT_SECONDS,
)
if (runLimitSeconds < 180) {
    error('JENKINS_PIPELINE_RUN_LIMIT_SECONDS must reserve finalization time')
}

def stages = [
    [name: 'Composition', stage: 'composition', label: 'composition',
     descriptor: 'deployment/jenkins/adapters/composition-v1.yaml',
     suppliedArguments: '--service-repository service-repository',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_COMPOSITION_WORK_LIMIT_SECONDS', env.JENKINS_COMPOSITION_WORK_LIMIT_SECONDS)],
    [name: 'Image build', stage: 'image_build', label: 'image-build',
     descriptor: 'deployment/jenkins/adapters/image-build-fixture-v1.yaml',
     suppliedArguments: '',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS', env.JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS)],
    [name: 'Continuous Validation', stage: 'cv', label: 'cv',
     descriptor: 'deployment/jenkins/adapters/cv-fixture-v1.yaml',
     suppliedArguments: '',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_CV_WORK_LIMIT_SECONDS', env.JENKINS_CV_WORK_LIMIT_SECONDS)],
    [name: 'Continuous Deployment', stage: 'cd', label: 'cd',
     descriptor: 'deployment/jenkins/adapters/cd-fixture-v1.yaml',
     suppliedArguments: '',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_CD_WORK_LIMIT_SECONDS', env.JENKINS_CD_WORK_LIMIT_SECONDS)],
]

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
// fetches only the blobs a sparse checkout without the unused CV/ Gazebo assets
// needs; CV/ is nearly all of the repository's bytes. The CLI reads committed
// blobs with lazy fetching disabled, so it fails closed outside that set. The
// git plugin's checkout step cannot filter blobs, so plain git is used. Only the
// transfer is retried, with stalled transfers aborted; no Stage attempt is.
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
git sparse-checkout set --no-cone '/*' '!/CV/'
git_transfer_with_retries 'submitted-commit checkout' reset_checkout checkout --quiet --detach "$RESOLVED_COMMIT_SHA"
test "$(git rev-parse HEAD)" = "$RESOLVED_COMMIT_SHA"
git update-ref refs/heads/main "$RESOLVED_COMMIT_SHA"
'''
    }
}

// Only composition is given the checked-out service repository. The Domain
// agents reach only the OpenAI endpoint, so Jenkins composes with the committed
// composition config's default luna generator and names neither; its key comes
// from the sdi-openai-api-key credential bound around the composition Stage only.
def commonArguments = '''\
  --repository . \
  --requested-ref "$REQUESTED_GIT_REF" \
  --resolved-commit "$RESOLVED_COMMIT_SHA" \
  --run-request-path "$RUN_REQUEST_PATH" \
  --execution-id "$EXECUTION_ID"'''

// Each agent image records a digest of the integration code it bakes in. An
// image built before that record existed reports `missing`. Exit status 4 means
// an agent image bakes integration code other than the submitted commit's; the
// build description lets the handoff report that as a typed failure.
def bakedCodeDigestCommand = 'cat /etc/sdi/integration-code-digest 2>/dev/null || printf missing'
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
        node('integration') {
            try {
                deleteDir()
                checkoutSubmittedCommit()
                withEnv([
                    "SDI_BUILD_STARTED_AT_MILLIS=${buildStartedAtMillis}",
                    "SDI_INTEGRATION_NODE=${env.NODE_NAME}",
                    "SDI_COMPOSITION_NODE=${resourceNodes.composition}",
                    "SDI_IMAGE_BUILD_NODE=${resourceNodes.image_build}",
                    "SDI_CV_NODE=${resourceNodes.cv}",
                    "SDI_CD_NODE=${resourceNodes.cd}",
                    "SDI_COMPOSITION_BAKED_CODE_DIGEST=${bakedCodeDigests.composition}",
                    "SDI_IMAGE_BUILD_BAKED_CODE_DIGEST=${bakedCodeDigests.image_build}",
                    "SDI_CV_BAKED_CODE_DIGEST=${bakedCodeDigests.cv}",
                    "SDI_CD_BAKED_CODE_DIGEST=${bakedCodeDigests.cd}",
                ]) {
                    def preflightStatus = sh(returnStatus: true, script: """set -eu
sdi-integration preflight-jenkins-run \\
${commonArguments} \\
  --handoff-contract-version \"\$HANDOFF_CONTRACT_VERSION\" \\
  --github-run-id \"\$GITHUB_RUN_ID\" \\
  --github-run-attempt \"\$GITHUB_RUN_ATTEMPT\" \\
  --build-started-at-millis \"\$SDI_BUILD_STARTED_AT_MILLIS\" \\
  --integration-node \"\$SDI_INTEGRATION_NODE\" \\
  --composition-node \"\$SDI_COMPOSITION_NODE\" \\
  --image-build-node \"\$SDI_IMAGE_BUILD_NODE\" \\
  --cv-node \"\$SDI_CV_NODE\" \\
  --cd-node \"\$SDI_CD_NODE\" \\
  --integration-baked-code-digest \"\$(${bakedCodeDigestCommand})\" \\
  --composition-baked-code-digest \"\$SDI_COMPOSITION_BAKED_CODE_DIGEST\" \\
  --image-build-baked-code-digest \"\$SDI_IMAGE_BUILD_BAKED_CODE_DIGEST\" \\
  --cv-baked-code-digest \"\$SDI_CV_BAKED_CODE_DIGEST\" \\
  --cd-baked-code-digest \"\$SDI_CD_BAKED_CODE_DIGEST\" >/dev/null
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
                    priorArguments = "${priorArguments} --work-limit-seconds '${definition.workLimitSeconds}'"
                    def stageExecutionStatus = 0
                    def executeStage = {
                        sh(returnStatus: true, script: """set -eu
sdi-integration execute-jenkins-stage \\
${commonArguments} \\
  ${definition.suppliedArguments} \\
  --descriptor-path '${definition.descriptor}' \\
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
                            timeout(time: definition.workLimitSeconds + 60, unit: 'SECONDS') {
                                if (definition.stage == 'composition') {
                                    withCredentials([string(
                                        credentialsId: 'sdi-openai-api-key',
                                        variable: 'OPENAI_API_KEY',
                                    )]) {
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
        node('integration') {
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
                        if (conclusion == 1) {
                            error('Pipeline integration machinery failed; structured result archived')
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
