# ROS2 Claude Workspace

A self-improving ROS2 workspace powered by Claude Code. Defaults to **ROS2 Jazzy** but supports any ROS2 distro.

## Setup

1. Add your ROS2 package repositories to `repos.yaml`:

   ```yaml
   repositories:
     my_package:
       type: git
       url: https://github.com/org/repo.git
       version: main
   ```

2. Build and run the Docker container:

   ```bash
   ./run.sh                        # defaults to jazzy
   ./run.sh --ros-distro humble    # use a different distro
   ROS_DISTRO=rolling ./run.sh     # or via env var
   ```

   The container is based on the official `ros:<distro>` image and includes
   colcon, rosdep, vcstool, and the Claude Code CLI.

3. Inside the container (or via `./scripts/init-src.sh`), initialize and build:

   ```bash
   ./scripts/init-src.sh
   ```

   This clones repos from `repos.yaml`, installs ROS2 dependencies via rosdep,
   and runs `colcon build --symlink-install`.

## Docker Image

The Dockerfile accepts a `ROS_DISTRO` build arg (default: `jazzy`) and layers on top of `ros:<distro>`:
- **ROS2 tools**: colcon, rosdep, vcstool
- **Dev tools**: git, gh CLI, jq, curl
- **Claude Code**: Node.js 22 + `@anthropic-ai/claude-code`

`--network host` is used so ROS2 DDS discovery works between the container and the host.

## Workspace Structure

```
.
├── repos.yaml          # Repository list (vcstool format)
├── src/                # ROS2 packages (cloned from repos.yaml)
├── build/              # Build artifacts (ignored)
├── install/            # Install space (ignored)
└── log/                # Build logs (ignored)
```
