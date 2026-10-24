## Remotion in Claude Code cloud sessions

The network blocks remotion.media, so Remotion cannot download its Chrome
Headless Shell here. Render with the Chromium headless shell that is already
installed:

```bash
npx remotion render src/index.ts <Composition> out/video.mp4 \
  --browser-executable="$(ls -d /opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell | head -1)"
```

Use the same flag for `npx remotion still`. In a project, the same path can go
in `remotion.config.ts` with `Config.setBrowserExecutable(...)`. FFmpeg is
installed. Tested 2026-10-22: a 2-second 1080x1920 video rendered in about 30 s.

## Language

Write chat replies to the user in Hebrew. Code, commit messages and repository
files keep the language each project uses.

## Searching skills with find-skills in Claude Code cloud sessions

Node's built-in fetch ignores the session's HTTPS proxy, so the search request
of the skills CLI fails and `npx skills find` prints "No skills found" even when
matches exist. Run the search with the proxy turned on for Node:

```bash
NODE_USE_ENV_PROXY=1 npx skills find <query>
```

The search goes to skills.sh, which must be in the environment's allowed
domains. Installing (`npx skills add`) uses git and works without the flag.
Tested 2026-10-24: a search for "instagram" listed skills with install counts.
