FROM traceverdict/self-base:py3.12-v1

# SWE-agent 1.1.0 resets a preexisting repository via the git CLI before the
# first model query. Pin the Debian package and explicitly trust the isolated
# bind mount, whose host UID differs from the root user inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 \
    && git config --system --add safe.directory /testbed \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /testbed
