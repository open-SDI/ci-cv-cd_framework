FROM docker.io/jenkins/jenkins:2.568.3-jdk21@sha256:68964c38dbb70c0bf6adff480d6dda2e1191d2d07e076c9fd2996345303eb999

COPY --chown=jenkins:jenkins controller/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --latest=false --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY --chown=jenkins:jenkins casc/jenkins.yaml /usr/local/share/jenkins/casc/jenkins.yaml
COPY --chown=jenkins:jenkins jobs/pipeline-integration.groovy /usr/local/share/jenkins/job-dsl/pipeline-integration.groovy

RUN printf '%s\n' '2.568.3' > /usr/share/jenkins/ref/jenkins.install.UpgradeWizard.state \
    && printf '%s\n' '2.568.3' > /usr/share/jenkins/ref/jenkins.install.InstallUtil.lastExecVersion

ENV CASC_JENKINS_CONFIG=/usr/local/share/jenkins/casc/jenkins.yaml
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Dcasc.strict.secret.resolution=true"
