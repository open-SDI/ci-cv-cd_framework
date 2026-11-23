ARG RUNTIME_IMAGE=docker.io/library/python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

FROM ${RUNTIME_IMAGE}
LABEL org.sdi.project=ci-cv-cd_framework

ARG SQUID_MAJOR=6

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/* \
    && squid -v | head -n 1 | grep -E -q "Version ${SQUID_MAJOR}\."
COPY squid.conf /etc/squid/squid.conf
RUN chmod 0444 /etc/squid/squid.conf \
    && squid -k parse -f /etc/squid/squid.conf

USER proxy
ENTRYPOINT ["squid", "-N", "-f", "/etc/squid/squid.conf"]
