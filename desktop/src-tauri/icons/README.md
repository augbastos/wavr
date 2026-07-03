# Icons (generated — not committed)

The bundle icons referenced by `tauri.conf.json` (`32x32.png`, `128x128.png`,
`128x128@2x.png`, `icon.icns`, `icon.ico`) are **generated**, not hand-committed. Run this
once from `desktop/` before the first build:

```bash
npm run icon        # = tauri icon ../frontend/icon.svg
```

That command writes all the required sizes/formats into this folder. They are git-ignored
(see `desktop/.gitignore`) so the repo stays free of binary blobs; regenerate anytime.

If you want a different source image, pass it explicitly: `npx tauri icon path/to/icon.png`
(a 1024×1024 PNG is ideal).
