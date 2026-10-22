# claude-skill-guard

Mandatory NVIDIA SkillSpector scan for every agent skill or plugin before it
is installed or used in Claude Code. Procedure and setup: `README.md` (Hebrew).

- `skill_guard.py` uses only the Python standard library; keep it that way so
  it runs with plain `python3 -I` from hooks.
- SkillSpector is pinned in `SKILLSPECTOR_SPEC` / `SKILLSPECTOR_VERSION`.
  Check a new commit on github.com/NVIDIA/skillspector before moving the pin.
- `approved.json` changes only after the user approves in chat; commit each
  approval with the reason in the note.
- `plugins.json` lists plugins and skills installed in every session; add one
  only after `skill-guard scan` passes (or the user approves a REVIEW or
  BLOCKED result in chat). Pin skill sets to the full commit that was scanned.
- `notes.md` goes into `~/.claude/CLAUDE.md` as a managed block; keep it to
  facts every session needs.
