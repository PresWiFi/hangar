# Running inside Hangar

This Claude Code session runs inside the Hangar Docker container (Debian bookworm), not directly on the user's computer.

- If a task needs a tool that isn't installed, you can install it with `sudo apt-get install -y <package>` (run `sudo apt-get update` first if the package isn't found). Every sudo command shows the user a permission prompt, so say briefly what you want to install and why before running it.
- Packages installed with apt are remembered and reinstalled automatically when the container is recreated.
- User-level installs (`pip install --user`, `~/.local/bin`, `~/.cargo`, `~/.nvm`) are kept in the home volume.
- Anything else written outside the home folder and the mounted workspace (for example `npm install -g` into /usr/local) is lost when the container is recreated. Prefer apt or user-level installs.
- The workspace is mounted at the same path as on the host, so file changes there are real changes on the user's machine.
