ARG ROS_DISTRO=jazzy
FROM ros:${ROS_DISTRO}

# ── System deps (git, gh CLI, jq, curl) ─────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-colcon-common-extensions \
    python3-rosdep \
    python3-vcstool \
    python3-yaml \
    git \
    jq \
    curl \
    ca-certificates \
    gpg \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI ───────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js (for Claude Code CLI) ───────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# ── Initialize rosdep ───────────────────────────────────────────────
RUN rosdep init || true

# ── User setup ───────────────────────────────────────────────────────
RUN useradd -m -s /bin/bash -u 1000 claude \
    && mkdir -p /home/claude/.claude /home/claude/.config/gh \
    && chown -R claude:claude /home/claude/.claude /home/claude/.config/gh

USER claude
RUN rosdep update

WORKDIR /workspace

# Persist the chosen distro so scripts can discover it at runtime
ENV ROS_DISTRO=${ROS_DISTRO}

# Source ROS2 setup in every shell
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /home/claude/.bashrc \
    && echo '[ -f /workspace/install/setup.bash ] && source /workspace/install/setup.bash' >> /home/claude/.bashrc

ENTRYPOINT ["claude"]
CMD ["--dangerously-skip-permissions"]
