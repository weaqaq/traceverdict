FROM traceverdict/self-base:swe-agent-1.1.0-v2

# SWE-ReX otherwise falls back to an unpinned, network-time `pipx run` inside
# every fresh agent container.  Bake the exact host-side runtime version used
# by pinned SWE-agent 1.1.0 so environment startup is offline and bounded.
RUN python -m pip install --no-cache-dir swe-rex==1.2.1 \
    && command -v swerex-remote \
    && swerex-remote --help >/dev/null

WORKDIR /testbed
