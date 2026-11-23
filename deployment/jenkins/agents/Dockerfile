ARG RUNTIME_IMAGE=docker.io/library/python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded
ARG REMOTING_IMAGE=docker.io/jenkins/inbound-agent:3391.va_37fa_a_305d6d-2-jdk21@sha256:a2ee24c1dbd2080a9f03bb668f63a2c8d23c7b6e5bf20fe0033d4d28f4410048
ARG BUILDKIT_IMAGE=docker.io/moby/buildkit:v0.34.0@sha256:b059f8d7226d0b326bc871af489a5dddf65e36af4d20d14251b072974d9ee87c
ARG KUBECTL_IMAGE=registry.k8s.io/kubectl:v1.33.5@sha256:126d8f5898d19973cf0e2f7b89f06bd5338eaa11cba9b8a8a95985058af6012f

FROM ${RUNTIME_IMAGE} AS runtime
FROM ${UV_IMAGE} AS uv
FROM ${REMOTING_IMAGE} AS remoting
FROM ${BUILDKIT_IMAGE} AS buildkit
FROM ${KUBECTL_IMAGE} AS kubectl

FROM runtime AS tooling
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /opt/sdi/integration
COPY integration/README.md integration/pyproject.toml integration/uv.lock ./
COPY integration/src src
COPY integration/fixtures fixtures
RUN uv sync --frozen --no-dev --no-editable
RUN .venv/bin/sdi-integration integration-code-digest --root . \
        > /opt/sdi/integration-code-digest \
    && chmod 0444 /opt/sdi/integration-code-digest

FROM runtime AS agent
LABEL org.sdi.project=ci-cv-cd_framework

ARG SDI_JENKINS_AGENT_LABEL
ARG SDI_JENKINS_AGENT_ROLE=integration

USER root
COPY --from=remoting / /
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=tooling /opt/sdi/integration/.venv /opt/sdi/integration/.venv
COPY \
    deployment/jenkins/agents/jenkins-agent-entrypoint \
    deployment/jenkins/agents/jenkins-agent-health \
    /usr/local/bin/
RUN case "$SDI_JENKINS_AGENT_ROLE" in \
        integration) test -z "$SDI_JENKINS_AGENT_LABEL" ;; \
        domain) test -n "$SDI_JENKINS_AGENT_LABEL" ;; \
        *) exit 1 ;; \
    esac \
    && install -d -m 0555 /etc/sdi \
    && printf '%s\n' "$SDI_JENKINS_AGENT_ROLE" > /etc/sdi/jenkins-agent-role \
    && printf '%s\n' "$SDI_JENKINS_AGENT_LABEL" > /etc/sdi/jenkins-agent-label \
    && chmod 0444 /etc/sdi/jenkins-agent-role /etc/sdi/jenkins-agent-label \
    && chmod 0555 \
        /usr/local/bin/jenkins-agent-entrypoint \
        /usr/local/bin/jenkins-agent-health \
    && python3 --version \
    && uv --version \
    && git --version \
    && /opt/java/openjdk/bin/java -version
COPY --from=tooling \
    /opt/sdi/integration-code-digest \
    /etc/sdi/integration-code-digest

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/sdi/integration/.venv/bin:/opt/java/openjdk/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin \
    UV_CACHE_DIR=/home/jenkins/agent/.uv-cache \
    XDG_CACHE_HOME=/home/jenkins/agent/.cache

USER jenkins
ENTRYPOINT ["/usr/local/bin/jenkins-agent-entrypoint"]

FROM agent AS image-build
COPY --from=buildkit /usr/bin/buildctl /usr/bin/buildctl

FROM agent AS cd
COPY --from=kubectl /bin/kubectl /usr/bin/kubectl
