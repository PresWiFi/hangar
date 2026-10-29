#!/bin/bash
# Prepares the container user's home (a volume, so it survives restarts) and starts Hangar.
set -e
mkdir -p "$HOME/.claude" "$HOME/.codex"

# A Docker volume mounted on a folder the image doesn't have starts out owned by root, and then Claude Code
# can't save its login there (it still prints "Login successful"). Fix the owner if we can.
for dir in "$HOME/.claude" "$HOME/.codex"; do
  if [ ! -w "$dir" ]; then
    if sudo -n chown "$(id -u):$(id -g)" "$dir" 2>/dev/null; then
      echo "Fixed the owner of $dir (it belonged to $(stat -c %U "$dir" 2>/dev/null || echo someone else) before)."
    else
      echo "WARNING: $dir isn't writable by $(id -un). Run: docker exec -u root <container> chown $(id -u):$(id -g) $dir"
    fi
  fi
done

# Claude Code keeps onboarding state and folder trust in ~/.claude.json, which lives outside the
# shared ~/.claude folder. Seed it from Claude's own newest backup so sessions don't start with
# the first-run setup screens.
python3 - <<'PY'
import glob, json, os
home = os.path.expanduser("~")
path = os.path.join(home, ".claude.json")
try:
    current = json.load(open(path))
except (OSError, ValueError):
    current = {}
if not current.get("hasCompletedOnboarding") or not current.get("projects"):
    # Newest backup from a fully set-up Claude Code (skips stubs the container itself may have written).
    seed = {}
    for b in sorted(glob.glob(os.path.join(home, ".claude", "backups", ".claude.json.backup.*")),
                    key=os.path.getmtime, reverse=True):
        try:
            data = json.load(open(b))
        except (OSError, ValueError):
            continue
        if data.get("hasCompletedOnboarding"):
            seed = data
            print(f"Seeded ~/.claude.json from {b}")
            break
    current = {**current, **seed}
    current["hasCompletedOnboarding"] = True
    current.setdefault("theme", "dark")

# Trust the workspace folders the owner configured (this covers their subfolders too). Otherwise a fresh
# login asks "Do you trust this folder?" on the first session, and its default answer exits Claude.
projects = current.setdefault("projects", {})
for ws in os.environ.get("HANGAR_WORKSPACES", "").replace(";", ",").split(","):
    ws = ws.strip().rstrip("/")
    if ws:
        projects.setdefault(ws, {})["hasTrustDialogAccepted"] = True
with open(path, "w") as f:
    json.dump(current, f, indent=2)
os.chmod(path, 0o600)
PY

# Mounted repos can be owned by a different uid than the container user; let git use them.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qx '\*'; then
  git config --global --add safe.directory '*'
fi

# A friendlier shell for sessions that drop back to bash.
if [ ! -f "$HOME/.bashrc" ] || ! grep -q hangar-prompt "$HOME/.bashrc"; then
  cat >> "$HOME/.bashrc" <<'RC'
# hangar-prompt
PS1='\[\e[2m\]hangar\[\e[0m\] \[\e[36m\]\w\[\e[0m\] \$ '
alias ll='ls -lah'
RC
fi

# Reinstall packages Claude (or you) added with apt before the container was recreated.
if [ -s /data/apt-packages.txt ] && sudo -n true 2>/dev/null; then
  missing=$(comm -13 <(sort -u /etc/hangar/base-packages.txt) <(sort -u /data/apt-packages.txt) | while read -r p; do
    dpkg -s "$p" >/dev/null 2>&1 || echo "$p"; done | xargs)
  if [ -n "$missing" ]; then
    echo "Reinstalling packages in the background: $missing (log: /data/apt-restore.log)"
    ( sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing ) > /data/apt-restore.log 2>&1 &
  fi
fi

exec python -m hangar "$@"
