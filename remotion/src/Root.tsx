import { Composition } from "remotion";
import { Short, ShortProps } from "./Short";

const FPS = 30;

export const RemotionRoot = () => (
  <Composition
    id="Short"
    component={Short}
    width={1080}
    height={1920}
    fps={FPS}
    durationInFrames={FPS * 30}
    defaultProps={{
      video: "clip.mp4",
      captions: "captions.json",
      hook: "",
      durationSec: 30,
      fps: 30,
      accent: "#FFD400",
      layout: "crop",
    } satisfies ShortProps}
    calculateMetadata={({ props }) => ({
      fps: props.fps,
      durationInFrames: Math.max(1, Math.round(props.durationSec * props.fps)),
    })}
  />
);
