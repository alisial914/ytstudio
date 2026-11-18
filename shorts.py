"""YouTube -> vertical Shorts pipeline.

  python shorts.py download <url>                 -> work/<id>/source.*, info.json
  python shorts.py transcribe <id>                -> work/<id>/transcript.json + transcript.txt
  python shorts.py render <id> --start S --end E --name NAME [--hook TEXT] [--mode crop|fit] [--no-captions]
                                                  -> output/NAME.mp4
  python shorts.py batch <id> clips.json          -> renders every clip in a clips.json list

Run with the project venv: .venv\\Scripts\\python.exe shorts.py ...
"""
import argparse, json, os, shutil, subprocess, sys, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK, OUT, REMOTION = ROOT / "work", ROOT / "output", ROOT / "remotion"
VENV_BIN = Path(sys.executable).parent
W, H = 1080, 1920


def _ca_bundle():
    """This machine's HTTPS is intercepted (antivirus/proxy): trust the Windows cert store + certifi."""
    import ssl, certifi
    pem = ROOT / "models" / "ca-bundle.pem"
    if os.name == "nt":
        certs = [ssl.DER_cert_to_PEM_cert(c) for st in ("ROOT", "CA") for c, enc, _ in ssl.enum_certificates(st)
                 if enc == "x509_asn"]
        pem.parent.mkdir(exist_ok=True)
        pem.write_text(Path(certifi.where()).read_text() + "\n" + "\n".join(certs))
        for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ[k] = str(pem)


_ca_bundle()


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd)[:300], flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def source_of(vid):
    files = [f for f in glob.glob(str(WORK / vid / "source.*")) if not f.endswith((".json", ".part"))]
    if not files:
        sys.exit(f"No source video in work/{vid}. Run download first.")
    return Path(files[0])


# ---------------------------------------------------------------- download
def cmd_download(a):
    info = json.loads(subprocess.run([sys.executable, "-m", "yt_dlp", "--compat-options", "no-certifi", "-J", "--no-playlist", a.url],
                                     capture_output=True, text=True, check=True).stdout)
    vid = info["id"]
    d = WORK / vid
    d.mkdir(parents=True, exist_ok=True)
    # Best video (prefer highest res/fps, then better codec) + best audio, merged without re-encoding.
    run([sys.executable, "-m", "yt_dlp", "--compat-options", "no-certifi", "--no-playlist", "-f", "bv*+ba[format_id!*=drc]/bv*+ba/b",
         "-S", "res,fps,hdr:12,vcodec:av01,acodec:opus,abr",
         "--merge-output-format", "mkv", "--write-info-json", "--embed-chapters",
         "-o", str(d / "source.%(ext)s"), a.url])
    p = probe(source_of(vid))
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    print(f"\nDownloaded {vid}: {v['width']}x{v['height']} {v['codec_name']} @ {v.get('r_frame_rate')}  "
          f"({float(p['format']['duration']):.0f}s)  -> {d}")
    print(f"ID={vid}")


# ---------------------------------------------------------------- transcribe
def _cuda_dlls():
    import site
    for sp in site.getsitepackages():
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
            p = Path(sp) / sub
            if p.exists():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]


_MODELS = {}  # kept loaded between calls (the live watcher transcribes many chunks in one process)


def _whisper(name):
    if name not in _MODELS:
        _cuda_dlls()
        from faster_whisper import WhisperModel
        try:
            _MODELS[name] = WhisperModel(name, device="cuda", compute_type="float16")
        except Exception as e:  # GPU unavailable -> CPU fallback
            print("CUDA failed, falling back to CPU:", e)
            _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODELS[name]


def cmd_transcribe(a):
    d, src = WORK / a.id, source_of(a.id)
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    model = _whisper(a.model)
    segs, info = model.transcribe(audio,word_timestamps=True, vad_filter=True, beam_size=5,
                                  language=a.language, condition_on_previous_text=False)
    out, lines = [], []
    for s in segs:
        words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "p": round(w.probability, 3)}
                 for w in (s.words or [])]
        out.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(), "words": words})
        m, sec = divmod(s.start, 60)
        lines.append(f"[{int(m):02d}:{sec:05.2f}] {s.text.strip()}")
        print(lines[-1], flush=True)
    (d / "transcript.json").write_text(json.dumps({"language": info.language, "segments": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nTranscript -> {d / 'transcript.txt'}")


# ---------------------------------------------------------------- reframe
def _face_detector(video_mode):
    from mediapipe.tasks.python import vision, BaseOptions
    return vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(ROOT / "models" / "blaze_face_short_range.tflite")),
        running_mode=vision.RunningMode.VIDEO if video_mode else vision.RunningMode.IMAGE,
        min_detection_confidence=0.5))


def _auto_mode(seg):
    """crop if a reasonably large face is on screen most of the time (talking head / podcast), else fit
    (gameplay, small facecam in a corner, screen recordings, wide shots)."""
    import cv2, mediapipe as mp
    cap = cv2.VideoCapture(str(seg))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    det, hits, samples = _face_detector(False), 0, 15
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / samples))
        ok, f = cap.read()
        if not ok:
            continue
        small = cv2.cvtColor(cv2.resize(f, (640, int(640 * f.shape[0] / f.shape[1]))), cv2.COLOR_BGR2RGB)
        res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=small))
        if any(d.bounding_box.width >= 0.07 * 640 for d in res.detections):
            hits += 1
    cap.release(); det.close()
    mode = "crop" if hits >= 0.6 * samples else "fit"
    print(f"  auto mode: face in {hits}/{samples} samples -> {mode}")
    return mode


def _cuts(seg):
    """Scene-cut times in seconds."""
    from scenedetect import detect, ContentDetector
    return [s.get_seconds() for s, _ in detect(str(seg), ContentDetector(threshold=27))[1:]]


def _face_track(seg_path, fps, n_frames, src_w, src_h, crop_w):
    """Return per-frame crop x-offsets: face-tracked, shot-aware, smoothed."""
    import cv2, numpy as np, mediapipe as mp
    from mediapipe.tasks.python import vision, BaseOptions
    from scenedetect import detect, ContentDetector

    shots = [(s.get_frames(), e.get_frames()) for s, e in detect(str(seg_path), ContentDetector(threshold=27))]
    if not shots:
        shots = [(0, n_frames)]
    det = _face_detector(True)
    cap = cv2.VideoCapture(str(seg_path))
    scale = 640 / src_w
    cx = np.full(n_frames, np.nan)
    for i in range(n_frames):
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.cvtColor(cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        res = det.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=small), int(i * 1000 / fps))
        if res.detections:
            b = max(res.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height).bounding_box
            cx[i] = (b.origin_x + b.width / 2) / scale
    cap.release()
    det.close()

    xs = np.full(n_frames, src_w / 2)
    sigma = fps * 0.6
    k = np.exp(-0.5 * (np.arange(-3 * sigma, 3 * sigma + 1) / sigma) ** 2); k /= k.sum()
    for s, e in shots:
        e = min(e, n_frames)
        c = cx[s:e]
        if e <= s or np.all(np.isnan(c)):
            continue  # no face in shot -> centre crop
        idx = np.arange(len(c)); good = ~np.isnan(c)
        c = np.interp(idx, idx[good], c[good])
        if np.std(c) < 0.05 * src_w:
            c[:] = np.median(c)            # mostly still subject -> locked-off "camera"
        else:
            pad = len(k) // 2
            c = np.convolve(np.pad(c, pad, mode="edge"), k, mode="valid")[: len(c)]
        xs[s:e] = c
    print(f"  face found in {np.mean(~np.isnan(cx)) * 100:.0f}% of frames, {len(shots)} shot(s)")
    return np.clip(xs - crop_w / 2, 0, src_w - crop_w).astype(int)


def _vertical(seg, dst, fps, mode):
    import cv2, numpy as np
    v = next(s for s in probe(seg)["streams"] if s["codec_type"] == "video")
    sw, sh, n = v["width"], v["height"], int(v.get("nb_frames") or 0)
    if not n:
        n = int(float(probe(seg)["format"]["duration"]) * fps)
    # short GOP so Remotion can seek frames quickly (avoids delayRender timeouts at 60fps)
    enc = ["-c:v", "libx264", "-preset", "slow", "-crf", "10", "-g", str(fps), "-pix_fmt", "yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if mode == "fit" or sw / sh < 1:  # full frame over blurred background (screen content, already-vertical, etc.)
        vf = (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,eq=brightness=-0.08[bg];"
              f"[b]scale={W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        run(["ffmpeg", "-y", "-v", "error", "-stats", "-i", seg, "-filter_complex", vf, "-an", *enc, dst])
        return
    crop_w = int(round(sh * 9 / 16 / 2) * 2)
    xs = _face_track(seg, fps, n, sw, sh, crop_w)
    cap = cv2.VideoCapture(str(seg))
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", *enc, str(dst)], stdin=subprocess.PIPE)
    for i in range(n):
        ok, f = cap.read()
        if not ok:
            break
        x = xs[min(i, len(xs) - 1)]
        p.stdin.write(cv2.resize(f[:, x:x + crop_w], (W, H), interpolation=cv2.INTER_LANCZOS4).tobytes())
    cap.release(); p.stdin.close(); p.wait()


# ---------------------------------------------------------------- render
def cmd_render(a):
    d, src = WORK / a.id, source_of(a.id)
    name = a.name
    cd = d / "clips" / name
    cd.mkdir(parents=True, exist_ok=True)
    v = next(s for s in probe(src)["streams"] if s["codec_type"] == "video")
    num, den = map(int, v["r_frame_rate"].split("/"))
    fps = min(60, round(num / den)) or 30
    dur = a.end - a.start

    print(f"[1/4] cutting {a.start:.2f}-{a.end:.2f}s ({dur:.1f}s) @ {fps}fps")
    seg = cd / "seg.mkv"
    run(["ffmpeg", "-y", "-v", "error", "-stats", "-ss", a.start, "-i", src, "-t", dur, "-r", fps,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "8", "-pix_fmt", "yuv420p",
         "-af", "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "48000", "-c:a", "pcm_s16le", seg])

    mode = _auto_mode(seg) if a.mode == "auto" else a.mode
    print(f"[2/4] reframing to 9:16 ({mode})")
    vert = cd / "vertical.mp4"
    _vertical(seg, vert, fps, mode)
    clip = cd / "clip.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", vert, "-i", seg, "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-shortest", "-movflags", "+faststart", clip])

    print("[3/4] building captions")
    caps = []
    if not a.no_captions:
        tj = d / "transcript.json"
        if not tj.exists():
            sys.exit("No transcript.json - run transcribe first (or pass --no-captions).")
        for s in json.loads(tj.read_text(encoding="utf-8"))["segments"]:
            for w in s["words"]:
                if w["end"] > a.start and w["start"] < a.end:
                    st, en = max(0, w["start"] - a.start), min(dur, w["end"] - a.start)
                    txt = w["word"] if w["word"].startswith(" ") else " " + w["word"]
                    caps.append({"text": txt, "startMs": int(st * 1000), "endMs": int(en * 1000),
                                 "timestampMs": int((st + en) * 500), "confidence": w.get("p")})
        if caps:
            caps[0]["text"] = caps[0]["text"].lstrip()

    if not getattr(a, "no_sfx", False):
        import sfx
        cues = sfx.plan(seg, dur, words=caps, cuts=_cuts(seg), hook=a.hook, claude_cues=getattr(a, "sfx", None))
        print("  sfx: " + (", ".join(f"{c['type']}@{c['t']:.1f}s" for c in cues) or "none"))
        if cues:
            mixed = cd / "clip_sfx.mp4"
            sfx.mix(clip, mixed, cues)
            mixed.replace(clip)
        (cd / "sfx.json").write_text(json.dumps(cues), encoding="utf-8")

    pub = REMOTION / "public" / a.id / name
    pub.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip, pub / "clip.mp4")
    (pub / "captions.json").write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    rel = f"{a.id}/{name}"
    props = {"video": f"{rel}/clip.mp4", "captions": f"{rel}/captions.json" if caps else "",
             "hook": a.hook or "", "durationSec": round(dur, 3), "fps": fps, "accent": a.accent, "layout": mode}
    pf = cd / "props.json"
    pf.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    print("[4/4] rendering with Remotion")
    OUT.mkdir(exist_ok=True)
    final = OUT / f"{name}.mp4"
    env = dict(os.environ, NODE_OPTIONS="--use-system-ca")
    render = ["npx.cmd" if os.name == "nt" else "npx", "remotion", "render", "src/index.ts", "Short", final,
              f"--props={pf}", "--timeout=120000"]
    try:
        run(render + ["--concurrency=50%"], cwd=REMOTION, env=env)
    except subprocess.CalledProcessError:
        print("  Remotion render failed - retrying with lower concurrency")
        run(render + ["--concurrency=4"], cwd=REMOTION, env=env)
    shutil.rmtree(pub, ignore_errors=True)
    if not a.keep:
        for f in (seg, vert):
            f.unlink(missing_ok=True)
    print(f"\nDONE -> {final}")


def cmd_batch(a):
    clips = json.loads(Path(a.clips).read_text(encoding="utf-8"))
    failed = []
    for c in clips:
        if (OUT / f"{c['name']}.mp4").exists() and not a.force:
            print(f"skip {c['name']} (already in output/, use --force to redo)")
            continue
        try:
            cmd_render(argparse.Namespace(id=a.id, start=float(c["start"]), end=float(c["end"]), name=c["name"],
                                          hook=c.get("hook", ""), mode=c.get("mode", "crop"), accent=c.get("accent", "#FFD400"),
                                          no_captions=c.get("no_captions", False), keep=False,
                                          no_sfx=c.get("no_sfx", False), sfx=c.get("sfx")))
        except Exception as e:
            print(f"FAILED {c['name']}: {e}")
            failed.append(c["name"])
    print(f"\nBatch done. {len(clips) - len(failed)}/{len(clips)} ok" + (f"; failed: {', '.join(failed)}" if failed else ""))
    if failed:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("download"); p.add_argument("url"); p.set_defaults(fn=cmd_download)
    p = sp.add_parser("transcribe"); p.add_argument("id"); p.add_argument("--model", default="large-v3")
    p.add_argument("--language", default=None); p.set_defaults(fn=cmd_transcribe)
    p = sp.add_parser("render"); p.add_argument("id")
    p.add_argument("--start", type=float, required=True); p.add_argument("--end", type=float, required=True)
    p.add_argument("--name", required=True); p.add_argument("--hook", default="")
    p.add_argument("--mode", choices=["auto", "crop", "fit"], default="auto"); p.add_argument("--accent", default="#FFD400")
    p.add_argument("--no-captions", action="store_true"); p.add_argument("--keep", action="store_true")
    p.add_argument("--no-sfx", action="store_true")
    p.set_defaults(fn=cmd_render)
    p = sp.add_parser("batch"); p.add_argument("id"); p.add_argument("clips"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_batch)
    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
