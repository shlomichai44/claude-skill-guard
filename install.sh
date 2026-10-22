#!/bin/bash
# Install skill-guard for every Claude Code session on this computer or cloud
# environment: scanner tools, the `skill-guard` command, and the hooks in
# ~/.claude/settings.json. Safe to run again.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -I "$DIR/skill_guard.py" setup

mkdir -p "$HOME/.local/bin"
printf '#!/bin/bash\nexec python3 -I "%s/skill_guard.py" "$@"\n' "$DIR" > "$HOME/.local/bin/skill-guard"
chmod +x "$HOME/.local/bin/skill-guard"

python3 -I "$DIR/skill_guard.py" install-hooks
echo "skill-guard installed from $DIR"
