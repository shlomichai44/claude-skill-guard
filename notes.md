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
