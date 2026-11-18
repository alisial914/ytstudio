import { Config } from "@remotion/cli/config";

// Highest-quality defaults for final Shorts renders
Config.setVideoImageFormat("png");
Config.setCodec("h264");
Config.setCrf(14);
Config.setPixelFormat("yuv420p");
Config.setColorSpace("bt709");
Config.setOverwriteOutput(true);
Config.setConcurrency("75%");
Config.setX264Preset("slow");
Config.setAudioBitrate("320k");
