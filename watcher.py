"""Live-stream watcher: paste a YouTube live (or upcoming live) URL into stream_url.txt and save.

  - waits for the stream to go live if it hasn't started yet
  - records it from "now" in CHUNK-second pieces while it is still running (yt-dlp resolves the HLS feed,
    ffmpeg copies it into chunks with no re-encode; the feed URL is re-resolved if it expires or drops)
  - every finished chunk -> transcribe -> pick the best moments (Claude CLI, or the built-in picker)
    -> face-tracked/fit 9:16 reframe + captions + sound effects -> output/<stream-id>-cNNN-<name>.mp4
  - stops when the stream ends, or when you clear stream_url.txt / paste a different URL

Run: .venv\\Scripts\\python.exe watcher.py [--chunk 300] [--clips-per-chunk 2] [--picker auto|claude|heuristic]
Log: work/live/watcher.log
"""
import argparse, json, logging, os, queue, re, shutil, subprocess, sys, threading, time
from pathlib import Path

import shorts  # sets up CA bundle, paths, pipeline functions

ROOT = shorts.ROOT
URL_FILE = ROOT / "stream_url.txt"
LIVE = shorts.WORK / "live"
YT = [sys.executable, "-m", "yt_dlp", "--compat-options", "no-certifi", "--no-playlist", "--no-warnings"]
# watch?v=…, youtu.be/…, youtube.com/live/…, youtube.com/@channel/live, youtube.com/channel/UC…/live
URL_RE = re.compile(r"https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/[^\s#]+")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

log = logging.getLogger("watcher")


def read_url():
    try:
        text = URL_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            m = URL_RE.search(line)
            if m:
                return m.group(0)
    return None


def yt_info(url):
    r = subprocess.run(YT + ["-J", url], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "yt-dlp failed")
    return json.loads(r.stdout)


# ======================================================================== recorder
class Recorder(threading.Thread):
    def __init__(self, url, chunk, jobs):
        super().__init__(daemon=True)
        self.url, self.chunk, self.jobs = url, chunk, jobs
        self.stop_evt = threading.Event()
        self.proc = None
        self.vid = None

    def stop(self):
        self.stop_evt.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q"); self.proc.stdin.flush()   # graceful: ffmpeg closes the current chunk
                self.proc.wait(15)
            except Exception:
                self.proc.kill()

    def _wait_until_live(self):
        while not self.stop_evt.is_set():
            try:
                info = yt_info(self.url)
            except Exception as e:
                msg = str(e)
                if "will begin" in msg or "Premieres" in msg or "This live event" in msg or "is_upcoming" in msg:
                    log.info("stream hasn't started yet - checking again in 30 s (%s)", msg[-120:])
                else:
                    log.warning("could not read stream info (%s) - retrying in 30 s", msg[-200:])
                self.stop_evt.wait(30); continue
            status = info.get("live_status")
            self.vid = info["id"]
            if status == "is_live":
                log.info("LIVE: %s - %s", info.get("title"), info.get("channel"))
                return info
            if status == "is_upcoming":
                log.info("upcoming stream '%s' - waiting for it to start", info.get("title"))
                self.stop_evt.wait(30); continue
            log.error("this URL is not a live stream (live_status=%s). For finished videos use shorts.py.", status)
            return None
        return None

    def _feed_urls(self):
        r = subprocess.run(YT + ["-g", "-f", "b/bv*+ba", self.url], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        urls = [u for u in r.stdout.split() if u.startswith("http")]
        if not urls:
            raise RuntimeError(r.stderr.strip()[-300:] or "no stream URL")
        return urls

    def run(self):
        info = self._wait_until_live()
        if not info:
            return
        base = LIVE / self.vid
        cdir = base / "chunks"
        cdir.mkdir(parents=True, exist_ok=True)
        (base / "info.json").write_text(json.dumps({k: info.get(k) for k in ("id", "title", "channel", "webpage_url")},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
        queued = {p.name for p in cdir.glob("*.ts")}  # leftovers from a previous run are not re-queued mid-write
        fails = 0
        while not self.stop_evt.is_set():
            try:
                urls = self._feed_urls()
            except Exception as e:
                fails += 1
                log.warning("couldn't get the live feed (%s)", str(e)[-200:])
                if self._ended():
                    break
                self.stop_evt.wait(min(60, 10 * fails)); continue
            n = max([int(p.stem.split("_")[1]) for p in cdir.glob("chunk_*.ts")] + [0]) + 1
            inputs = []
            for u in urls:
                inputs += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                           "-reconnect_delay_max", "15", "-i", u]
            maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(urls) > 1 else ["-map", "0:v:0", "-map", "0:a:0?"]
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, *maps, "-c", "copy",
                   "-f", "segment", "-segment_time", str(self.chunk), "-reset_timestamps", "1",
                   "-segment_start_number", str(n), str(cdir / "chunk_%05d.ts")]
            log.info("recording -> %s (chunks of %ss, starting at #%d)", cdir, self.chunk, n)
            errlog = base / "ffmpeg.log"
            with open(errlog, "wb") as ef:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ef, creationflags=NO_WINDOW)
                started = time.time()
                while self.proc.poll() is None:
                    self._queue_finished(cdir, queued, final=False)
                    time.sleep(3)
            err = errlog.read_text(errors="replace").strip()[-300:] if errlog.exists() else ""
            self._queue_finished(cdir, queued, final=True)
            if self.stop_evt.is_set():
                break
            if err:
                log.warning("ffmpeg stopped: %s", err[-300:])
            fails = 0 if time.time() - started > 120 else fails + 1
            if self._ended():
                break
            log.info("feed dropped but stream is still live - reconnecting")
            self.stop_evt.wait(min(30, 3 * fails))
        log.info("recorder for %s finished", self.vid)

    def _ended(self):
        try:
            st = yt_info(self.url).get("live_status")
        except Exception:
            return False
        if st != "is_live":
            log.info("stream is over (live_status=%s) - last chunks will still be processed", st)
            return True
        return False

    def _queue_finished(self, cdir, queued, final):
        files = sorted(cdir.glob("chunk_*.ts"))
        done = files if final else files[:-1]  # newest file is still being written while ffmpeg runs
        for f in done:
            if f.name not in queued:
                queued.add(f.name)
                self.jobs.put((self.vid, f))
                log.info("chunk ready: %s (%.0f MB)", f.name, f.stat().st_size / 1e6)


# ======================================================================== clip picking
PICK_PROMPT = """You are an expert short-form video editor. Below is the transcript of one {dur:.0f}-second chunk of a
LIVE stream ("{title}" by {channel}). Timestamps are seconds from the start of this chunk.

Pick up to {n} moments that would make great YouTube Shorts / TikToks. Rules:
- each 20-50 s (sweet spot 25-40), fully inside 0..{dur:.0f}
- starts on the first word of a strong line (no warm-up), ends right after the payoff
- must make sense on its own; prefer funny, shocking, hype, emotional, clutch plays, fails, bold claims
- skip dead air, "be right back", chat reading with no payoff, sponsor reads. If nothing is good, return []
- "hook": a curiosity-driven title of at most 7 words (no lies, no emojis)
- "sfx": sound effects that match the ACTION at that moment, absolute chunk seconds. Types:
  whoosh (transitions, sudden movement), impact (hits, crashes, punches, shots), boom (explosions, huge moments),
  pop (something appears), ding (money, wins, correct answers), riser (build-up, place it where the payoff lands),
  bassdrop (hype / "no way" moments), glitch (fails, deaths, mistakes). At most one per 4 seconds.

Reply with ONLY a JSON array, no prose:
[{{"name":"kebab-case-title","start":12.3,"end":45.6,"hook":"...","sfx":[{{"type":"impact","t":20.1}}]}}]

TRANSCRIPT:
{transcript}
"""


def claude_pick(cdir, n, dur, meta):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not found")
    lines = []
    for s in json.loads((cdir / "transcript.json").read_text(encoding="utf-8"))["segments"]:
        lines.append(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    prompt = PICK_PROMPT.format(n=n, dur=dur, title=meta.get("title", ""), channel=meta.get("channel", ""),
                                transcript="\n".join(lines) or "(no speech detected)")
    r = subprocess.run([claude, "-p", "--output-format", "text"], input=prompt, capture_output=True, text=True,
                       encoding="utf-8", timeout=240, cwd=str(cdir), creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError((r.stdout + r.stderr).strip()[-300:])
    m = re.search(r"\[.*\]", r.stdout, re.S)
    if not m:
        raise RuntimeError("no JSON in Claude reply: " + r.stdout[:200])
    return json.loads(m.group(0))


HYPE = re.compile(r"no way|let'?s go|oh my god|what the|insane|crazy|clutch|holy|wow|bro|dude|yes+|no+|run|watch this|"
                  r"boom|dead|kill|win|won|lost|money|million|chat", re.I)


def heuristic_pick(cdir, n, dur):
    """No-Claude fallback: score speech windows by energy, excitement words and loudness."""
    import numpy as np
    segs = json.loads((cdir / "transcript.json").read_text(encoding="utf-8"))["segments"]
    src = shorts.source_of(cdir.relative_to(shorts.WORK).as_posix())
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True, creationflags=NO_WINDOW).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    sec = np.sqrt((a[: len(a) // 8000 * 8000].reshape(-1, 8000) ** 2).mean(1)) + 1e-6 if len(a) >= 8000 else np.ones(1)
    db = 20 * np.log10(sec)
    z = (db - db.mean()) / (db.std() + 1e-6)

    def loud(s, e):
        return float(z[int(s):max(int(s) + 1, int(e))].mean()) if len(z) else 0

    cands = []
    # action with little talking (gameplay, crowds, ceremonies): loudest ~30 s windows compete too
    # ...but only if the audio actually has dynamics (a static ambience/"waiting" shot has none)
    if len(db) > 30 and db.std() > 4:
        for s in range(0, max(1, int(dur) - 25), 5):
            cands.append((1.5 * loud(s, s + 30) - 0.5, float(s), min(dur, s + 30.0), True))
    for i, s in enumerate(segs):
        e_idx = i
        while e_idx + 1 < len(segs) and segs[e_idx + 1]["end"] - s["start"] <= 45:
            e_idx += 1
        st, en = s["start"], segs[e_idx]["end"]
        if en - st < 20:
            continue
        text = " ".join(x["text"] for x in segs[i:e_idx + 1])
        words = sum(len(x["words"]) for x in segs[i:e_idx + 1])
        score = 0.6 * len(HYPE.findall(text)) + text.count("!") + 0.15 * words / (en - st) * 10 + 1.5 * loud(st, en)
        cands.append((score, st, en, False))
    picks = []
    for score, st, en, raw in sorted(cands, reverse=True):
        if len(picks) >= n:
            break
        if all(en <= p["start"] or st >= p["end"] for p in picks):
            # raw = loudness window: keep its exact span (snapping to sparse words would shrink it)
            picks.append({"name": f"moment-{int(st // 60):02d}m{int(st % 60):02d}s", "start": st, "end": en, "hook": "",
                          "no_captions": not segs, "raw": raw})
    return picks


def snap(clip, cdir, dur):
    """Snap to word boundaries (start 0.1 s before the first word, end 0.3 s after the last)."""
    words = [w for s in json.loads((cdir / "transcript.json").read_text(encoding="utf-8"))["segments"] for w in s["words"]]
    st, en = max(0.0, float(clip["start"])), min(dur, float(clip["end"]))
    if words:
        inside = [w for w in words if w["end"] > st + 0.05 and w["start"] < en - 0.05]
        if inside:
            st, en = max(0.0, inside[0]["start"] - 0.1), min(dur, inside[-1]["end"] + 0.3)
    return st, en


# ======================================================================== chunk worker
def process_chunk(vid, chunk_file, args):
    meta = json.loads((LIVE / vid / "info.json").read_text(encoding="utf-8"))
    num = chunk_file.stem.split("_")[1]
    cdir = LIVE / vid / f"c{num}"
    cdir.mkdir(parents=True, exist_ok=True)
    src = cdir / "source.ts"
    if not src.exists():
        shutil.move(str(chunk_file), src)
    dur = float(shorts.probe(src)["format"].get("duration", 0))
    if dur < 20:
        log.info("c%s: only %.0f s long - skipped", num, dur); return
    cid = cdir.relative_to(shorts.WORK).as_posix()
    log.info("c%s: transcribing %.0f s", num, dur)
    if not (cdir / "transcript.json").exists():
        shorts.cmd_transcribe(argparse.Namespace(id=cid, model=args.model, language=args.language))

    picks, how = None, "heuristic"
    if args.picker in ("auto", "claude"):
        try:
            picks, how = claude_pick(cdir, args.clips_per_chunk, dur, meta), "claude"
        except Exception as e:
            log.warning("c%s: Claude picking unavailable (%s) - using built-in picker", num, str(e)[-160:])
    if picks is None:
        picks = heuristic_pick(cdir, args.clips_per_chunk, dur)
    picks = picks[: args.clips_per_chunk]
    (cdir / "clips.json").write_text(json.dumps(picks, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("c%s: %d clip(s) picked by %s", num, len(picks), how)

    for c in picks:
        try:
            # heuristic loudness windows keep their exact span; speech picks snap to word boundaries
            st, en = (max(0.0, c["start"]), min(dur, c["end"])) if c.get("raw") else snap(c, cdir, dur)
            if en - st < 12:
                log.info("c%s: '%s' too short after snapping - skipped", num, c.get("name")); continue
            name = re.sub(r"[^a-z0-9-]+", "-", f"{vid}-c{num}-{c.get('name', 'clip')}".lower()).strip("-")[:90]
            cues = [{"type": q.get("type"), "t": float(q.get("t", 0)) - st} for q in (c.get("sfx") or [])]
            shorts.cmd_render(argparse.Namespace(
                id=cid, start=st, end=en, name=name, hook=c.get("hook", ""), mode=args.mode, accent=args.accent,
                no_captions=bool(c.get("no_captions")), keep=False, no_sfx=args.no_sfx, sfx=cues))
            log.info("SHORT READY -> output/%s.mp4", name)
        except BaseException as e:  # noqa - never let one clip kill the watcher
            if isinstance(e, KeyboardInterrupt):
                raise
            log.exception("c%s: render of '%s' failed: %s", num, c.get("name"), e)
    if not args.keep_chunks:
        src.unlink(missing_ok=True)


def worker(jobs, args):
    while True:
        vid, f = jobs.get()
        try:
            process_chunk(vid, f, args)
        except Exception:
            log.exception("chunk %s failed", f.name)
        finally:
            jobs.task_done()


# ======================================================================== main loop
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunk", type=int, default=300, help="seconds per recorded chunk (default 300)")
    ap.add_argument("--clips-per-chunk", type=int, default=2)
    ap.add_argument("--picker", choices=["auto", "claude", "heuristic"], default="auto")
    ap.add_argument("--mode", choices=["auto", "crop", "fit"], default="auto")
    ap.add_argument("--accent", default="#FFD400")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--language", default=None)
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--keep-chunks", action="store_true", help="keep raw chunk recordings after processing")
    args = ap.parse_args()

    LIVE.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(LIVE / "watcher.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    (LIVE / "watcher.pid").write_text(str(os.getpid()))
    if not URL_FILE.exists():
        URL_FILE.write_text("# Paste a YouTube live stream URL on the next line and save.\n", encoding="utf-8")

    import sfx; sfx.ensure_library()
    jobs = queue.Queue()
    threading.Thread(target=worker, args=(jobs, args), daemon=True).start()
    log.info("watching %s (chunk %ss, %d clip(s)/chunk, picker=%s)", URL_FILE, args.chunk, args.clips_per_chunk, args.picker)

    rec, current, last_mtime = None, None, None
    try:
        while True:
            try:
                mtime = URL_FILE.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if mtime != last_mtime:
                last_mtime = mtime
                url = read_url()
                if url != current:
                    if rec:
                        log.info("stopping recording of %s", current); rec.stop()
                    current, rec = url, None
                    if url:
                        log.info("new URL: %s", url)
                        rec = Recorder(url, args.chunk, jobs); rec.start()
                    else:
                        log.info("no URL in stream_url.txt - idle")
            if rec and not rec.is_alive() and current:
                log.info("recording ended; %d chunk(s) still in the queue. Save stream_url.txt again to restart.",
                         jobs.qsize())
                rec, current = None, None
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("shutting down")
        if rec:
            rec.stop()


if __name__ == "__main__":
    main()
