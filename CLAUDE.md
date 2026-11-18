# YT Studio — YouTube → Shorts clipping

Pipeline: **yt-dlp** (download best quality) → **faster-whisper large-v3** on the RTX 4060 (word-level transcript)
→ **Claude picks the moments** → **face-tracked 9:16 reframe** (MediaPipe + PySceneDetect + OpenCV Lanczos)
→ **Remotion** (animated word-by-word captions + hook title) → `output/<name>.mp4` (1080x1920, H.264 CRF 14, AAC 320k, -14 LUFS).

## Environment quirks (Windows, this machine)
- Always use the venv interpreter: `.venv\Scripts\python.exe`. Never call `.venv\Scripts\*.exe` launchers
  (yt-dlp.exe, auto-editor.exe…) — Smart App Control blocks them. Use `python -m <module>` instead.
- HTTPS is intercepted (antivirus/proxy). Fixes already in place: `shorts.py` builds
  `models/ca-bundle.pem` from the Windows cert store and yt-dlp runs with `--compat-options no-certifi`
  (do NOT install `pip-system-certs` — it breaks Hugging Face downloads), `uv pip install --system-certs`, `NODE_OPTIONS=--use-system-ca` for npm/npx, `winget --source winget`.
- If ffmpeg/deno aren't found in a fresh shell, refresh PATH:
  `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')`
- Update yt-dlp when YouTube breaks: `uv pip install --system-certs --python .venv\Scripts\python.exe -U "yt-dlp[default]"`

## Workflow (do this when the user gives a YouTube URL)
1. `.venv\Scripts\python.exe shorts.py download <url>` → prints `ID=<id>`; files in `work/<id>/`.
2. `.venv\Scripts\python.exe shorts.py transcribe <id>` → `work/<id>/transcript.txt` (`[mm:ss.ss] text`) and
   `transcript.json` (word timestamps). Run in background for long videos.
3. **Read `transcript.txt` and choose clips.** Write `work/<id>/clips.json`:
   ```json
   [{"name": "kebab-case-title", "start": 123.4, "end": 171.9, "hook": "Short punchy headline", "mode": "crop"}]
   ```
   Clip-picking rules:
   - 20–59 s each (sweet spot 25–45 s). Start on the first word of a strong line — no warm-up, no "so, um".
   - Must make sense standalone: one complete idea/story/punchline. End right after the payoff line.
   - Prefer: bold/contrarian claims, emotional peaks, surprising facts, conflict, funny moments, concrete advice.
   - Snap start/end to word timestamps in `transcript.json` (start ≈ 0.1 s before first word, end ≈ 0.3 s after last).
   - `hook`: ≤ 7 words, curiosity-driven, not clickbait lies. Empty string for none.
   - `mode`: `crop` (face-tracked, talking heads/podcasts) or `fit` (whole frame over blurred bg — screen
     recordings, gameplay, wide shots, multiple people far apart).
   - Show the user the list (time ranges + why each works) before rendering if they haven't said to just go.
4. `.venv\Scripts\python.exe shorts.py batch <id> work/<id>/clips.json` (or `render` for one clip:
   `shorts.py render <id> --start S --end E --name N --hook "..." [--mode fit] [--no-captions]`).
5. Check output: grab a frame `ffmpeg -ss 5 -i output/<name>.mp4 -frames:v 1 frame.png` and look at framing/captions.

## Live streams (watcher)
- `.\start_watcher.ps1 [--chunk 300] [--clips-per-chunk 2] [--picker auto|claude|heuristic] [--mode auto|crop|fit]`
  starts `watcher.py` hidden; `.\stop_watcher.ps1` kills it and its ffmpeg/render children.
- User pastes a live (or upcoming) URL into `stream_url.txt` and saves → watcher waits for it to go live, records
  from now in N-second `.ts` chunks (yt-dlp `-g` HLS feed → ffmpeg segment muxer, copy, re-resolves on drop),
  and processes each finished chunk: transcribe → pick → `shorts.cmd_render` → `output/<id>-cNNNNN-<name>.mp4`.
  Clearing the file / new URL stops the current recording. Stops by itself when the stream ends.
- Picking: `claude -p` (headless Claude Code) first; if it fails (e.g. not logged in) → built-in heuristic picker
  (loudness + hype words + speech density). Chunks live in `work/live/<id>/cNNNNN/`. Log: `work/live/watcher.log`.
- Moments that straddle a chunk boundary are lost; longer chunks = fewer losses but more delay.

## Sound effects
- `sfx.py`: synthesized library in `sfx/` (whoosh, impact, boom, pop, ding, riser, bassdrop, glitch). Replace any
  file with a real WAV of the same name for better sound. On by default in `render`/`batch`; `--no-sfx` to skip.
- Cues come from (priority): Claude's `sfx` list in clips.json (`[{"type":"impact","t":12.3}]`, clip-relative for
  batch) → spoken keywords (boom/crash/money/insane/dead…) → sudden loudness jumps (gunshots, crashes, screams)
  → scene cuts (whoosh). Max ~1 per 4 s, ≥1.5 s apart. Mixed under the voice with a limiter.
- `--mode auto` (default) samples frames: big face most of the time → `crop`, otherwise `fit`.

## Quality notes
- Source quality is the ceiling: a 9:16 crop of 1080p is only ~608 px wide → prefer videos with 4K available
  (download already picks the highest resolution).
- Intermediates are near-lossless (x264 CRF 8–10); only the final Remotion render is a delivery encode.
- Caption style lives in `remotion/src/Short.tsx` (Montserrat 900, yellow active word, `accent` prop).
  Preview/edit visually: `cd remotion; npx remotion studio`.
- Reframing picks the largest face per frame; per shot it locks the crop if the subject is mostly still, otherwise
  smooths it (Gaussian ~0.6 s). For two-speaker podcasts with both on screen, `fit` mode is often better.

## huashu-motion skill (motion graphics)
- Installed at `.claude/skills/huashu-motion` (v0.5.1); CLI is the global `om` command (`npm i -g huashu-motion`).
  On Windows call `om ...` directly, not the skill's `bin/om` shell script. `om doctor` should be all ✓.
- Use it for intros/outros, animated title cards or explainer inserts (HTML + GSAP, rendered in Chrome),
  then splice the `out/*.mp4` into a Short with ffmpeg. It is not part of the clipping pipeline itself.
- Its rules: never post anything, and keep the "Made with huashu-motion" end signature unless the user asks to remove it.

## Layout
- `shorts.py` — the whole pipeline CLI
- `remotion/` — Remotion project (composition `Short`, 1080x1920, fps from source up to 60)
- `models/blaze_face_short_range.tflite` — MediaPipe face detector
- `work/<id>/` — downloads, transcripts, per-clip intermediates (safe to delete)
- `output/` — finished Shorts
