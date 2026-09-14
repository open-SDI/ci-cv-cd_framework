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
     descriptor: 'deployment/jenkins/adapters/composition-fixture-v1.yaml',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_COMPOSITION_WORK_LIMIT_SECONDS', env.JENKINS_COMPOSITION_WORK_LIMIT_SECONDS)],
    [name: 'Image build', stage: 'image_build', label: 'image-build',
     descriptor: 'deployment/jenkins/adapters/image-build-fixture-v1.yaml',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS', env.JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS)],
    [name: 'Continuous Validation', stage: 'cv', label: 'cv',
     descriptor: 'deployment/jenkins/adapters/cv-fixture-v1.yaml',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_CV_WORK_LIMIT_SECONDS', env.JENKINS_CV_WORK_LIMIT_SECONDS)],
    [name: 'Continuous Deployment', stage: 'cd', label: 'cd',
     descriptor: 'deployment/jenkins/adapters/cd-fixture-v1.yaml',
     workLimitSeconds: readPositiveSeconds(
         'JENKINS_CD_WORK_LIMIT_SECONDS', env.JENKINS_CD_WORK_LIMIT_SECONDS)],
]

def isTimeoutInterruption = { interruption ->
    interruption.causes.any { cause ->
        cause instanceof TimeoutStepExecution.ExceededTimeout
    }
}

def checkoutSubmittedCommit = {
    checkout([
        $class: 'GitSCM',
        branches: [[name: env.RESOLVED_COMMIT_SHA]],
        extensions: [[$class: 'CloneOption', honorRefspec: true, noTags: true]],
        userRemoteConfigs: [[
            refspec: '+refs/heads/main:refs/remotes/origin/main',
            url: env.SDI_REPOSITORY_URL,
        ]],
    ])
    sh '''set -eu
test "$(git rev-parse HEAD)" = "$RESOLVED_COMMIT_SHA"
git fetch --no-tags origin '+refs/heads/main:refs/remotes/origin/sdi-protected-main'
git merge-base --is-ancestor "$RESOLVED_COMMIT_SHA" refs/remotes/origin/sdi-protected-main
git update-ref refs/heads/main "$RESOLVED_COMMIT_SHA"
'''
}

def commonArguments = '''\
  --repository . \
  --requested-ref "$REQUESTED_GIT_REF" \
  --resolved-commit "$RESOLVED_COMMIT_SHA" \
  --run-request-path "$RUN_REQUEST_PATH" \
  --execution-id "$EXECUTION_ID"'''

def resourceNodes = [:]
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
                ]) {
                    sh """set -eu
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
  --cd-node \"\$SDI_CD_NODE\" >/dev/null
"""
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
                    withEnv([
                        "SDI_RUN_DEADLINE_EPOCH_MILLIS=${runDeadlineEpochMillis}",
                    ]) {
                        try {
                            timeout(time: definition.workLimitSeconds + 60, unit: 'SECONDS') {
                                stageExecutionStatus = sh(returnStatus: true, script: """set -eu
sdi-integration execute-jenkins-stage \\
${commonArguments} \\
  --descriptor-path '${definition.descriptor}' \\
  --attempt-root 'transfers/${definition.stage}' \\
  ${priorArguments} \\
  --run-deadline-epoch-millis \"\$SDI_RUN_DEADLINE_EPOCH_MILLIS\" >/dev/null
""")
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
