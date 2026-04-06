# ROS2 Claude Workspace

A self-improving ROS2 workspace powered by Claude Code.

## Setup

1. Add your ROS2 package repositories to `repos.yaml`:

   ```yaml
   repositories:
     my_package:
       type: git
       url: https://github.com/org/repo.git
       version: main
   ```

2. Initialize the `src/` folder:

   ```bash
   ./scripts/init-src.sh
   ```

   This clones all repositories listed in `repos.yaml` into `src/` using [vcstool](https://github.com/dirk-thomas/vcstool).

3. Build the workspace:

   ```bash
   colcon build
   ```

## Workspace Structure

```
.
├── repos.yaml          # Repository list (vcstool format)
├── src/                # ROS2 packages (cloned from repos.yaml)
├── build/              # Build artifacts (ignored)
├── install/            # Install space (ignored)
└── log/                # Build logs (ignored)
```
