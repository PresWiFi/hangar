#!/bin/bash
# Prepares the container user's home (a volume, so it survives restarts) and starts Hangar.
set -e
mkdir -p "$HOME/.claude"

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
    merged = {**current, **seed}
    merged["hasCompletedOnboarding"] = True
    merged.setdefault("theme", "dark")
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
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

exec python -m hangar "$@"
