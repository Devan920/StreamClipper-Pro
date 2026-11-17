# ⚡ StreamClipper Pro

<div align="center">

![StreamClipper Pro Icon](assets/app_icon.png)

### Autonomous AI-Powered Creative Studio for Real-Time Live Stream & Video Highlight Clipping

[![Platform](https://img.shields.io/badge/Platform-Windows%2011%20%7C%2010-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Devan920/StreamClipper-Pro)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt-41CD52?style=for-the-badge&logo=qt&logoColor=white)](https://www.qt.io/)
[![Engine](https://img.shields.io/badge/Video-FFmpeg%20Demuxer-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![AI Transcription](https://img.shields.io/badge/ASR-faster--whisper%20%2B%20Gemini%202.5%20Flash-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://ai.google.dev/)

</div>

---

## 🚀 Overview

**StreamClipper Pro** is an autonomous video clipping workstation designed for content creators, stream editors, and social media managers. It monitors YouTube live streams or processes pre-recorded VODs and local video files, detects high-energy moments, transcribes speech with word-level accuracy, tracks speaker faces, formats videos into modern vertical/square ratios, and burns viral animated ASS karaoke subtitles.

---

## ✨ Key Features

### 🎬 Universal Ingestion & Processing
- **YouTube Live Stream Monitoring:** Buffers active YouTube live streams in real-time with sub-second segment detection.
- **VOD & Local Video Support:** Process local `.mp4`, `.mkv`, `.mov` files or download full YouTube videos with automatic resolution selection.
- **Zero-Stall Decoupled Architecture:** Live stream recording runs in a dedicated thread while transcription, title generation, and FFmpeg rendering run in an asynchronous background queue—ensuring live stream capture never drops frames or stalls.

### ⏱️ Sliding Window & Hype Capture
- **Sliding Window Mode:** Captures full 60-second (or 45s, 30s, 15s) clips triggered every 10 seconds (`0–60s`, `10–70s`, `20–80s`) via lossless FFmpeg concatenation (`-c copy`) in under 0.2 seconds.
- **Hype Spike Mode:** Combines live YouTube chat velocity (`pytchat`) and audio RMS peak telemetry to isolate climactic stream reactions.

### 👤 Smart Face Tracking
- **Automated Framing:** Automatically tracks the active speaker's face using OpenCV Haar / DNN models.
- **Cinematic Panning:** Smooth exponential moving average (EMA) center smoothing prevents jerky panning when framing horizontal landscape sources into 9:16 Shorts or 1:1 Square clips.

### 🎨 8 Viral Caption Presets (ASS Karaoke Engine)
Burned-in animated subtitles scaled perfectly across all resolutions and aspect ratios:
1. **Viral Glow Pop (Shorts Edit):** Modern signature lower-third style with bold white text, vibrant yellow active-word pop, and a soft outer glow.
2. **Hormozi Pop (Classic):** High-impact uppercase white text with bright yellow keyword highlights and thick drop shadow.
3. **MrBeast Viral (Green):** Extra-heavy text with energetic neon green (`#39FF14`) active-word scaling.
4. **Cyberpunk Neon:** Neon cyan text with hot pink (`#FF007F`) active-word pop and a futuristic tech vibe.
5. **Clean Minimalist:** Subtle slate background capsule, crisp typography, and refined emerald highlight.
6. **Midnight Electric:** Dark electric violet styling with electric cyan active-word highlights.
7. **Bold Red Punch:** Aggressive sports/gaming style with blazing crimson red keyword emphasis.
8. **Golden Creator:** Premium gold gradient outline with warm luxury creator aesthetic.

### 🎙️ Dual-Engine Transcription & AI Refinement
- **Vocal Audio Pre-Processing:** Applies high-pass, low-pass, and loudness normalization (`loudnorm`) before feeding speech to speech models.
- **faster-whisper Engine:** High-speed offline transcription with Silero VAD filtering to eliminate hallucinations.
- **Gemini 2.5 Flash Ultra AI Mode:** Optional Google Gemini AI transcript refinement to correct domain-specific vocabulary and punctuation while preserving Whisper's word-level timestamps.

### 🏷️ Auto Viral English Title & Hashtags
- Generates high-CTR English titles and viral hashtags on the fly for every produced clip.
- Displayed directly on each clip card with one-click clipboard copying.

### 🖥️ Modern Creative Studio GUI
- Sleek obsidian workstation interface (`#0D0F12`) with Linear-inspired typography and controls.
- Interactive Gallery with `▶ Play`, `📋 Copy Title`, `📁 Open Folder`, and `🗑 Delete` actions.
- Customizable Output Directory selection persisting across app restarts.
- Custom multi-resolution Windows app icon embedded in the title bar, taskbar, and notification tray.

---

## 📐 Aspect Ratios & Quality Presets

| Aspect Ratio | Resolution | Best For |
|--------------|------------|----------|
| **9:16** | 1080 × 1920 | YouTube Shorts, TikTok, Instagram Reels |
| **16:9** | 1920 × 1080 | Standard YouTube, Twitch Highlights |
| **1:1** | 1080 × 1080 | Instagram Posts, Twitter/X Feeds |
| **4:5** | 1080 × 1350 | Instagram & Facebook In-Feed Video |

| Quality Preset | CRF / Bitrate | Description |
|----------------|---------------|-------------|
| **1080p Ultra** | CRF 18, 320k Audio | Maximum fidelity master export |
| **1080p Standard** | CRF 21, 192k Audio | Balanced web export |
| **720p Fast** | CRF 23, 128k Audio | Rapid preview rendering |

---

## 🛠️ Architecture

```
┌────────────────────────────────────────────────────────┐
│                    StreamClipper Pro                   │
└────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────────┐
        ▼                                     ▼
┌──────────────────┐                  ┌──────────────────┐
│  YouTube Live    │                  │  VOD / Local     │
│  stream_ingestor │                  │  vod_processor   │
└──────────────────┘                  └──────────────────┘
        │                                     │
        ▼                                     ▼
┌────────────────────────────────────────────────────────┐
│  Sliding Window Concatenation (ffmpeg demuxer -c copy) │
└────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│        Decoupled Asynchronous Render Worker            │
├────────────────────────────────────────────────────────┤
│  1. Audio Vocal Clarity Pre-Processing                 │
│  2. faster-whisper ASR (+ Optional Gemini 2.5 Flash)   │
│  3. Smart Face Tracking (OpenCV Center Crop)           │
│  4. Animated ASS Karaoke Subtitle Generation           │
│  5. Title & Hashtags Generator                         │
│  6. FFmpeg Hardware / Software Encode                  │
└────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│   Generated Clips Gallery (Play, Copy, Folder, Delete) │
└────────────────────────────────────────────────────────┘
```

---

## 📦 Installation & Setup

### Prerequisites
1. **Python 3.11+** installed on your system.
2. **FFmpeg & FFprobe:** Must be available on system PATH (or installed in standard tool locations).

### Clone & Install Dependencies
```bash
git clone https://github.com/Devan920/StreamClipper-Pro.git
cd StreamClipper-Pro
pip install -r requirements.txt
```

### Run Application
```bash
python main_app.py
```

---

## 🔨 Building Standalone Executable

To compile a standalone Windows executable (`StreamClipperPro.exe`) with PyInstaller:

```cmd
build_software.bat
```

The compiled standalone application will be located at:
```
dist\StreamClipperPro\StreamClipperPro.exe
```

### Windows Installer (Inno Setup)
An Inno Setup configuration script is included (`installer.iss`). To build a setup installer:
1. Install [Inno Setup 6+](https://jrsoftware.org/isdl.php).
2. Open `installer.iss` and click **Compile**, or run:
   ```cmd
   iscc installer.iss
   ```
3. The setup executable will be generated in `dist_installer\StreamClipperPro_Setup_v1.0.exe`.

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
