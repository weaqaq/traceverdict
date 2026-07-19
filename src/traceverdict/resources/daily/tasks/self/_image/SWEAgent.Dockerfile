FROM traceverdict/self-base:py3.12-v1

# SWE-agent 1.1.0 resets a preexisting repository via the git CLI before the
# first model query. Pin the Debian package version observed from the frozen
# Python base image's trixie repository on 2026-07-15.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /testbed
