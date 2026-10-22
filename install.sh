#!/bin/bash
# Install skill-guard for every Claude Code session on this computer or cloud
# environment: scanner tools, the `skill-guard` command, and the hooks in
# ~/.claude/settings.json. Safe to run again.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A failed tool install must not stop the hooks from being installed: the
# session-start hook retries it and reports what is missing.
python3 -I "$DIR/skill_guard.py" setup || echo "skill-guard: scanner setup incomplete; the session-start hook will retry"

mkdir -p "$HOME/.local/bin"
printf '#!/bin/bash\nexec python3 -I "%s/skill_guard.py" "$@"\n' "$DIR" > "$HOME/.local/bin/skill-guard"
chmod +x "$HOME/.local/bin/skill-guard"

python3 -I "$DIR/skill_guard.py" install-hooks
python3 -I "$DIR/skill_guard.py" install-plugins || echo "skill-guard: some listed plugins were not installed"
python3 -I "$DIR/skill_guard.py" install-notes || echo "skill-guard: notes were not written"
echo "skill-guard installed from $DIR"
