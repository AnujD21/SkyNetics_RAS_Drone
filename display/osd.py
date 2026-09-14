"""
OSD Overlay — display/osd.py

Renders a minimal, clean flight-style OSD with Montserrat font via PIL.
"""

import os
import cv2
import numpy as np
import time
from typing import List, Optional
from PIL import Image, ImageDraw, ImageFont
from ml.detection import Detection

OSD_WHITE   = (255, 255, 255)   # pure white for maximum contrast
OSD_GREEN   = (60,  220, 80)
OSD_RED     = (210, 30,  30)
OSD_CYAN    = (40,  210, 200)
OSD_ORANGE  = (255, 140, 0)
OSD_GRAY    = (140, 140, 140)

# ── Adaptive text contrast ──────────────────────────────────────────
# This is an avalanche/snow rescue drone — a large fraction of flight time
# is spent over bright white snow or sky, where plain white text with a
# thin black outline can wash out. We sample the local background
# luminance under each text element (from a cheap downsampled grayscale
# copy of the frame, computed once per render) and pick a fill/outline
# pair that stays legible against it, while keeping each element's hue —
# darkened orange still reads as "orange", not just "some dark color".
# Per-element EMA smoothing (keyed by a stable id per on-screen element)
# stops the style flickering every frame as the camera pans across an
# edge between bright snow and dark rock.
_LUMA_GRID_W, _LUMA_GRID_H = 64, 36     # coarse — only need "bright or dark here"
_LUMA_BRIGHT_THRESH = 150.0             # 0-255 grayscale; snow/sky read reliably above this
_LUMA_EMA_ALPHA     = 0.15              # smoothing factor for the per-element luminance sample

# ── Font setup (Montserrat via Pillow) ──────────────────────────────
_FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "Montserrat.ttf")
_FONT_PATH = os.path.normpath(_FONT_PATH)

def _load(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)

# Pre-load sizes used across the OSD (pt sizes tuned for 1280×720)
_F_SM   = _load(14)   # small labels (km/h, etc.)
_F_MED  = _load(17)   # standard telemetry
_F_LG   = _load(20)   # armed/mode status
_F_XL   = _load(22)   # alert banner
_F_TOP  = _load(16)   # top-bar FPS / RSSI

# Start time for fly timer
START_TIME = time.time()


# Reused for every _text_w() call instead of allocating a new dummy
# Image+Draw each time — textbbox() only queries font glyph metrics, it
# never touches the image's pixels, so one shared 1x1 context is safe to
# reuse across all text/font combinations. With adaptive contrast now
# calling _text_w() for every OSD element (~20/frame) to size the
# luminance-sample region, this removes ~20 PIL object allocations/frame.
_DUMMY_IMG  = Image.new("RGB", (1, 1))
_DUMMY_DRAW = ImageDraw.Draw(_DUMMY_IMG)


def _text_w(text: str, font: ImageFont.FreeTypeFont) -> int:
    """Return the pixel width of a string rendered with the given font."""
    bbox = _DUMMY_DRAW.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _line(img, p1, p2, color=OSD_WHITE, t=1):
    cv2.line(img, p1, p2, color, t, cv2.LINE_AA)


class OSDRenderer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.W   = cfg.display_width
        self.H   = cfg.display_height
        self._luma_ema: dict = {}   # per-element id -> smoothed background luminance

    def _bg_luma(self, gray_small, key, x, y, tw, th, sw, sh) -> float:
        """Sample the mean background luminance under a text region from the
        pre-downsampled grayscale frame, EMA-smoothed per element id."""
        gh, gw = gray_small.shape[:2]
        gx1 = max(0, min(gw - 1, int(x * gw / sw)))
        gy1 = max(0, min(gh - 1, int(y * gh / sh)))
        gx2 = max(gx1 + 1, min(gw, int((x + max(tw, 1)) * gw / sw)))
        gy2 = max(gy1 + 1, min(gh, int((y + max(th, 1)) * gh / sh)))
        raw = float(gray_small[gy1:gy2, gx1:gx2].mean())
        prev = self._luma_ema.get(key, raw)
        smoothed = (1 - _LUMA_EMA_ALPHA) * prev + _LUMA_EMA_ALPHA * raw
        self._luma_ema[key] = smoothed
        # Per-track keys (human_<id>) accumulate over a long mission as
        # track ids cycle — bound the dict rather than leak indefinitely.
        if len(self._luma_ema) > 128:
            self._luma_ema.clear()
        return smoothed

    @staticmethod
    def _style_for(color, luma: float):
        """Fill/outline pair that keeps `color`'s hue but stays legible
        whether the video under the text is bright (snow/sky) or dark."""
        if luma >= _LUMA_BRIGHT_THRESH:
            fill   = tuple(max(0, int(c * 0.35)) for c in color)
            stroke = (255, 255, 255)
        else:
            fill   = color
            stroke = (0, 0, 0)
        return fill, stroke

    def render(
        self,
        frame:    np.ndarray,
        dets:     List[Detection],
        num_humans:      int    = 0,
        total_unique:    int    = 0,
        radar_present:   bool   = False,
        radar_state:     int    = 0,
        radar_dist_cm:   int    = 0,
        radar_strength:  int    = 0,
        fps:             float  = 0.0,
        frame_id:        int    = 0,
        recording:       bool   = False,
        sensor_ok:       Optional[dict] = None,
        yolo_backend:    str    = "none",
        total_ms:        float  = 0.0,
        fc_telemetry:    Optional[dict] = None,
        anomaly_triggered: bool = False,
        inference_fps:   float  = 0.0,
        video_fps:       float  = 0.0,
    ) -> np.ndarray:
        out = frame.copy()
        H, W = out.shape[:2]
        cx, cy = W // 2, H // 2

        # Cheap downsampled grayscale of the untouched video, used to sample
        # local background brightness for adaptive text contrast below.
        # Computed once per frame on a tiny 64x36 grid — negligible cost.
        gray_small = cv2.resize(
            cv2.cvtColor(out, cv2.COLOR_BGR2GRAY),
            (_LUMA_GRID_W, _LUMA_GRID_H), interpolation=cv2.INTER_AREA,
        )

        fc = fc_telemetry or {}

        # MAVLink Variables
        lat       = fc.get("lat", 0.0)
        lon       = fc.get("lon", 0.0)
        sats      = fc.get("sats", 0)
        alt       = fc.get("alt_m", 0.0)
        speed     = fc.get("speed_kmh", 0.0)
        volts     = fc.get("battery_v", 0.0)
        armed     = fc.get("armed", False)
        pitch     = fc.get("pitch", 0.0)
        roll      = fc.get("roll", 0.0)
        hdg       = fc.get("heading", 0.0)
        mode      = fc.get("mode", "UNKNOWN")
        rssi      = fc.get("rssi", 0)
        throttle  = fc.get("throttle", 0)
        batt_rem  = fc.get("batt_rem", 0)
        dist_home = fc.get("dist_home", 0.0)

        # ── Phase 1: cv2-only drawing (crosshair + bbox corner brackets) ──
        # Plain cv2 calls straight on the numpy array — cheap, no PIL needed.
        _line(out, (cx - 15, cy), (cx - 5, cy), OSD_WHITE, 2)
        _line(out, (cx + 5,  cy), (cx + 15, cy), OSD_WHITE, 2)
        _line(out, (cx, cy - 15), (cx, cy - 5),  OSD_WHITE, 2)
        _line(out, (cx, cy + 5),  (cx, cy + 15), OSD_WHITE, 2)
        cv2.circle(out, (cx, cy), 3, OSD_WHITE, 1, cv2.LINE_AA)

        for det in dets:
            x1 = int(det.x1); y1 = int(det.y1)
            x2 = int(det.x2); y2 = int(det.y2)
            box_col = OSD_ORANGE
            L = 20
            _line(out, (x1, y1),   (x1+L, y1),   box_col, 1)
            _line(out, (x1, y1),   (x1, y1+L),   box_col, 1)
            _line(out, (x2, y1),   (x2-L, y1),   box_col, 1)
            _line(out, (x2, y1),   (x2, y1+L),   box_col, 1)
            _line(out, (x1, y2),   (x1+L, y2),   box_col, 1)
            _line(out, (x1, y2),   (x1, y2-L),   box_col, 1)
            _line(out, (x2, y2),   (x2-L, y2),   box_col, 1)
            _line(out, (x2, y2),   (x2, y2-L),   box_col, 1)

        # ── Phase 2: every text element in ONE PIL round-trip ──────────
        # Previously each piece of text did its own full-frame numpy<->PIL
        # conversion (~20 conversions/frame across all the telemetry fields
        # plus one per tracked human) — that repeated 1280x720 color-space
        # + array copy was the actual fps bottleneck, not the ML overlay
        # itself. Converting once, drawing everything on that one PIL
        # image, then converting back once removes ~19/20 of that cost.
        pil  = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)

        def txt(text, pos, font=_F_MED, color=OSD_WHITE, key=None):
            x, y = pos
            luma = self._bg_luma(
                gray_small, key or text, x, y,
                _text_w(text, font), font.size, W, H,
            )
            fill, stroke = self._style_for(color, luma)
            draw.text(pos, text, font=font, fill=fill,
                      stroke_width=2, stroke_fill=stroke)

        # 0. Top Center: FPS Metrics
        ifps_col = OSD_GREEN  if inference_fps >= 3.0  else OSD_ORANGE
        vfps_col = OSD_GREEN  if video_fps     >= 15.0 else OSD_ORANGE
        ifps_txt = f"INF {inference_fps:4.1f} FPS"
        vfps_txt = f"VID {video_fps:4.1f} FPS"
        gap      = 20
        ifps_w   = _text_w(ifps_txt, _F_TOP)
        total_w  = ifps_w + gap + _text_w(vfps_txt, _F_TOP)
        fx       = cx - total_w // 2
        txt(ifps_txt, (fx, 10),              _F_TOP, ifps_col, key="ifps")
        txt(vfps_txt, (fx + ifps_w + gap, 10), _F_TOP, vfps_col, key="vfps")

        # 1. Top Left: LAT / LON
        txt(f"LAT  {lat: .7f}", (30, 30), _F_MED, OSD_WHITE, key="lat")
        txt(f"LON  {lon: .7f}", (30, 55), _F_MED, OSD_WHITE, key="lon")

        # 2. Top Right: RSSI & Timer
        fly_sec = int(time.time() - START_TIME)
        fly_mn  = fly_sec // 60
        fly_s   = fly_sec % 60
        rssi_txt  = f"RSSI {rssi}%"
        timer_txt = f"{fly_mn:02d}:{fly_s:02d}"
        txt(rssi_txt,  (W - _text_w(rssi_txt,  _F_TOP) - 20, 10), _F_TOP, OSD_WHITE, key="rssi")
        txt(timer_txt, (W - _text_w(timer_txt, _F_TOP) - 20, 32), _F_TOP, OSD_WHITE, key="timer")
        if recording:
            txt("● REC", (W - 90, 54), _F_SM, OSD_RED, key="rec")

        # 3. Bottom left panel (Power) — BAT/AMP/MAH rendered over the
        #    thermal PiP in rescue_display.py
        by = H - 160

        # 4. Center-Left: Disarm Status
        cx_left = W // 2 - 250
        cell_v  = (volts / 4.0) if volts > 5.0 else volts
        txt(f"CELL  {cell_v:.2f} v", (cx_left, by),      _F_MED, OSD_WHITE, key="cell")
        status_txt = "ARMED" if armed else "DISARMED"
        txt(status_txt,               (cx_left, by + 30), _F_LG,  OSD_WHITE, key="status")
        txt(f"MODE  {mode}",           (cx_left, by + 58), _F_MED, OSD_WHITE, key="mode")

        # 5. Center-right Telemetry
        cx_right = W // 2 + 150
        ry = H // 2 - 50
        txt(f"HOME  {dist_home:.0f} M", (cx_right, ry),       _F_MED, OSD_WHITE, key="home")
        txt(f"THR   {throttle}%",        (cx_right, ry + 30),  _F_MED, OSD_WHITE, key="thr")
        txt(f"SAT   {sats}",             (cx_right, ry + 60),  _F_MED, OSD_WHITE, key="sat")
        txt(f"ALT   {alt:.1f} M",        (cx_right, ry + 90),  _F_MED, OSD_WHITE, key="alt")

        # 6. Bottom Center: Alert Banner
        if num_humans > 0:
            alert_text = "HUMAN DETECTED!"
            col = OSD_ORANGE
        elif anomaly_triggered:
            alert_text = "POSSIBLE HUMAN (THERMAL)"
            col = OSD_CYAN
        else:
            alert_text = "RESCUE DRONE OSD"
            col = OSD_WHITE

        atw = _text_w(alert_text, _F_XL)
        txt(alert_text, (cx - atw // 2, H - 36), _F_XL, col, key="alert")

        # 7. Speed readout (crosshair itself drawn in phase 1)
        txt(f"{speed:.0f}", (cx + 22, cy - 8),  _F_MED, OSD_WHITE, key="speed")
        txt("km/h",         (cx + 22, cy + 14), _F_SM,  OSD_GRAY, key="kmh")

        # 8. Attitude Telemetry
        txt(f"HDG  {hdg:03.0f}",           (cx - 45, cy + 70),  _F_MED, OSD_WHITE, key="hdg")
        txt(f"P {pitch:+.0f}  R {roll:+.0f}", (cx - 45, cy + 96), _F_SM,  OSD_WHITE, key="pr")

        # 9. Bounding box labels (corner brackets drawn in phase 1). Keyed by
        # track_id (stable per tracked person) rather than the label text
        # itself, so each person's smoothed luminance follows them around.
        for det in dets:
            x1 = int(det.x1); y1 = int(det.y1); x2 = int(det.x2)
            cx_b  = (x1 + x2) // 2
            label = "HUMAN"
            lw    = _text_w(label, _F_SM)
            txt(label, (cx_b - lw // 2, y1 - 20), _F_SM, OSD_ORANGE, key=f"human_{det.track_id}")

        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
