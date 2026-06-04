# Base image
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    ca-certificates \
    gnupg \
    lsb-release \
    python3 \
    python3-pip \
    unzip \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (Claude Code requires >= 18)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies
RUN pip3 install anthropic fastapi uvicorn[standard] pyyaml

# ── App files ──────────────────────────────────────────────────────────────
WORKDIR /app

# Copy server and UI
COPY server.py        /app/server.py
COPY static/          /app/static/
COPY vjailbreak-migrate/SKILL.md /app/skill/SKILL.md

# ── Environment ────────────────────────────────────────────────────────────
# Kubeconfig can be provided three ways (in priority order):
#   1. Volume mount:  -v /path/kub.yaml:/root/.kube/kub.yaml
#   2. UI upload:     paste/upload via the Sorting Hat UI
#   3. Build-time:    uncomment the COPY below
# COPY kub.yaml /root/.kube/kub.yaml
RUN mkdir -p /root/.kube
ENV KUBECONFIG=/root/.kube/kub.yaml
# Pass at runtime: docker run -e ANTHROPIC_API_KEY=sk-ant-...

EXPOSE 7090

# ── Entrypoint ─────────────────────────────────────────────────────────────
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7090"]
