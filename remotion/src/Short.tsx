import { useEffect, useMemo, useState } from "react";
import {
  AbsoluteFill, OffthreadVideo, Sequence, continueRender, delayRender,
  interpolate, spring, staticFile, useCurrentFrame, useVideoConfig,
} from "remotion";
import type { Caption } from "@remotion/captions";
import { loadFont } from "@remotion/google-fonts/Montserrat";

const { fontFamily } = loadFont("normal", { weights: ["800", "900"] });

export type ShortProps = {
  video: string;        // file in public/
  captions: string;     // Remotion Caption[] JSON in public/ ("" = none)
  hook: string;         // headline ("" = none)
  durationSec: number;
  fps: number;
  accent: string;
  layout: "crop" | "fit"; // fit = 16:9 band in the middle: captions go below it, hook stays on top all clip
};

type Page = { startMs: number; endMs: number; words: Caption[] };

// Readable pages: max 4 words / 1.6 s, always break after sentence punctuation or a pause.
const MAX_WORDS = 4, MAX_MS = 1600, GAP_MS = 350;
const paginate = (caps: Caption[]): Page[] => {
  const pages: Page[] = [];
  let cur: Caption[] = [];
  const flush = () => {
    if (cur.length) pages.push({ startMs: cur[0].startMs, endMs: cur[cur.length - 1].endMs, words: cur });
    cur = [];
  };
  caps.forEach((c, i) => {
    if (cur.length) {
      const prev = cur[cur.length - 1];
      if (cur.length >= MAX_WORDS || c.endMs - cur[0].startMs > MAX_MS || c.startMs - prev.endMs > GAP_MS
          || /[.!?,;:]$/.test(prev.text.trim())) flush();
    }
    cur.push(c);
    if (i === caps.length - 1) flush();
  });
  return pages;
};

const useCaptions = (file: string) => {
  const [caps, setCaps] = useState<Caption[] | null>(null);
  const [handle] = useState(() => delayRender("captions"));
  useEffect(() => {
    if (!file) { setCaps([]); continueRender(handle); return; }
    fetch(staticFile(file)).then((r) => r.json())
      .then((c: Caption[]) => { setCaps(c); continueRender(handle); })
      .catch(() => { setCaps([]); continueRender(handle); });
  }, [file, handle]);
  return caps;
};

const PageView = ({ page, accent, layout }: { page: Page; accent: string; layout: ShortProps["layout"] }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = page.startMs + (frame / fps) * 1000;
  const pop = spring({ frame, fps, config: { damping: 14, stiffness: 220 }, durationInFrames: 8 });
  const pos = layout === "fit"
    ? { justifyContent: "flex-start", paddingTop: 1330 }   // below the 16:9 band (y 656–1264)
    : { justifyContent: "flex-end", paddingBottom: 520 };
  return (
    <AbsoluteFill style={{ alignItems: "center", ...pos }}>
      <div style={{
        fontFamily, fontWeight: 900, fontSize: 84, lineHeight: 1.1, textAlign: "center",
        maxWidth: 960, textTransform: "uppercase", color: "white",
        WebkitTextStroke: "14px black", paintOrder: "stroke fill",
        textShadow: "0 8px 24px rgba(0,0,0,0.55)",
        transform: `scale(${interpolate(pop, [0, 1], [0.85, 1])})`,
      }}>
        {page.words.map((w, i) => {
          const active = t >= w.startMs && (t < w.endMs || i === page.words.length - 1);
          return (
            <span key={w.startMs} style={{ color: active ? accent : "white" }}>
              {(i ? " " : "") + w.text.trim()}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const Hook = ({ text, accent, persist }: { text: string; accent: string; persist: boolean }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const inn = spring({ frame, fps, config: { damping: 12 } });
  const out = persist ? 1
    : interpolate(frame, [fps * 2.2, fps * 2.6], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: persist ? "center" : "flex-start",
      paddingTop: persist ? 0 : 260, height: persist ? 656 : "100%" }}>
      <div style={{
        fontFamily, fontWeight: 900, fontSize: 72, lineHeight: 1.15, textAlign: "center", maxWidth: 940,
        background: accent, color: "black", padding: "20px 36px", borderRadius: 20,
        transform: `translateY(${interpolate(inn, [0, 1], [-60, 0])}px) scale(${inn})`, opacity: out,
        boxShadow: "0 12px 40px rgba(0,0,0,0.4)",
      }}>{text}</div>
    </AbsoluteFill>
  );
};

export const Short = ({ video, captions, hook, accent, layout = "crop" }: ShortProps) => {
  const { fps, durationInFrames } = useVideoConfig();
  const caps = useCaptions(captions);
  const pages = useMemo(() => (caps ? paginate(caps) : []), [caps]);
  const persist = layout === "fit";

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      <OffthreadVideo src={staticFile(video)} />
      {pages.map((p, i) => {
        const from = Math.round((p.startMs / 1000) * fps);
        const end = Math.min(pages[i + 1]?.startMs ?? Infinity, p.endMs + 600);
        const dur = Math.max(1, Math.round(((end - p.startMs) / 1000) * fps));
        return <Sequence key={i} from={from} durationInFrames={dur}><PageView page={p} accent={accent} layout={layout} /></Sequence>;
      })}
      {hook ? (
        <Sequence durationInFrames={persist ? durationInFrames : Math.round(fps * 2.6)}>
          <Hook text={hook} accent={accent} persist={persist} />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
