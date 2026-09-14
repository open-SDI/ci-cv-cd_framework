pipelineJob('pipeline-integration') {
    description('Repository-owned SDI pipeline integration entry point.')

    authorization {
        userPermission('hudson.model.Item.Read', HANDOFF_USER)
        userPermission('hudson.model.Item.Build', HANDOFF_USER)
        userPermission('hudson.model.Item.Cancel', HANDOFF_USER)
    }

    properties {
        disableConcurrentBuilds()
    }

    logRotator {
        daysToKeep(90)
        artifactDaysToKeep(90)
    }

    parameters {
        stringParam('HANDOFF_CONTRACT_VERSION', '', 'Version of the scalar handoff contract.')
        stringParam('EXECUTION_ID', '', 'Project-owned pipeline integration run identity.')
        stringParam('REQUESTED_GIT_REF', '', 'Protected Git reference requested by GitHub.')
        stringParam('RESOLVED_COMMIT_SHA', '', 'Immutable full commit SHA to check out.')
        stringParam('RUN_REQUEST_PATH', '', 'Repository-relative pipeline integration run request.')
        stringParam('GITHUB_RUN_ID', '', 'GitHub workflow run provenance identifier.')
        stringParam('GITHUB_RUN_ATTEMPT', '', 'GitHub workflow run attempt provenance identifier.')
    }

    definition {
        cpsScm {
            scm {
                git {
                    remote {
                        url(REPOSITORY_URL)
                        refspec('+refs/heads/main:refs/remotes/origin/main')
                    }
                    branch('${RESOLVED_COMMIT_SHA}')
                    extensions {
                        cloneOptions {
                            honorRefspec()
                            noTags()
                        }
                        wipeOutWorkspace()
                    }
                }
            }
            scriptPath('Jenkinsfile')
            lightweight(false)
        }
    }
}
