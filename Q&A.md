# Rescue Drone v3 — Interview Q&A

A reference sheet for explaining this project to an interviewer, teammate, or
anyone else asking "so what does it actually do?" Written to be read out loud,
not just skimmed.

> **Note on numbers:** Every technical figure below (resolutions, thresholds,
> fps, algorithms) is pulled directly from the code in this repo. Figures
> that depend on your *physical build* — VTX transmit power, antenna gain,
> battery/motor choice, actual component prices you paid — are **not** in the
> software repo, so they're marked clearly below as estimates/placeholders.
> Fill in your real numbers before an interview if you have them.

---

## 1. Elevator Pitch

**"What is this project?"**

Rescue Drone v3 is a fully autonomous, offline human-detection system for
avalanche and landslide rescue. It runs entirely on a Raspberry Pi 4 mounted
on a drone, fuses three different sensors (thermal camera, RGB camera, and
mmWave radar) to spot buried or hard-to-see survivors, tracks them frame to
frame, and streams a live augmented-reality overlay (bounding boxes,
telemetry, radar scope) straight to the pilot's FPV goggles over analog
video — no internet, no cloud, no ground station required.

**"Why does this matter?"** In an avalanche, survival odds drop sharply
after the first 15–30 minutes. A drone that can scan a debris field from the
air, in conditions too dangerous for rescuers to search on foot, buys back
exactly the time that matters most.

---

## 2. Hardware / Items Used

| Component | Model | Role |
|---|---|---|
| Compute | Raspberry Pi 4 | Runs the entire stack — sensor drivers, ML inference, fusion, tracking, OSD rendering. Everything is offline, no cloud dependency. |
| Thermal camera | MLX90640 | 32×24 px thermal array, 55° FOV, I²C, ~4 Hz refresh. Primary detection sensor — finds body heat through snow/light debris. |
| RGB camera | Raspberry Pi Camera Module 3 (Sony IMX708) | CSI/libcamera, native 2304×1296 sensor. Feeds the YOLO human detector. |
| Radar | HLK-LD2450 (24 GHz FMCW) | UART, tracks up to 3 targets in 2D (X/Y position + velocity). Works through fog, smoke, and whiteout where cameras can't see, and can pick up the micro-movement of someone breathing. |
| Flight controller | BotWing F722 | Sends MAVLink telemetry (battery, GPS, attitude, arm state) to the Pi over UART — **read-only**, the Pi never sends flight commands back. |
| Video output | Analog VTX via the Pi's composite (3.5 mm TRRS) output | Streams the OSD overlay live to FPV goggles — no HDMI capture card, no digital latency. |

**"Why these specific sensors?"** Each one covers the others' blind spot:
- RGB/YOLO needs line-of-sight and light — useless at night or under snow.
- Thermal sees body heat through light debris, works in the dark, but is low-resolution (32×24 px) and gives false positives from anything warm (rocks in sun, engines).
- Radar doesn't need light *or* line-of-sight, and can detect breathing-level movement — but it can't image anything, so it's used as a **confirmation layer**, not a standalone detector.

Fusing all three means: if only one sensor fires, it's a *possible* detection;
if two or three agree, confidence goes up sharply — see the fusion weights in
section 6.

---

## 3. Range

Software-configurable/spec'd ranges (from the code):

| Sensor | Range | Source |
|---|---|---|
| LD2450 radar | Up to 8 m detection distance, 60° azimuth (hardware spec); software radar-scope UI is configured for a 6 m max range by default (`config.py: ld2410_max_range_cm = 600`, tune per environment) | `sensors/ld2450_radar.py`, `config.py` |
| Thermal (MLX90640) | 55° field of view; effective human-detection distance is a few meters and depends heavily on ambient vs. body temperature contrast (snow background helps a lot here) | `sensors/thermal_camera.py` |
| RGB/YOLO | Depends entirely on subject size in frame and lighting — no hard-coded limit, but far subjects shrink below the model's practical detection size | `ml/models.py` |

**Not in this repo — hardware/build-dependent, fill in your real numbers:**

- **Video transmission range (VTX → goggles):** determined by your VTX's
  transmit power and antenna, not the software. Typical analog FPV setups
  (200 mW–600 mW) reach roughly 1–5 km line-of-sight — *treat this as general
  FPV knowledge, not a tested figure for this build.*
- **Flight range / endurance:** determined by your airframe, motors, props,
  and battery capacity — none of which live in this repo.

If asked in an interview: *"The detection sensors have documented ranges
(radar ~6–8 m, thermal a few meters depending on conditions); the drone's
overall flight range and video range depend on the airframe and VTX we
paired it with, which is a hardware choice separate from the software
stack."*

---

## 4. Estimated Cost

**Not tracked in this repo** — no receipts or BOM file exist in the codebase,
so treat the table below as **rough, indicative retail pricing** for the
electronics this software actually talks to, useful as an interview ballpark,
not a verified figure.

| Component | Approx. price range (USD) |
|---|---|
| Raspberry Pi 4 (4GB) | $45 – $75 |
| MLX90640 thermal camera | $50 – $65 |
| Pi Camera Module 3 | $25 – $35 |
| HLK-LD2450 radar | $8 – $15 |
| BotWing F722 flight controller | $20 – $40 |
| Analog VTX + antenna | $20 – $50 |
| **Electronics subtotal** | **~$170 – $280** |

**Not included above** (airframe, motors, ESCs, propellers, battery, FPV
goggles/receiver) — these are standard FPV-drone build costs, typically
another **$300–$800+** depending on frame class, and are independent of this
software project.

*Good interview framing:* "The compute + sensor stack is a few hundred
dollars in commodity parts — the value isn't in expensive hardware, it's in
fusing cheap sensors intelligently so the whole system is affordable enough
to actually deploy at scale for rescue teams."

---

## 5. Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.9+ |
| OS | Raspberry Pi OS Lite (headless-capable) |
| Computer vision | OpenCV (`opencv-python`) |
| ML inference | ONNX Runtime (`onnxruntime`) — with an OpenCV DNN (`cv2.dnn`) fallback if ONNX Runtime fails to load |
| Numerics | NumPy — nearly every hot-path array op (masks, IoU, Kalman math) is vectorized |
| Image/text rendering | Pillow (PIL) — used specifically for the OSD's Montserrat font rendering, since OpenCV's built-in fonts look poor |
| Tracking math | SciPy (`linear_sum_assignment` for optimal detection↔track matching), `filterpy`-style Kalman filtering (hand-rolled, see §6) |
| Camera driver | Picamera2 / libcamera (NOT `cv2.VideoCapture` — needed for proper RPi Camera Module 3 support) |
| Radar/FC comms | `pyserial` (UART to the LD2450), `pymavlink` (MAVLink telemetry from the F722) |
| Thermal driver | `adafruit-circuitpython-mlx90640` + `adafruit-blinka` (I²C) |
| Process management | `systemd` (`rescue_drone.service`) — auto-starts the whole system on boot, restarts on crash |
| Training (off-device only) | `ultralytics` + `torch` — used to train/export the YOLO model on a laptop/GPU; never runs on the Pi itself |

---

## 6. Algorithms Used — and Exactly Where

This is usually the meat of a technical interview. Know these cold.

| Algorithm | Where | What it does |
|---|---|---|
| **YOLOv8 (single-class "person") — ONNX inference** | `ml/models.py: YOLODetector` | RGB human detection. Runs on a 320×320 letterboxed input; confidence threshold deliberately dropped to 0.10 to favor recall (catching partial/occluded people) over precision, since sensor fusion downstream cleans up false positives. |
| **Letterbox resize** | `YOLODetector.detect()` | Preprocessing: pads the camera frame to a square 320×320 without distorting aspect ratio, runs inference, then maps boxes back through the same transform to the display's native resolution — this is *why* the boxes line up correctly with the live video regardless of camera/display resolution mismatch. |
| **Adaptive background subtraction (EMA-based)** | `ml/models.py: AnomalyDetector`, `ml/thermal_isolation.py: ThermalIsolator` | Thermal detection without any trained model: continuously tracks the *cold* background temperature with an exponential moving average, then flags pixels significantly hotter than that adaptive baseline **and** within a human body-temperature range (12–45 °C window, tuned to tolerate clothing/indoor testing) as candidate human blobs. |
| **Non-Maximum Suppression (vectorized, NumPy)** | `ml/models.py: _nms()` | Removes duplicate/overlapping boxes — used both inside YOLO postprocessing and again after sensor fusion merges thermal + RGB detections. |
| **Sensor Fusion (weighted confidence + IoU merge)** | `ml/models.py: SensorFusion` | Combines YOLO (RGB), thermal-anomaly, and radar-presence detections. Weights: **thermal 0.65** (primary sensor), **radar 0.25** (confidence booster), **anomaly 0.10**. A radar hit on a target that thermal/RGB already flagged boosts confidence; radar alone (no visual/thermal blob) just logs a "possible deep burial" note rather than drawing a box, since radar can't localize precisely enough on its own. |
| **Kalman Filter (linear, 7-state constant-velocity model)** | `ml/models.py: _Track` | State vector `[cx, cy, scale, aspect_ratio, vx, vy, vscale]`. Predicts each tracked person's position every frame and corrects it against new detections — this is what lets a person's box keep moving smoothly even on frames where the ML pipeline didn't run. |
| **SORT-style multi-object tracking** (Hungarian algorithm via `scipy.optimize.linear_sum_assignment`, greedy IoU fallback if SciPy is unavailable) | `ml/models.py: HumanTracker`, `_assign()` | Assigns detections to existing tracks frame-to-frame by IoU overlap, gives each person a persistent ID, and keeps a short "graveyard" of recently-lost tracks so someone briefly walking off-frame gets their *same* ID back instead of being double-counted. |
| **Linear extrapolation (dead-reckoning)** | `display/rescue_display.py` | The ML pipeline only produces new boxes ~4×/second (thermal-sensor-gated), but the video renders up to 30 fps. This projects each box forward using its own recent velocity between real detections, so it glides smoothly instead of visibly jumping every ~250 ms. |
| **Haversine formula** | `sensors/flight_controller.py` | Great-circle distance calculation from GPS lat/lon — computes live "distance to home" for the OSD from MAVLink `HOME_POSITION`/`GLOBAL_POSITION_INT`. |
| **AGC (Auto Gain Control) / dynamic range normalization** | `sensors/thermal_camera.py` | Thermal frames are auto-normalized every frame using a percentile-based min/max (with its own EMA smoothing) so the color palette stays well-contrasted whether the scene is a warm room or freezing snow. |
| **Adaptive local-luminance text contrast** | `display/osd.py` | Samples background brightness under each OSD text element (from a cheap downsampled grayscale frame) and switches between bright-on-black and dark-on-white styling per element, so telemetry text stays readable over both bright snow and dark rock. |

---

## 7. Tools Used

- **Git / GitHub** — version control.
- **systemd** — boot-time auto-start and crash-restart (`rescue_drone.service`); logs viewable live via `journalctl -u rescue_drone -f`.
- **`i2cdetect`, `libcamera-hello`** — hardware bring-up/verification tools used to confirm the thermal camera (I²C address `0x33`) and Pi Camera are actually detected before running the full stack.
- **ONNX export tooling** (`ultralytics` on a separate laptop/GPU) — trains YOLOv8n on a custom person-detection dataset and exports to `.onnx` for on-device inference; this training step never touches the Pi.
- **`cpufrequtils`** — pins the Pi 4's CPU to performance governor so it doesn't throttle under sustained ML load.
- **OpenCV's `cv2.dnn`** — a deliberate fallback ML backend if ONNX Runtime fails to load (e.g. an illegal-instruction crash on some ARM builds) — the system tries to stay useful with anomaly-only detection rather than crashing outright.

---

## 8. Working Structure / System Architecture

**"Walk me through how data flows through the system."**

```
┌─────────────┐   ┌──────────────┐   ┌─────────────┐   ┌───────────────┐
│  RGB Camera │   │ Thermal Cam  │   │    Radar     │   │ Flight Ctrl   │
│  (own thread)│   │ (rate-limited│   │ (own thread) │   │ (own thread,  │
│  ~30fps      │   │  4Hz reads)  │   │  UART reads) │   │  read-only)   │
└──────┬──────┘   └──────┬───────┘   └──────┬───────┘   └──────┬────────┘
       │                 │                   │                  │
       │           ┌─────▼───────────────────▼──────────────────▼─────┐
       │           │         DetectionPipeline (inference thread)      │
       │           │  thermal-gated ~4fps:                             │
       │           │  1. Anomaly Detector (thermal)                    │
       │           │  2. YOLO (RGB)                                    │
       │           │  3. Sensor Fusion (weighted merge + NMS)          │
       │           │  4. Kalman/SORT Tracker (IDs, smoothing)          │
       │           └─────────────────────┬──────────────────────────┘
       │                                 │ latest detections
       └───────────────┐                 │ (shared, lock-protected)
                        ▼                 ▼
              ┌─────────────────────────────────────┐
              │   Display thread (up to 30fps)       │
              │  - always grabs the FRESHEST RGB     │
              │    frame directly from the camera    │
              │  - overlays latest tracked boxes     │
              │    (extrapolated for smoothness)      │
              │  - draws OSD (telemetry/radar/PiP)    │
              └──────────────────┬────────────────────┘
                                 ▼
                    Composite video → analog VTX → FPV goggles
```

**Key architectural decision — three independent threads:**
1. **Camera capture thread** — continuously grabs frames into a single "latest frame" slot (not a queue; old frames are simply overwritten, never buffered up).
2. **Inference thread** — runs the full detection pipeline (thermal + YOLO + fusion + tracking), gated to the thermal sensor's real ~4 Hz refresh rate.
3. **Display thread** — renders at up to 30 fps, always using the *freshest* camera frame available, overlaying whatever the inference thread last published.

**Why split like this?** YOLO inference and thermal reads are relatively
slow (tens–hundreds of ms). If the display waited on them directly, video
would visibly stutter down to inference speed. By decoupling them, the pilot
always sees smooth live video — bounding boxes just update at whatever rate
the ML pipeline can sustain, which is a perfectly acceptable trade-off for a
rescue overlay.

**Fail-safe behavior:** thermal sensor failure is the only one that's fatal
(it's the primary sensor); RGB camera, radar, and flight-controller failures
all degrade gracefully — the system keeps running with reduced detection
confidence/telemetry rather than crashing.

---

## 9. Other Questions an Interviewer Might Ask

**Q: Why fuse three sensors instead of just running a good YOLO model?**
A: YOLO alone fails exactly when it matters most — no light, buried under
snow, or someone facing away/curled up. Thermal sees heat regardless of
light. Radar sees through snow/fog entirely and can catch someone who isn't
visually or thermally distinct (e.g. deeply buried, where only faint
breathing motion is detectable). Fusion means the system degrades gracefully
instead of having one single point of failure.

**Q: How do you avoid false positives (e.g., warm rocks, animals)?**
A: Multiple layers: a human-shape filter (aspect ratio + area bounds) on
thermal blobs, a temperature-range filter, an adaptive (not fixed) background
threshold so it self-calibrates to the scene, sensor-fusion weighting (a
thermal-only hit is treated as lower confidence than one confirmed by
RGB/radar too), and finally a tracker that only "confirms" a detection after
it's been consistently matched across frames — a one-off flicker doesn't
trigger an alert.

**Q: Why run everything on a Raspberry Pi 4 instead of a bigger onboard computer or the cloud?**
A: This is explicitly an *offline* rescue tool — no internet in an avalanche
field. A Pi 4 is cheap, low-power (important for flight time), and just
capable enough if you're careful: small model input size (320×320), a
single-class ONNX model, vectorized NumPy everywhere, and deliberately
leaving CPU cores free for the camera/display threads rather than letting
ML inference hog every core.

**Q: What was the hardest technical problem you solved?**
A few real, concrete ones from this project:
- **CPU starvation stutter:** ONNX Runtime was configured to use all 4 of
  the Pi's cores per YOLO call, which starved the camera/display threads of
  CPU during every ~200 ms inference — even though they were architecturally
  independent threads, they still need a core to actually run on. Fixed by
  capping ONNX's thread pool, leaving headroom for everything else.
- **OSD rendering bottleneck:** the on-screen telemetry was doing ~20
  separate full-frame color-space round-trips through PIL per rendered
  frame (one per text element) — batched into a single conversion, which
  gave roughly a 7× speedup on that code path alone.
- **IMX708 camera PDAF bug:** requesting a low-resolution camera stream
  triggers a libcamera driver bug (`PDAF data in unsupported format`) that
  silently drops every frame. Fixed by forcing the sensor's full-frame
  readout mode and letting the ISP hardware-scale down for free.
- **Thermal sensor thread-safety:** the MLX90640's I²C library isn't
  thread-safe, so it's deliberately read synchronously inside the inference
  loop (not a background thread) with explicit rate-limiting to match its
  real ~4 Hz refresh rate.

**Q: How did you test this without a physical drone in the field?**
A: A `--demo` mode that fabricates synthetic sensor data for every sensor
(a fake human-temperature thermal blob, random RGB noise, a fake radar
target) so the entire pipeline — detection, fusion, tracking, OSD — can be
exercised end-to-end on a laptop with zero hardware attached.

**Q: The video is smooth but boxes lag/aren't perfectly synced — why?**
A: By design. The detection pipeline runs at the sensor's real rate
(~4 fps, thermal-gated); the video renders independently at up to 30 fps.
Bounding-box position is *extrapolated* between real detections using each
track's recent velocity so it looks continuous, but it's fundamentally an
estimate between real ~250 ms-apart updates, not a new detection every
frame.

**Q: Why analog video (VTX) instead of a digital feed?**
A: Zero added latency and no capture-card/decoding pipeline — the Pi's
composite output goes straight into a standard analog VTX, which is what
most FPV goggles already expect. Digital would add real-time video encoding
overhead that a Pi 4 doing ML work can't comfortably spare.

**Q: What would you improve if you had more time?**
Reasonable, honest answers: hardware-accelerated video encoding for
recording (currently software `mp4v`, CPU-heavy on ARM); a proper labeled
thermal+RGB dataset to train a fusion-aware model instead of hand-tuned
heuristic thresholds; GPS-based search-pattern autopilot integration instead
of manual piloting; a multi-drone coordination layer for larger search
areas.
