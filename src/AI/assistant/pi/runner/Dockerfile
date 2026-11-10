# One image that runs one assistant turn with pi: pi, the Studio extension, the skills and the
# Python validators. Built from the repository root, like the assistant image.
FROM node:24-trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git ripgrep curl jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

# The Agent's build containers reach the network through a mediating proxy with its own CA.
RUN if [ -f /run/agent/tls/ca-bundle.pem ]; then export NODE_EXTRA_CA_CERTS=/run/agent/tls/ca-bundle.pem; fi \
    && npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.4.0

WORKDIR /opt/assistant
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1
COPY src/AI/assistant/pyproject.toml src/AI/assistant/uv.lock src/AI/assistant/.python-version ./
RUN if [ -f /run/agent/tls/ca-bundle.pem ]; then export SSL_CERT_FILE=/run/agent/tls/ca-bundle.pem; fi \
    && uv sync --locked --no-dev --no-install-project

COPY src/AI/assistant/agents/ ./agents/
COPY src/AI/assistant/shared/ ./shared/
COPY src/AI/assistant/pi/ ./pi/
COPY src/common/ts/layout-contract/schemas/json/ /common/ts/layout-contract/schemas/json/

ENV PI_CODING_AGENT_DIR=/opt/assistant/pi/agent \
    PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1 \
    STUDIO_PYTHON=/opt/assistant/.venv/bin/python \
    STUDIO_TOOL=/opt/assistant/pi/studio_tool.py \
    STUDIO_SKILLS_DIR=/opt/assistant/agents/skills

RUN useradd --create-home studio && mkdir /workspace && chown studio /workspace
USER studio
WORKDIR /workspace
ENTRYPOINT ["pi", "--no-extensions", "-e", "/opt/assistant/pi/extension/index.ts", \
    "--no-skills", "--skill", "/opt/assistant/agents/skills", "--no-prompt-templates", "--no-themes", \
    "--model", "azure/gpt-6.1-terra", "--thinking", "off", \
    "--append-system-prompt", "/opt/assistant/pi/APPEND_SYSTEM.md"]
