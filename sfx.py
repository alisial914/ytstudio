"""Sound effects: a synthesized library + automatic cue placement that follows the action.

Library lives in sfx/<type>.wav (48 kHz stereo). Drop your own WAV with the same name to replace any sound.
Types: whoosh, impact, boom, pop, ding, riser, bassdrop, glitch
"""
import json, re, subprocess
from pathlib import Path
import numpy as np

SR = 48000
DIR = Path(__file__).resolve().parent / "sfx"
TYPES = ["whoosh", "impact", "boom", "pop", "ding", "riser", "bassdrop", "glitch"]
VOLUME = {"whoosh": 0.55, "impact": 0.75, "boom": 0.8, "pop": 0.5, "ding": 0.45,
          "riser": 0.4, "bassdrop": 0.7, "glitch": 0.45}
# words in the speech -> effect (matched on the spoken word, lands on that word)
KEYWORDS = {
    "boom": r"boom|explod\w*|explosion\w*|blew|blow(?:s|ing)?|bomb\w*|nuke\w*|grenade\w*|rpg",
    "impact": r"crash\w*|smash\w*|slam\w*|punch\w*|hit|hits|kick\w*|knock\w*|shot|shoots?|headshot\w*|run(?:s)?over|wreck\w*",
    "ding": r"money|cash|paid|rich|million\w*|dollars?|won|win|wins|loot\w*|bonus|level(?:ed)?up|unlock\w*",
    "bassdrop": r"insane|crazy|unbelievable|wow|whoa|omg|chaos|legendary|massive|huge",
    "glitch": r"dead|died|dies|killed|fail\w*|lost|wasted|busted|broke|glitch\w*|oops",
}


# ------------------------------------------------------------------ synthesis
def _rng(seed):
    return np.random.default_rng(seed)


def _bandpass_sweep(x, f_from, f_to, q=1.2):
    """State-variable band-pass whose centre frequency sweeps exponentially."""
    n = len(x); f = f_from * (f_to / f_from) ** (np.arange(n) / n)
    out = np.zeros(n); low = band = 0.0
    for i in range(n):
        g = 2 * np.sin(np.pi * f[i] / SR)
        high = x[i] - low - band / q
        band += g * high; low += g * band
        out[i] = band
    return out


def _lowpass(x, fc):
    a = np.exp(-2 * np.pi * fc / SR); y = np.zeros_like(x); s = 0.0
    for i in range(len(x)):
        s = (1 - a) * x[i] + a * s; y[i] = s
    return y


def _sweep(f0, f1, dur, curve=3.0):
    t = np.arange(int(dur * SR)) / SR
    f = f1 + (f0 - f1) * np.exp(-curve * t / dur)
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def _env(n, attack, decay):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(attack, 1e-4)) * np.exp(-t / decay)


def _norm(x, peak=0.9):
    return x / (np.max(np.abs(x)) + 1e-9) * peak


def _stereo(x, pan=None):
    if pan is None:
        return np.stack([x, x], 1)
    return np.stack([x * np.cos(pan * np.pi / 2), x * np.sin(pan * np.pi / 2)], 1)


def synth(kind):
    r = _rng(TYPES.index(kind) + 7)
    if kind == "whoosh":
        n = int(0.7 * SR); t = np.arange(n) / n
        x = _bandpass_sweep(r.standard_normal(n), 250, 3500, q=1.6) * np.sin(np.pi * t) ** 2
        x = _norm(x)
        return _stereo(x, pan=0.15 + 0.7 * t)
    if kind == "impact":
        n = int(0.6 * SR)
        thump = _sweep(150, 45, 0.6, 5) * _env(n, 0.002, 0.12)
        crack = _lowpass(r.standard_normal(n), 3500) * _env(n, 0.0005, 0.03)
        return _stereo(_norm(np.tanh(2.5 * (thump + 0.8 * crack))))
    if kind == "boom":
        n = int(2.4 * SR)
        rumble = _lowpass(_lowpass(r.standard_normal(n), 400), 400) * _env(n, 0.005, 0.7)
        sub = _sweep(80, 28, 2.4, 2) * _env(n, 0.003, 0.9)
        crack = _lowpass(r.standard_normal(n), 5000) * _env(n, 0.001, 0.08)
        return _stereo(_norm(np.tanh(1.8 * (_norm(rumble) + sub + 0.6 * crack))))
    if kind == "pop":
        n = int(0.12 * SR)
        return _stereo(_norm(_sweep(900, 220, 0.12, 6) * _env(n, 0.001, 0.035)))
    if kind == "ding":
        n = int(1.4 * SR); t = np.arange(n) / SR
        x = sum(a * np.sin(2 * np.pi * 1318 * m * t) * np.exp(-t / d)
                for m, a, d in [(1, 1, 0.6), (2.76, 0.45, 0.25), (5.4, 0.25, 0.12), (8.93, 0.1, 0.06)])
        return _stereo(_norm(x * np.minimum(1, t / 0.002)))
    if kind == "riser":
        n = int(1.6 * SR); t = np.arange(n) / n
        noise = _bandpass_sweep(r.standard_normal(n), 300, 6000, q=2.5)
        tone = _sweep(160, 1400, 1.6, -2.5)
        return _stereo(_norm((_norm(noise) * 0.7 + 0.4 * tone) * t ** 2.2))
    if kind == "bassdrop":
        n = int(1.5 * SR)
        x = np.tanh(3 * _sweep(140, 32, 1.5, 3) * _env(n, 0.003, 0.7))
        return _stereo(_norm(x + 0.3 * _lowpass(r.standard_normal(n), 1500) * _env(n, 0.001, 0.05)))
    if kind == "glitch":
        n = int(0.45 * SR); x = np.zeros(n); i = 0
        while i < n:
            L = int(r.integers(400, 3000)); f = r.choice([180, 360, 720, 1440, 2880])
            seg = np.sign(np.sin(2 * np.pi * f * np.arange(L) / SR)) * (r.random() > 0.25)
            x[i:i + L] = seg[: max(0, min(L, n - i))]; i += L
        return _stereo(_norm(x * _env(n, 0.001, 0.3), 0.6), pan=0.5)
    raise ValueError(kind)


def ensure_library():
    """Create any missing sfx/<type>.wav (user-supplied files are left alone)."""
    import wave
    DIR.mkdir(exist_ok=True)
    for k in TYPES:
        p = DIR / f"{k}.wav"
        if p.exists():
            continue
        x = (np.clip(synth(k), -1, 1) * 32767).astype(np.int16)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())
    return {k: DIR / f"{k}.wav" for k in TYPES}


def length(kind):
    import wave
    with wave.open(str(DIR / f"{kind}.wav")) as w:
        return w.getnframes() / w.getframerate()


# ------------------------------------------------------------------ cue placement
def _loud_onsets(path, dur):
    """Sudden loudness jumps (gunshots, crashes, screams, explosions) -> list of (t, strength_dB)."""
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    hop = 400  # 50 ms
    if len(a) < hop * 40:
        return []
    frames = a[: len(a) // hop * hop].reshape(-1, hop)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-6)
    out = []
    for i in range(20, len(db)):
        base = np.median(db[i - 20:i])  # previous second
        jump = db[i] - base
        if jump > 10 and db[i] > -28 and db[i] >= db[i - 1] + 4:
            out.append((i * hop / 8000, jump))
    return out


def plan(seg_path, dur, words=(), cuts=(), hook="", claude_cues=()):
    """Return [{"type", "t", "vol"}] for one clip. Priority: Claude > spoken keywords > loud onsets > cuts."""
    cands = []  # (priority, t, type)
    for c in claude_cues or []:
        if c.get("type") in TYPES and 0 <= float(c.get("t", -1)) < dur:
            cands.append((0, float(c["t"]), c["type"]))
    for w in words:
        word = re.sub(r"[^a-z]", "", w["text"].lower())
        for kind, pat in KEYWORDS.items():
            if word and re.fullmatch(pat, word):
                cands.append((1, w["startMs"] / 1000, kind)); break
    for t, jump in sorted(_loud_onsets(seg_path, dur), key=lambda x: -x[1])[:6]:
        cands.append((2, t, "boom" if jump > 20 else "impact"))
    for t in cuts:
        if 0.5 < t < dur - 0.5:
            cands.append((3, max(0, t - 0.3), "whoosh"))
    if hook:
        cands.append((0, 0.0, "whoosh"))

    cues, budget = [], max(2, int(dur / 4))  # about one effect every 4 s at most
    for pri, t, kind in sorted(cands, key=lambda c: (c[0], c[1])):
        if len(cues) >= budget:
            break
        if all(abs(t - c["t"]) >= 1.5 for c in cues):
            cues.append({"type": kind, "t": round(t, 3), "vol": VOLUME[kind]})
    return sorted(cues, key=lambda c: c["t"])


def mix(video_in, video_out, cues):
    """Mix cues into the clip's audio (video stream copied). Risers end on their cue instead of starting there."""
    if not cues:
        return False
    ensure_library()
    inputs, chains, labels = ["-i", str(video_in)], [], []
    for k, c in enumerate(cues, 1):
        start = max(0.0, c["t"] - (length("riser") if c["type"] == "riser" else 0))
        ms = int(start * 1000)
        inputs += ["-i", str(DIR / f"{c['type']}.wav")]
        chains.append(f"[{k}:a]aresample=48000,volume={c['vol']},adelay={ms}|{ms}[s{k}]")
        labels.append(f"[s{k}]")
    fc = ";".join(chains) + f";[0:a]{''.join(labels)}amix=inputs={len(cues) + 1}:normalize=0:duration=first," \
                             f"alimiter=limit=0.7:level=false[a]"  # -3 dB ceiling: AAC + Remotion re-encode overshoot
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(video_out)], check=True)
    return True


if __name__ == "__main__":
    print(json.dumps({k: str(v) for k, v in ensure_library().items()}, indent=1))
