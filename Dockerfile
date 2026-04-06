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

# ── User setup (reuse the existing 'ubuntu' user from the base image) ─
# .claude-home is bind-mounted over /home/ubuntu at runtime, so only
# create dirs needed for the build here; runtime state lives on the host.
RUN mkdir -p /home/ubuntu/.claude /home/ubuntu/.config/gh \
    && chown -R ubuntu:ubuntu /home/ubuntu

USER ubuntu
RUN rosdep update

WORKDIR /workspace

# Persist the chosen distro so scripts can discover it at runtime
ENV ROS_DISTRO=${ROS_DISTRO}

# Write .bashrc to a build-time location; the entrypoint copies it if the
# bind-mounted home doesn't already have one.
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /tmp/.bashrc.default \
    && echo '[ -f /workspace/install/setup.bash ] && source /workspace/install/setup.bash' >> /tmp/.bashrc.default

COPY --chown=ubuntu:ubuntu entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--dangerously-skip-permissions"]
