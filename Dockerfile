FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-yaml \
    git \
    jq \
    curl \
    ca-certificates \
    gpg \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN usermod -u 1001 node \
    && useradd -m -s /bin/bash -u 1000 claude \
    && mkdir -p /home/claude/.claude /home/claude/.config/gh \
    && chown -R claude:claude /home/claude/.claude /home/claude/.config/gh
USER claude
WORKDIR /workspace

ENTRYPOINT ["claude"]
CMD ["--dangerously-skip-permissions"]
