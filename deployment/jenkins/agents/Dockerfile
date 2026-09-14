ARG RUNTIME_IMAGE=docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded
ARG REMOTING_IMAGE=docker.io/jenkins/inbound-agent:3391.va_37fa_a_305d6d-2-jdk21@sha256:a2ee24c1dbd2080a9f03bb668f63a2c8d23c7b6e5bf20fe0033d4d28f4410048

FROM ${RUNTIME_IMAGE} AS runtime
FROM ${UV_IMAGE} AS uv
FROM ${REMOTING_IMAGE} AS remoting

FROM runtime AS tooling
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /opt/sdi/integration
COPY integration/README.md integration/pyproject.toml integration/uv.lock ./
COPY integration/src src
COPY integration/fixtures fixtures
RUN uv sync --frozen --no-dev --no-editable

FROM runtime

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

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/sdi/integration/.venv/bin:/opt/java/openjdk/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin \
    UV_CACHE_DIR=/home/jenkins/agent/.uv-cache \
    XDG_CACHE_HOME=/home/jenkins/agent/.cache

USER jenkins
ENTRYPOINT ["/usr/local/bin/jenkins-agent-entrypoint"]
