ARG RUNTIME_IMAGE=docker.io/library/python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded
ARG RUNNER_VERSION=2.337.0
ARG RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613

FROM ${RUNTIME_IMAGE} AS runtime
FROM ${UV_IMAGE} AS uv

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

FROM runtime AS runner-dist
ARG RUNNER_VERSION
ARG RUNNER_SHA256
ADD --checksum=sha256:${RUNNER_SHA256} \
    https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz \
    /tmp/actions-runner.tar.gz
RUN install -d /opt/actions-runner \
    && tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner --no-same-owner

FROM runtime
LABEL org.sdi.project=ci-cv-cd_framework

ARG RUNNER_VERSION

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libicu76 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=runner-dist /opt/actions-runner /opt/actions-runner
COPY --from=tooling /opt/sdi/integration/.venv /opt/sdi/integration/.venv
COPY \
    deployment/jenkins/runner/sdi-runner-entrypoint \
    deployment/jenkins/runner/sdi-runner-probe \
    /usr/local/bin/
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /home/runner \
        --shell /usr/sbin/nologin runner \
    && install -d -o runner -g runner -m 0700 /var/lib/sdi-runner \
    && install -d -m 0555 /etc/sdi \
    && printf '%s\n' "$RUNNER_VERSION" > /etc/sdi/github-runner-version \
    && chmod 0444 /etc/sdi/github-runner-version \
    && chmod 0555 \
        /usr/local/bin/sdi-runner-entrypoint \
        /usr/local/bin/sdi-runner-probe \
    && test "$(/opt/actions-runner/bin/Runner.Listener --version)" = "$RUNNER_VERSION" \
    && rm -rf /opt/actions-runner/_diag \
    && python3 --version \
    && git --no-lazy-fetch --version
COPY --from=tooling \
    /opt/sdi/integration-code-digest \
    /etc/sdi/integration-code-digest

ENV HOME=/home/runner \
    PATH=/opt/sdi/integration/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin

USER runner
ENTRYPOINT ["/usr/local/bin/sdi-runner-entrypoint"]
