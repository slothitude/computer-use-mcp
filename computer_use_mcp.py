"""Computer Use MCP Server — PyAutoGUI + OpenCV + Ollama/NVIDIA vision.

Controls the local desktop (mouse, keyboard, screenshots, screen understanding,
window management, pixel color, OCR, clipboard, accessibility tree, shell).
Runs as stdio MCP server via FastMCP.

Config via environment variables:
    VISION_BACKEND         - Vision provider: "ollama" (default) or "nvidia"
    COMPUTER_VISION_MODEL  - Ollama vision model (default: qwen2.5vl:3b)
    OLLAMA_BASE            - Ollama API base URL (default: http://localhost:11434)
    NVIDIA_VISION_URL      - NVIDIA NIM endpoint (default: https://integrate.api.nvidia.com/v1/chat/completions)
    NVIDIA_VISION_MODEL    - NVIDIA model (default: meta/llama-3.2-90b-vision-instruct)
    NVIDIA_API_KEY         - NVIDIA API bearer token
    VISION_TIMEOUT         - Vision API timeout seconds (default: 300)
    SCREEN_MAX_DIMENSION   - Max dimension for vision screenshots (default: 1280)
"""

import base64
import json
import os
import re
import subprocess
import time
import urllib.request
from collections import deque
from io import BytesIO
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("computer-use")

# ── Config ───────────────────────────────────────────────────────────────────────

OLLAMA_BASE = os.getenv("OLLAMA_BASE", "http://localhost:11434")
COMPUTER_VISION_MODEL = os.getenv("COMPUTER_VISION_MODEL", "qwen2.5vl:3b")
NVIDIA_VISION_URL = os.getenv("NVIDIA_VISION_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
NVIDIA_VISION_MODEL = os.getenv("NVIDIA_VISION_MODEL", "meta/llama-3.2-90b-vision-instruct")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "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")  # fallback; primary source is .mcp.json env
VISION_BACKEND = os.getenv("VISION_BACKEND", "nvidia")  # "ollama" or "nvidia"
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "300"))
SCREEN_MAX_DIMENSION = int(os.getenv("SCREEN_MAX_DIMENSION", "1280"))
DATA_DIR = Path(os.getenv("COMPUTER_USE_DATA_DIR", "data"))

# ── Startup Config Dump (to stderr, visible in MCP logs) ────────────────────────
import sys as _sys
_key_src = "env" if os.environ.get("NVIDIA_API_KEY") else "default"
_backend_src = "env" if os.environ.get("VISION_BACKEND") else "default"
print(f"[computer-use] VISION_BACKEND={VISION_BACKEND} (from {_backend_src})", file=_sys.stderr)
print(f"[computer-use] NVIDIA_API_KEY={'***set***' if NVIDIA_API_KEY else '(empty)'} (from {_key_src})", file=_sys.stderr)

MULTI_SCALE_STEPS = [0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25]

# HSV color ranges for element detection (H: 0-180, S: 0-255, V: 0-255)
# Red needs two ranges due to hue wrap-around.
_COLOR_HSV_RANGES = {
    "red": [(0, 70, 50), (10, 255, 255), (170, 70, 50), (180, 255, 255)],
    "green": [(35, 70, 50), (85, 255, 255)],
    "blue": [(100, 70, 50), (130, 255, 255)],
    "yellow": [(20, 70, 50), (35, 255, 255)],
    "orange": [(10, 70, 50), (20, 255, 255)],
    "purple": [(130, 70, 50), (160, 255, 255)],
    "white": [(0, 0, 200), (180, 50, 255)],
    "gray": [(0, 0, 80), (180, 50, 200)],
    "black": [(0, 0, 0), (180, 255, 80)],
}

# ── Action Trace ──────────────────────────────────────────────────────────────
# In-memory deque recording every tool call for crash diagnosis.

_action_trace = deque(maxlen=100)


def _trace(tool_name, result, elapsed=None):
    """Record a tool call in the action trace."""
    entry = {"tool": tool_name, "timestamp": time.time(), "elapsed": round(elapsed, 3) if elapsed else None}
    if isinstance(result, dict):
        entry.update({k: v for k, v in result.items() if k != "error"})
        if "error" in result:
            entry["error"] = result["error"]
    else:
        entry["result_preview"] = str(result)[:200]
    _action_trace.append(entry)


# ── RapidOCR Remote Service ───────────────────────────────────────────────────

OCR_SERVICE_URL = os.getenv("OCR_SERVICE_URL", "http://192.168.0.33:8100/ocr")


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _grab(bbox=None):
    """Screenshot using ImageGrab — supports multi-monitor via bbox."""
    from PIL import ImageGrab
    if bbox is None:
        img = ImageGrab.grab(all_screens=True)
    else:
        img = ImageGrab.grab(bbox=bbox)
    return img


def _grab_monitor(monitor=None):
    """Screenshot a specific monitor or all screens.
    monitor=0 or None -> all screens, 1+ -> specific monitor index."""
    monitors = _get_monitors_raw()
    if monitor is None or monitor == 0:
        return _grab()
    idx = monitor - 1
    if idx < 0 or idx >= len(monitors):
        return None
    m = monitors[idx]
    return _grab(bbox=(m["left"], m["top"], m["right"], m["bottom"]))


def _get_monitors_raw():
    """Return list of monitor dicts with left/top/right/bottom/primary."""
    import ctypes
    from ctypes import wintypes, POINTER
    import win32api
    import win32con

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    monitors = []
    def callback(hmonitor, hdc, rect, data):
        info = win32api.GetMonitorInfo(hmonitor)
        monitors.append({
            "left": info["Monitor"][0],
            "top": info["Monitor"][1],
            "right": info["Monitor"][2],
            "bottom": info["Monitor"][3],
            "primary": bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY),
        })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, POINTER(RECT), wintypes.LPARAM,
    )
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(callback), 0)
    return monitors


def _vision_call(img, question):
    """Send a PIL image to vision model, return text response.
    Routes to Ollama or NVIDIA NIM based on VISION_BACKEND."""
    if VISION_BACKEND == "nvidia" and NVIDIA_API_KEY:
        return _vision_call_nvidia(img, question)
    return _vision_call_ollama(img, question)


def _vision_call_ollama(img, question):
    """Send a PIL image to Ollama vision model, return text response."""
    if max(img.width, img.height) > SCREEN_MAX_DIMENSION:
        scale = SCREEN_MAX_DIMENSION / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = json.dumps({
        "model": COMPUTER_VISION_MODEL,
        "messages": [{"role": "user", "content": question, "images": [b64]}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data.get("message", {}).get("content", "No response from vision model.")
    except Exception as e:
        return f"Vision model error: {e}"


def _vision_call_nvidia(img, question):
    """Send a PIL image to NVIDIA NIM vision model, return text response."""
    if max(img.width, img.height) > SCREEN_MAX_DIMENSION:
        scale = SCREEN_MAX_DIMENSION / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = json.dumps({
        "model": NVIDIA_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0.5,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_VISION_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "No response from NVIDIA vision model.")
        return f"No response from NVIDIA vision model: {data}"
    except Exception as e:
        return f"NVIDIA vision model error: {e}"


def _find_window(title_or_handle):
    """Resolve a title substring or handle int to a window handle.
    Returns (hwnd, title) or (None, error_msg)."""
    import win32gui
    if isinstance(title_or_handle, int):
        title = win32gui.GetWindowText(title_or_handle)
        if title:
            return title_or_handle, title
        return None, f"Handle {title_or_handle} has no title."
    filter_lower = title_or_handle.lower()
    results = []
    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and filter_lower in title.lower():
                results.append((hwnd, title))
    win32gui.EnumWindows(callback, None)
    if not results:
        return None, f"No visible window matching '{title_or_handle}'."
    # Prefer exact match, then first match
    for hwnd, title in results:
        if title.lower() == filter_lower:
            return hwnd, title
    return results[0]


# ── Monitor ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_monitors() -> dict:
    """List all connected monitors with bounds and primary flag.
    Returns list of {index, left, top, right, bottom, width, height, primary}."""
    monitors = _get_monitors_raw()
    out = []
    for i, m in enumerate(monitors):
        out.append({
            "index": i + 1,
            "left": m["left"], "top": m["top"],
            "right": m["right"], "bottom": m["bottom"],
            "width": m["right"] - m["left"],
            "height": m["bottom"] - m["top"],
            "primary": m["primary"],
        })
    return {"monitors": out, "total_virtual": {
        "width": max(m["right"] for m in monitors) - min(m["left"] for m in monitors),
        "height": max(m["bottom"] for m in monitors) - min(m["top"] for m in monitors),
    }}


# ── Vision ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def computer_screenshot(monitor: int = 0, region: str = "") -> dict:
    """Take a screenshot. Use monitor=0 for all screens, 1+ for specific monitor.
    Optionally crop with region='x,y,w,h'.

    Returns path and dimensions."""
    img_dir = DATA_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = img_dir / f"screenshot_{ts}.png"

    img = _grab_monitor(monitor)
    if img is None:
        return {"error": f"Invalid monitor index {monitor}."}

    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                img = img.crop((parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3]))
        except ValueError:
            pass

    img.save(str(path))
    return {"path": str(path), "width": img.width, "height": img.height}


@mcp.tool()
def analyze_screen(question: str = "Describe what is on screen and any notable UI elements",
                   monitor: int = 0) -> str:
    """Take a screenshot and send it to a vision model for understanding.
    Use monitor=0 for all screens, 1+ for specific monitor.
    Returns a text description."""
    img = _grab_monitor(monitor)
    if img is None:
        return f"Error: Invalid monitor index {monitor}."
    return _vision_call(img, question)


# ── Template Matching ──────────────────────────────────────────────────────────

def _match_template_method(screen_cv, tpl_pil, threshold, multi_scale):
    """Multi-scale template matching using TM_CCOEFF_NORMED.
    Returns dict with found/best/confidence/scale/count/all_matches."""
    import cv2
    import numpy as np

    scales = [1.0] if not multi_scale else MULTI_SCALE_STEPS
    best_result = {"found": False, "count": 0}

    for scale in scales:
        if scale == 1.0:
            tpl = tpl_pil
        else:
            w = int(tpl_pil.shape[1] * scale)
            h = int(tpl_pil.shape[0] * scale)
            if w < 5 or h < 5:
                continue
            tpl = cv2.resize(tpl_pil, (w, h))

        th, tw = tpl.shape[:2]
        sh, sw = screen_cv.shape[:2]
        if tw > sw or th > sh:
            continue

        result = cv2.matchTemplate(screen_cv, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val >= threshold and max_val > best_result.get("confidence", 0):
            best_result = {
                "found": True,
                "best": {"x": int(max_loc[0]), "y": int(max_loc[1])},
                "confidence": round(float(max_val), 4),
                "template_size": {"width": tw, "height": th},
                "scale": scale,
            }

    if best_result["found"]:
        scale = best_result["scale"]
        if scale != 1.0:
            w = int(tpl_pil.shape[1] * scale)
            h = int(tpl_pil.shape[0] * scale)
            tpl_scaled = cv2.resize(tpl_pil, (w, h))
        else:
            tpl_scaled = tpl_pil
        result = cv2.matchTemplate(screen_cv, tpl_scaled, cv2.TM_CCOEFF_NORMED)
        locs = np.where(result >= threshold)
        points = list(zip(locs[1].tolist(), locs[0].tolist()))
        best_result["count"] = len(points)
        best_result["all_matches"] = [{"x": int(p[0]), "y": int(p[1])} for p in points]

    return best_result


def _match_feature_method(screen_cv, tpl_cv, threshold):
    """ORB feature matching with RANSAC homography.
    Returns dict with found/best/confidence or found=False."""
    import cv2
    import numpy as np

    orb = cv2.ORB_create(nfeatures=1000)
    kp1, des1 = orb.detectAndCompute(tpl_cv, None)
    kp2, des2 = orb.detectAndCompute(screen_cv, None)

    if des1 is None or des2 is None or len(des1) < 4 or len(des2) < 4:
        return {"found": False}

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    matches = bf.knnMatch(des1, des2, k=2)

    # Lowe ratio test
    good = []
    for pair in matches:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

    if len(good) < 4:
        return {"found": False}

    src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    if H is None:
        return {"found": False}

    inlier_mask = mask.ravel().tolist()
    inliers = sum(1 for v in inlier_mask if v == 1)
    if inliers < 4:
        return {"found": False}

    confidence = inliers / len(good)

    # Scale threshold for feature matching (ORB ratios are typically 0.3-0.8 vs TM's 0.7-0.99)
    effective_threshold = threshold * 0.6
    if confidence < effective_threshold:
        return {"found": False}

    # Project template corners to screen
    h, w = tpl_cv.shape[:2]
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    projected = cv2.perspectiveTransform(corners, H)
    xs = projected[:, 0, 0]
    ys = projected[:, 0, 1]
    x, y = int(min(xs)), int(min(ys))
    bw, bh = int(max(xs)) - x, int(max(ys)) - y

    return {
        "found": True,
        "best": {"x": x, "y": y},
        "confidence": round(float(confidence), 4),
        "template_size": {"width": bw, "height": bh},
        "method": "feature",
        "inliers": inliers,
        "total_good": len(good),
    }


@mcp.tool()
def save_template(name: str, x: int, y: int, width: int, height: int,
                  monitor: int = 0) -> dict:
    """Crop a region from the current screen as a reusable template for find_on_screen.

    Args:
        name: Template name (alphanumeric, underscores, hyphens)
        x: Left edge X coordinate
        y: Top edge Y coordinate
        width: Region width in pixels
        height: Region height in pixels
        monitor: Monitor to capture from (0=all, 1+=specific)
    """
    tpl_dir = DATA_DIR / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', name)
    tpl_path = tpl_dir / f"{safe_name}.png"
    if tpl_path.resolve().parent != tpl_dir.resolve():
        return {"error": "Invalid template name."}
    img = _grab_monitor(monitor)
    if img is None:
        return {"error": f"Invalid monitor index {monitor}."}
    cropped = img.crop((x, y, x + width, y + height))
    cropped.save(str(tpl_path))
    return {"saved": safe_name, "path": str(tpl_path), "width": width, "height": height}


@mcp.tool()
def find_on_screen(template: str, threshold: float = 0.8,
                   multi_scale: bool = True, monitor: int = 0,
                   method: str = "auto") -> dict:
    """Find a template image on screen via OpenCV template matching.
    Supports multi-scale matching to handle DPI/zoom differences.
    Can use ORB feature matching for better resilience to rotation/scale.

    Args:
        template: Template name (without .png extension, must be saved first)
        threshold: Match confidence threshold 0-1 (default 0.8)
        multi_scale: Try multiple scales if initial match fails (default True)
        monitor: Monitor to search on (0=all, 1+=specific)
        method: "auto" (try ORB first, fall back to template), "feature" (ORB only),
                or "template" (template matching only, default behavior)
    """
    import cv2
    import numpy as np

    tpl_dir = DATA_DIR / "templates"
    tpl_path = tpl_dir / f"{template}.png"
    if not tpl_path.is_file():
        return {"found": False, "error": f"Template '{template}' not found. Save one first with save_template."}
    tpl_cv = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl_cv is None:
        return {"found": False, "error": f"Failed to read template '{template}'."}

    img = _grab_monitor(monitor)
    if img is None:
        return {"found": False, "error": f"Invalid monitor index {monitor}."}
    screen_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    if method == "feature":
        return _match_feature_method(screen_cv, tpl_cv, threshold)

    if method == "template":
        return _match_template_method(screen_cv, tpl_cv, threshold, multi_scale)

    # Auto: try feature first, fall back to template
    result = _match_feature_method(screen_cv, tpl_cv, threshold)
    if result.get("found"):
        return result
    return _match_template_method(screen_cv, tpl_cv, threshold, multi_scale)


@mcp.tool()
def find_and_click_all(template: str, button: str = "left", threshold: float = 0.8,
                       min_distance: int = 20, multi_scale: bool = True) -> dict:
    """Find all instances of a template on screen and click each one.
    Uses min_distance clustering to avoid clicking duplicates.

    Args:
        template: Template name (without .png extension)
        button: Mouse button - left, right, or middle
        threshold: Match confidence 0-1
        min_distance: Min pixels between clicks to avoid duplicates
        multi_scale: Try multiple scales (default True)
    """
    import cv2
    import numpy as np
    import pyautogui

    tpl_dir = DATA_DIR / "templates"
    tpl_path = tpl_dir / f"{template}.png"
    if not tpl_path.is_file():
        return {"error": f"Template '{template}' not found."}
    tpl_pil = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl_pil is None:
        return {"error": f"Failed to read template '{template}'."}

    screen = _grab_monitor(0)
    screen_cv = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)

    scales = [1.0] if not multi_scale else MULTI_SCALE_STEPS
    all_centers = []

    for scale in scales:
        if scale == 1.0:
            tpl = tpl_pil
        else:
            w = int(tpl_pil.shape[1] * scale)
            h = int(tpl_pil.shape[0] * scale)
            if w < 5 or h < 5:
                continue
            tpl = cv2.resize(tpl_pil, (w, h))

        th, tw = tpl.shape[:2]
        sh, sw = screen_cv.shape[:2]
        if tw > sw or th > sh:
            continue

        result = cv2.matchTemplate(screen_cv, tpl, cv2.TM_CCOEFF_NORMED)
        locs = np.where(result >= threshold)
        for lx, ly in zip(locs[1], locs[0]):
            cx = int(lx + tw / 2)
            cy = int(ly + th / 2)
            conf = float(result[ly, lx])
            all_centers.append({"x": cx, "y": cy, "confidence": round(conf, 4)})

    if not all_centers:
        return {"clicked": 0, "matches": 0}

    # Cluster by min_distance (greedy)
    all_centers.sort(key=lambda c: c["confidence"], reverse=True)
    clustered = []
    for c in all_centers:
        too_close = False
        for existing in clustered:
            if abs(c["x"] - existing["x"]) < min_distance and abs(c["y"] - existing["y"]) < min_distance:
                too_close = True
                break
        if not too_close:
            clustered.append(c)

    for c in clustered:
        pyautogui.click(x=c["x"], y=c["y"], button=button)

    return {"clicked": len(clustered), "matches": len(all_centers),
            "points": [{"x": c["x"], "y": c["y"]} for c in clustered]}


# ── UI Element Detection ──────────────────────────────────────────────────────

def _parse_element_query(query):
    """Parse a natural-language query into color and shape extraction hints.
    Returns dict: colors=[...], shapes=[...], mode='color'|'shape'|'color_shape'|'default'"""
    q = query.lower()
    color_map = {
        "red": "red", "blue": "blue", "green": "green", "yellow": "yellow",
        "orange": "orange", "purple": "purple", "white": "white", "gray": "gray",
        "grey": "gray", "black": "black",
    }
    colors = [color_map[w] for w in color_map if w in q]
    # Deduplicate preserving order
    seen = set()
    colors = [c for c in colors if not (c in seen or seen.add(c))]

    shapes = []
    shape_map = {
        "button": "rectangle", "rect": "rectangle", "rectangle": "rectangle",
        "square": "rectangle", "box": "rectangle", "bar": "rectangle",
        "circle": "circle", "round": "circle", "dot": "circle",
        "oval": "circle", "ellipse": "circle",
        "icon": "blob", "blob": "blob", "shape": "blob",
        "close": "circle",
    }
    for word, shape in shape_map.items():
        if word in q:
            shapes.append(shape)
    seen_s = set()
    shapes = [s for s in shapes if not (s in seen_s or seen_s.add(s))]

    # Special: "close button" means red circle
    if "close" in q and "button" in q:
        colors = ["red"]
        shapes = ["circle"]
    elif "close" in q:
        colors = colors or ["red"]
        shapes = ["circle"]

    if colors and shapes:
        mode = "color_shape"
    elif colors:
        mode = "color"
    elif shapes:
        mode = "shape"
    else:
        mode = "default"

    return {"colors": colors, "shapes": shapes, "mode": mode}


def _apply_color_mask(screen_cv, color_name):
    """Apply HSV mask for a color name. Returns masked image (white=match)."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(screen_cv, cv2.COLOR_BGR2HSV)
    ranges = _COLOR_HSV_RANGES.get(color_name)
    if ranges is None:
        return None

    if len(ranges) == 4:  # Two ranges (red)
        lo1, hi1, lo2, hi2 = ranges
        mask1 = cv2.inRange(hsv, np.array(lo1), np.array(hi1))
        mask2 = cv2.inRange(hsv, np.array(lo2), np.array(hi2))
        return cv2.bitwise_or(mask1, mask2)
    else:
        lo, hi = ranges[0], ranges[1]
        return cv2.inRange(hsv, np.array(lo), np.array(hi))


def _detect_by_color(screen_cv, color_name, min_area):
    """Detect regions matching a color. Returns list of element dicts."""
    import cv2

    mask = _apply_color_mask(screen_cv, color_name)
    if mask is None:
        return []

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    elements = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        elements.append({
            "x": x, "y": y, "width": w, "height": h,
            "center_x": x + w // 2, "center_y": y + h // 2,
            "color": color_name, "shape": None, "area": int(area),
        })
    return elements


def _detect_by_shape(screen_cv, shape_name, min_area):
    """Detect contours matching a shape. Returns list of element dicts."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(screen_cv, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        x, y, w, h = cv2.boundingRect(cnt)
        perimeter = cv2.arcLength(cnt, True)

        detected_shape = None
        if shape_name == "rectangle":
            # approxPolyDP: 4 vertices = rectangle-ish
            approx = cv2.approxPolyDP(cnt, 0.02 * perimeter, True)
            if len(approx) == 4:
                detected_shape = "rectangle"
        elif shape_name == "circle":
            # Circularity check
            if perimeter > 0:
                circularity = 4 * np.pi * area / (perimeter * perimeter)
                if circularity > 0.85:
                    detected_shape = "circle"
        elif shape_name == "blob":
            # Any significant contour
            detected_shape = "blob"

        if detected_shape:
            elements.append({
                "x": x, "y": y, "width": w, "height": h,
                "center_x": x + w // 2, "center_y": y + h // 2,
                "color": None, "shape": detected_shape, "area": int(area),
            })

    return elements


def _detect_by_color_shape(screen_cv, color_name, shape_name, min_area):
    """Detect regions matching both a color and shape. Returns list of element dicts."""
    import cv2
    import numpy as np

    mask = _apply_color_mask(screen_cv, color_name)
    if mask is None:
        return []

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        x, y, w, h = cv2.boundingRect(cnt)
        perimeter = cv2.arcLength(cnt, True)

        match = False
        detected_shape = None
        if shape_name == "rectangle":
            approx = cv2.approxPolyDP(cnt, 0.02 * perimeter, True)
            match = len(approx) == 4
            detected_shape = "rectangle"
        elif shape_name == "circle":
            if perimeter > 0:
                circularity = 4 * np.pi * area / (perimeter * perimeter)
                match = circularity > 0.85
                detected_shape = "circle"
        elif shape_name == "blob":
            match = True
            detected_shape = "blob"

        if match:
            elements.append({
                "x": x, "y": y, "width": w, "height": h,
                "center_x": x + w // 2, "center_y": y + h // 2,
                "color": color_name, "shape": detected_shape, "area": int(area),
            })

    return elements


def _detect_default(screen_cv, min_area):
    """Default detection: find all significant contours (no color/shape filter).
    Returns list of element dicts."""
    import cv2

    gray = cv2.cvtColor(screen_cv, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        elements.append({
            "x": x, "y": y, "width": w, "height": h,
            "center_x": x + w // 2, "center_y": y + h // 2,
            "color": None, "shape": None, "area": int(area),
        })
    return elements


def _nms_elements(elements, overlap_threshold=0.3):
    """Non-max suppression by IoU to remove overlapping detections."""
    if not elements:
        return []

    # Sort by area descending (prefer larger detections)
    elements.sort(key=lambda e: e["area"], reverse=True)
    keep = []

    for elem in elements:
        too_close = False
        for kept in keep:
            # Compute IoU
            x1 = max(elem["x"], kept["x"])
            y1 = max(elem["y"], kept["y"])
            x2 = min(elem["x"] + elem["width"], kept["x"] + kept["width"])
            y2 = min(elem["y"] + elem["height"], kept["y"] + kept["height"])

            inter = max(0, x2 - x1) * max(0, y2 - y1)
            union = elem["area"] + kept["area"] - inter
            if union > 0 and inter / union > overlap_threshold:
                too_close = True
                break
        if not too_close:
            keep.append(elem)

    return keep


@mcp.tool()
def find_elements(query: str, region: str = "", min_area: int = 500,
                  monitor: int = 0) -> dict:
    """Find UI elements on screen by color, shape, or both — no template needed.
    Uses OpenCV contour detection, HSV color masking, and shape classification.

    Args:
        query: What to find, e.g. "blue buttons", "red circles", "close button", "icons"
        region: Optional crop region as 'x,y,w,h'
        min_area: Minimum contour area in pixels (default 500, filters noise)
        monitor: Monitor to search (0=all, 1+=specific)
    """
    import cv2
    import numpy as np

    img = _grab_monitor(monitor)
    if img is None:
        return {"error": f"Invalid monitor index {monitor}."}

    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                img = img.crop((parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3]))
        except ValueError:
            pass

    screen_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    parsed = _parse_element_query(query)

    elements = []
    if parsed["mode"] == "color_shape":
        # For each color+shape combination
        for color in parsed["colors"]:
            for shape in parsed["shapes"]:
                elements.extend(_detect_by_color_shape(screen_cv, color, shape, min_area))
    elif parsed["mode"] == "color":
        for color in parsed["colors"]:
            elements.extend(_detect_by_color(screen_cv, color, min_area))
    elif parsed["mode"] == "shape":
        for shape in parsed["shapes"]:
            elements.extend(_detect_by_shape(screen_cv, shape, min_area))
    else:
        elements = _detect_default(screen_cv, min_area)

    elements = _nms_elements(elements)

    return {
        "count": len(elements),
        "query": query,
        "parsed": {"colors": parsed["colors"], "shapes": parsed["shapes"], "mode": parsed["mode"]},
        "elements": elements[:50],  # cap to prevent huge responses
    }


@mcp.tool()
def click_element(query: str, index: int = 0, min_area: int = 500,
                  monitor: int = 0) -> dict:
    """Find a UI element by description and click it.
    Uses find_elements internally to locate the target, then clicks at its center.

    Args:
        query: What to find, e.g. "blue button", "red circle", "close button"
        index: Which element to click (0 = first, default)
        min_area: Minimum contour area (default 500)
        monitor: Monitor to search (0=all, 1+=specific)
    """
    import pyautogui

    result = find_elements(query=query, min_area=min_area, monitor=monitor)
    if "error" in result:
        return result

    elements = result.get("elements", [])
    if not elements:
        return {"clicked": False, "error": f"No elements found matching '{query}'."}

    if index >= len(elements):
        return {"clicked": False, "error": f"Index {index} out of range. Found {len(elements)} elements."}

    elem = elements[index]
    pyautogui.click(x=elem["center_x"], y=elem["center_y"])

    return {
        "clicked": True,
        "element": elem,
        "index": index,
        "total_found": len(elements),
    }


# ── Mouse ───────────────────────────────────────────────────────────────────────

@mcp.tool()
def computer_click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
    """Click at coordinates on the physical display.

    Args:
        x: X coordinate
        y: Y coordinate
        button: Mouse button - left, right, or middle (default left)
        clicks: Number of clicks (default 1)
    """
    import pyautogui
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    return {"clicked": {"x": x, "y": y}, "button": button, "clicks": clicks}


@mcp.tool()
def computer_move(x: int, y: int, duration: float = 0.3) -> dict:
    """Move mouse to coordinates without clicking.

    Args:
        x: X coordinate
        y: Y coordinate
        duration: Movement duration in seconds (default 0.3)
    """
    import pyautogui
    pyautogui.moveTo(x=x, y=y, duration=duration)
    return {"moved_to": {"x": x, "y": y}}


@mcp.tool()
def computer_scroll(amount: int = 3, direction: str = "down") -> dict:
    """Scroll at current mouse position.

    Args:
        amount: Number of scroll clicks (default 3)
        direction: Scroll direction - up or down (default down)
    """
    import pyautogui
    clicks = amount if direction == "up" else -amount
    pyautogui.scroll(clicks)
    return {"scrolled": direction, "amount": amount}


@mcp.tool()
def computer_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> dict:
    """Drag from point A to point B on the physical display.

    Args:
        x1: Start X coordinate
        y1: Start Y coordinate
        x2: End X coordinate
        y2: End Y coordinate
        duration: Drag duration in seconds (default 0.5)
    """
    import pyautogui
    pyautogui.moveTo(x=x1, y=y1)
    pyautogui.drag(x2 - x1, y2 - y1, duration=duration)
    return {"dragged": {"from": {"x": x1, "y": y1}, "to": {"x": x2, "y": y2}}}


# ── Keyboard ────────────────────────────────────────────────────────────────────

@mcp.tool()
def computer_type(text: str, interval: float = 0.05) -> dict:
    """Type text or press special keys. Use key combos like 'ctrl+a', 'alt+tab', 'ctrl+shift+del' for hotkeys. Plain text is typed character by character.

    Args:
        text: Text to type or key combo (e.g. 'ctrl+a', 'enter', 'alt+tab')
        interval: Seconds between keystrokes (default 0.05)
    """
    import pyautogui

    # Known key names for hotkey detection
    _KNOWN_KEYS = {
        "ctrl", "alt", "shift", "win", "cmd", "super", "fn",
        "a","b","c","d","e","f","g","h","i","j","k","l","m",
        "n","o","p","q","r","s","t","u","v","w","x","y","z",
        "0","1","2","3","4","5","6","7","8","9",
        "f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11","f12",
        "f13","f14","f15","f16","f17","f18","f19","f20","f21","f22","f23","f24",
        "enter", "return", "tab", "escape", "esc", "space", "backspace",
        "delete", "del", "home", "end", "pageup", "pgup", "pagedown", "pgdn",
        "up", "down", "left", "right",
        "insert", "ins", "printscreen", "scrolllock", "numlock", "capslock",
        "plus", "minus", "comma", "period", "slash", "backslash",
        "apps", "menu",
    }

    parts = text.strip().split('+')
    if len(parts) > 1:
        # Hotkey mode: all parts must be known keys
        keys = [p.lower().strip() for p in parts]
        unknown = [k for k in keys if k not in _KNOWN_KEYS]
        if unknown:
            return {"error": f"Unknown key(s) in combo: {unknown}. Valid keys include letters, f1-f24, enter, tab, escape, etc."}
        pyautogui.hotkey(*keys, interval=interval)
        return {"hotkey": keys}
    # Special single keys
    single = text.strip().lower()
    if single in _KNOWN_KEYS:
        pyautogui.press(single)
        return {"pressed": single}
    pyautogui.write(text, interval=interval)
    return {"typed": text}


# ── Window Management ──────────────────────────────────────────────────────────

@mcp.tool()
def window_list(title_filter: str = "", visible_only: bool = True) -> dict:
    """List open windows. Returns handle, title, class, rect for each.
    Filter by title substring. Only visible windows by default."""
    import win32gui
    results = []
    filter_lower = title_filter.lower() if title_filter else ""
    def callback(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if filter_lower and filter_lower not in title.lower():
            return
        cls = win32gui.GetClassName(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        results.append({
            "hwnd": hwnd, "title": title, "class": cls,
            "rect": {"left": left, "top": top, "right": right, "bottom": bottom,
                     "width": right - left, "height": bottom - top},
        })
    win32gui.EnumWindows(callback, None)
    return {"windows": results, "count": len(results)}


@mcp.tool()
def window_focus(title_or_handle: str = "", bring_to_front: bool = True) -> dict:
    """Focus a window by title substring or handle ID.
    If minimized, restores it first."""
    import win32con
    import win32gui
    handle_str = title_or_handle.strip()
    # Try parsing as int handle
    try:
        hwnd = int(handle_str)
        title = win32gui.GetWindowText(hwnd)
    except ValueError:
        hwnd, title = _find_window(handle_str)
    if hwnd is None:
        return {"error": title}  # title contains error msg here
    # Restore if minimized
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if bring_to_front:
        win32gui.SetForegroundWindow(hwnd)
    return {"focused": title, "hwnd": hwnd}


@mcp.tool()
def window_move(title_or_handle: str = "", x: int = 0, y: int = 0) -> dict:
    """Move a window to specific coordinates."""
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    return {"moved": title, "hwnd": hwnd, "x": x, "y": y}


@mcp.tool()
def window_resize(title_or_handle: str = "", width: int = 800, height: int = 600) -> dict:
    """Resize a window. Keeps current position."""
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    rect = win32gui.GetWindowRect(hwnd)
    win32gui.MoveWindow(hwnd, rect[0], rect[1], width, height, True)
    return {"resized": title, "hwnd": hwnd, "width": width, "height": height}


@mcp.tool()
def window_maximize(title_or_handle: str = "") -> dict:
    """Maximize a window."""
    import win32con
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return {"maximized": title, "hwnd": hwnd}


@mcp.tool()
def window_minimize(title_or_handle: str = "") -> dict:
    """Minimize a window."""
    import win32con
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return {"minimized": title, "hwnd": hwnd}


@mcp.tool()
def window_close(title_or_handle: str = "") -> dict:
    """Close a window gracefully via WM_CLOSE."""
    import win32con
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return {"closed": title, "hwnd": hwnd}


@mcp.tool()
def window_screenshot(title_or_handle: str = "") -> dict:
    """Capture a screenshot of a specific window.
    Works for windows on any monitor using GetWindowRect + ImageGrab."""
    import win32gui
    hwnd, title = _find_window(title_or_handle)
    if hwnd is None:
        return {"error": title}
    rect = win32gui.GetWindowRect(hwnd)
    bbox = (rect[0], rect[1], rect[2], rect[3])
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return {"error": f"Window '{title}' has invalid bounds."}
    img = _grab(bbox=bbox)
    img_dir = DATA_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', title[:30])
    ts = int(time.time())
    path = img_dir / f"window_{safe_name}_{ts}.png"
    img.save(str(path))
    return {"path": str(path), "title": title, "hwnd": hwnd,
            "width": img.width, "height": img.height}


# ── Pixel Color ─────────────────────────────────────────────────────────────────

@mcp.tool()
def pixel_color(x: int, y: int) -> dict:
    """Get the RGB color and hex value at a specific screen coordinate.

    Args:
        x: X coordinate
        y: Y coordinate
    """
    import pyautogui
    r, g, b = pyautogui.pixel(x, y)
    return {"x": x, "y": y, "rgb": {"r": r, "g": g, "b": b},
            "hex": f"#{r:02x}{g:02x}{b:02x}"}


@mcp.tool()
def pixel_color_region(x: int, y: int, w: int = 10, h: int = 10) -> dict:
    """Get the average RGB color of a screen region.

    Args:
        x: Left edge X coordinate
        y: Top edge Y coordinate
        w: Region width (default 10)
        h: Region height (default 10)
    """
    import cv2
    import numpy as np
    img = _grab(bbox=(x, y, x + w, y + h))
    arr = np.array(img)
    mean = cv2.mean(arr)[:3]
    b, g, r = int(mean[0]), int(mean[1]), int(mean[2])
    return {"x": x, "y": y, "width": w, "height": h,
            "rgb": {"r": r, "g": g, "b": b},
            "hex": f"#{r:02x}{g:02x}{b:02x}"}


# ── Wait for Change ─────────────────────────────────────────────────────────────

@mcp.tool()
def wait_for_change(region: str = "", timeout: float = 30.0,
                     interval: float = 0.5, threshold: float = 10.0) -> dict:
    """Wait until a screen region changes visually. Polls until mean pixel
    difference exceeds threshold, or timeout is reached.

    Args:
        region: Region to watch as 'x,y,w,h'. Empty = full screen.
        timeout: Max seconds to wait (default 30)
        interval: Poll interval in seconds (default 0.5)
        threshold: Mean pixel difference to trigger (default 10.0)
    """
    import numpy as np

    bbox = None
    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                bbox = (parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3])
        except ValueError:
            return {"error": f"Invalid region format '{region}'. Use 'x,y,w,h'."}

    import numpy as np
    baseline = np.array(_grab(bbox=bbox), dtype=np.float32)
    start = time.time()

    while time.time() - start < timeout:
        time.sleep(interval)
        current = np.array(_grab(bbox=bbox), dtype=np.float32)
        diff = float(np.mean(np.abs(current - baseline)))
        if diff > threshold:
            return {"changed": True, "elapsed": round(time.time() - start, 2),
                    "mean_diff": round(diff, 2)}

    return {"changed": False, "elapsed": round(time.time() - start, 2),
            "mean_diff": 0.0, "error": f"Timeout after {timeout}s"}


# ── Screen Diff ─────────────────────────────────────────────────────────────────

@mcp.tool()
def screen_diff(region: str = "", baseline_path: str = "",
                describe: bool = False) -> dict:
    """Compare current screen against a saved baseline image.
    If baseline_path is empty, saves current screen as baseline and returns path.

    Args:
        region: Region to compare as 'x,y,w,h'. Empty = full screen.
        baseline_path: Path to baseline image (empty = capture and save new baseline)
        describe: If True, send the diff to vision model for description
    """
    import cv2
    import numpy as np

    bbox = None
    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                bbox = (parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3])
        except ValueError:
            return {"error": f"Invalid region format '{region}'."}

    current = _grab(bbox=bbox)
    current_cv = cv2.cvtColor(np.array(current), cv2.COLOR_RGB2BGR)

    if not baseline_path:
        img_dir = DATA_DIR / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        path = img_dir / f"baseline_{ts}.png"
        current.save(str(path))
        return {"saved_baseline": str(path), "message": "Baseline saved. Call again with this path to compare."}

    baseline = cv2.imread(baseline_path, cv2.IMREAD_COLOR)
    if baseline is None:
        return {"error": f"Cannot read baseline image '{baseline_path}'."}

    # Resize baseline to match current if needed
    if baseline.shape[:2] != current_cv.shape[:2]:
        baseline = cv2.resize(baseline, (current_cv.shape[1], current_cv.shape[0]))

    diff = cv2.absdiff(current_cv, baseline)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    mse = float(np.mean(gray.astype(np.float32) ** 2))
    changed_pixels = int(np.count_nonzero(gray > 30))

    result = {
        "mse": round(mse, 2),
        "changed_pixels": changed_pixels,
        "total_pixels": gray.size,
        "pct_changed": round(100.0 * changed_pixels / gray.size, 2),
    }

    if describe and changed_pixels > 100:
        diff_img = Image.fromarray(cv2.cvtColor(diff, cv2.COLOR_BGR2RGB))
        desc = _vision_call(diff_img, "Describe the visual differences visible in this diff image. What changed?")
        result["description"] = desc
    elif describe:
        result["description"] = "No significant changes detected."

    return result


# ── OCR ──────────────────────────────────────────────────────────────────────────

@mcp.tool()
def screen_ocr(region: str = "", question: str = "") -> str:
    """Extract text from the screen. Uses RapidOCR remote service for fast OCR.
    Falls back to vision model only when a question is asked.

    Args:
        region: Region to OCR as 'x,y,w,h'. Empty = full screen.
        question: Optional specific question (e.g. 'What is the price shown?')
    """
    t0 = time.time()
    bbox = None
    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                bbox = (parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3])
        except ValueError:
            return json.dumps({"error": f"Invalid region format '{region}'."})

    img = _grab(bbox=bbox)

    # If a question is asked, use vision model
    if question:
        result = _vision_call(img, question)
        _trace("screen_ocr", {"vision_model": True}, time.time() - t0)
        return result

    # Fast path: RapidOCR remote service (HTTP)
    try:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        payload = json.dumps({"image": b64}).encode()
        req = urllib.request.Request(
            OCR_SERVICE_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if result.get("lines", 0) > 0:
            _trace("screen_ocr", {"rapid_ocr": True, "lines": result["lines"]}, time.time() - t0)
            return result["text"]
    except Exception:
        pass  # Fall through to vision model

    # Fallback: vision model
    prompt = "Extract all visible text from this image. Return only the text content, preserving layout."
    result = _vision_call(img, prompt)
    _trace("screen_ocr", {"vision_model": True}, time.time() - t0)
    return result


# ── Clipboard Image ─────────────────────────────────────────────────────────────

@mcp.tool()
def clipboard_get_image() -> dict:
    """Get the current image from the system clipboard. Saves to data/images/ and returns path."""
    import win32clipboard
    try:
        win32clipboard.OpenClipboard()
        fmt = win32clipboard.EnumClipboardFormats(0)
        cf_dib = 8  # CF_DIB
        has_image = False
        while fmt:
            if fmt == cf_dib:
                has_image = True
                break
            fmt = win32clipboard.EnumClipboardFormats(fmt)
        if not has_image:
            win32clipboard.CloseClipboard()
            return {"error": "No image on clipboard."}
        data = win32clipboard.GetClipboardData(cf_dib)
        win32clipboard.CloseClipboard()
    except Exception as e:
        return {"error": f"Clipboard access error: {e}"}

    import io
    img_dir = DATA_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = img_dir / f"clipboard_{ts}.png"

    # Convert DIB to PNG using PIL
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.save(str(path))
    return {"path": str(path), "width": img.width, "height": img.height}


@mcp.tool()
def clipboard_set_image(path: str) -> dict:
    """Copy an image file to the system clipboard.

    Args:
        path: Path to the image file (PNG, BMP, JPG)
    """
    import win32clipboard
    import win32con
    from PIL import Image

    p = Path(path)
    if not p.is_file():
        return {"error": f"File not found: {path}"}

    try:
        img = Image.open(p)
        # Convert to BMP for clipboard
        buf = BytesIO()
        img.save(buf, format="BMP")
        bmp_data = buf.getvalue()

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, bmp_data)
        win32clipboard.CloseClipboard()
        return {"copied": str(p), "width": img.width, "height": img.height}
    except Exception as e:
        return {"error": f"Failed to copy image: {e}"}


# ── Accessibility Tree ─────────────────────────────────────────────────────────

@mcp.tool()
def accessibility_tree(title_or_handle: str = "", depth: int = 5) -> dict:
    """Get the UI Automation (UIA) tree for a window.
    Returns element names, types, automation IDs, bounding rectangles, and states.
    Use this to find clickable elements by AutomationId instead of pixel coordinates.

    Args:
        title_or_handle: Window title substring or handle ID (empty = foreground window)
        depth: Max tree depth (default 5)
    """
    from pywinauto import Desktop
    try:
        desktop = Desktop(backend="uia")
        if title_or_handle.strip():
            handle_str = title_or_handle.strip()
            try:
                hwnd = int(handle_str)
                window = desktop.window(handle=hwnd)
            except ValueError:
                window = desktop.window(title_re=".*" + re.escape(handle_str) + ".*")
        else:
            window = desktop.top_window()

        def _walk(element, d):
            if d > depth:
                return None
            try:
                info = element.element_info
                node = {
                    "name": info.name[:100] if info.name else "",
                    "type": info.class_name,
                    "automation_id": info.automation_id if info.automation_id else "",
                    "visible": info.visible,
                }
                rect = info.rectangle
                if rect:
                    node["rect"] = {"left": rect.left, "top": rect.top,
                                    "right": rect.right, "bottom": rect.bottom}
            except Exception:
                return None
            children = []
            try:
                for child in element.children():
                    child_node = _walk(child, d + 1)
                    if child_node:
                        children.append(child_node)
            except Exception:
                pass
            if children:
                node["children"] = children
            return node

        tree = _walk(window, 0)
        return {"window": window.window_text(), "tree": tree}
    except Exception as e:
        return {"error": f"Accessibility tree error: {e}"}


@mcp.tool()
def click_by_automation_id(automation_id: str, title_or_handle: str = "",
                            action: str = "click") -> dict:
    """Click a UI element by its AutomationId. Finds it via the UIA tree.
    More reliable than pixel coordinates — works regardless of window position or DPI.

    Args:
        automation_id: The AutomationId of the target element
        title_or_handle: Window to search in (empty = foreground)
        action: "click", "double_click", "right_click", or "invoke"
    """
    from pywinauto import Desktop
    try:
        desktop = Desktop(backend="uia")
        if title_or_handle.strip():
            try:
                hwnd = int(title_or_handle.strip())
                window = desktop.window(handle=hwnd)
            except ValueError:
                window = desktop.window(title_re=".*" + re.escape(title_or_handle) + ".*")
        else:
            window = desktop.top_window()

        def _find_by_id(element, target_id):
            try:
                info = element.element_info
                if info.automation_id == target_id:
                    return element
            except Exception:
                pass
            try:
                for child in element.children():
                    found = _find_by_id(child, target_id)
                    if found:
                        return found
            except Exception:
                pass
            return None

        elem = _find_by_id(window, automation_id)
        if not elem:
            return {"error": f"AutomationId '{automation_id}' not found."}

        if action == "click":
            elem.click_input()
        elif action == "double_click":
            elem.double_click_input()
        elif action == "right_click":
            elem.right_click_input()
        elif action == "invoke":
            elem.invoke()
        else:
            return {"error": f"Unknown action '{action}'. Use click, double_click, right_click, or invoke."}

        return {"clicked": automation_id, "action": action}
    except Exception as e:
        return {"error": f"UIA click error: {e}"}


# ── Shell / Process Tools ──────────────────────────────────────────────────────

@mcp.tool()
def shell_run(command: str, timeout: int = 30, cwd: str = "") -> dict:
    """Run a shell command and return output. Use for launching apps, checking processes, one-liners.

    Args:
        command: Shell command to execute
        timeout: Max wait time in seconds (default 30)
        cwd: Working directory (empty = current)
    """
    t0 = time.time()
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd if cwd else None,
        )
        elapsed = round(time.time() - t0, 3)
        entry = {"exit_code": result.returncode, "elapsed": elapsed}
        if result.stdout:
            out = result.stdout.strip()
            entry["stdout"] = out[:2000]
            if len(out) > 2000:
                entry["stdout_truncated"] = True
        if result.stderr:
            err = result.stderr.strip()
            entry["stderr"] = err[:1000]
        _trace("shell_run", entry, elapsed)
        return entry
    except subprocess.TimeoutExpired:
        _trace("shell_run", {"error": f"Timeout after {timeout}s"}, time.time() - t0)
        return {"error": f"Command timed out after {timeout}s", "exit_code": -1}
    except Exception as e:
        _trace("shell_run", {"error": str(e)}, time.time() - t0)
        return {"error": str(e), "exit_code": -1}


@mcp.tool()
def launch_app(name: str, args: str = "") -> dict:
    """Launch an application by name. Handles common Windows apps.

    Args:
        name: Application name or path (e.g. 'notepad', 'chrome', 'C:\\path\\to\\app.exe')
        args: Command line arguments
    """
    t0 = time.time()
    try:
        cmd = name if os.path.isabs(name) else f"start {name}"
        if args:
            cmd = f"{cmd} {args}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        elapsed = round(time.time() - t0, 3)
        entry = {"launched": name, "exit_code": result.returncode, "elapsed": elapsed}
        if result.stderr:
            entry["stderr"] = result.stderr.strip()[:500]
        _trace("launch_app", entry, elapsed)
        return entry
    except Exception as e:
        _trace("launch_app", {"error": str(e)}, time.time() - t0)
        return {"error": str(e)}


@mcp.tool()
def process_list(name_filter: str = "") -> dict:
    """List running processes. Filter by name substring.

    Args:
        name_filter: Process name to filter by (e.g. 'chrome', 'python')
    """
    import psutil
    try:
        filter_lower = name_filter.lower() if name_filter else ""
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = proc.info['name'] or ""
                if filter_lower and filter_lower not in name.lower():
                    continue
                cmdline = " ".join(proc.info['cmdline'] or [])[:200]
                processes.append({
                    "pid": proc.info['pid'],
                    "name": name,
                    "cmdline": cmdline,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"processes": processes, "count": len(processes)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def process_kill(pid: int, force: bool = False) -> dict:
    """Kill a process by PID.

    Args:
        pid: Process ID to kill
        force: Force kill (default False)
    """
    try:
        import signal
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)
        return {"killed": pid}
    except PermissionError:
        # Fallback to taskkill on Windows
        try:
            flag = "/F" if force else ""
            result = subprocess.run(f"taskkill {flag} /PID {pid}", shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                return {"killed": pid}
            return {"error": result.stderr.strip()}
        except Exception as e:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


# ── Action Trace ──────────────────────────────────────────────────────────────

@mcp.tool()
def action_trace(clear: bool = False, last_n: int = 0) -> dict:
    """Get the action trace log — records every tool call for crash diagnosis.
    When a multi-step automation fails at step 8, this shows what the UI
    state was at step 7 and every preceding step.

    Args:
        clear: Clear the trace after reading (default False)
        last_n: Only return last N entries (0 = all)
    """
    entries = list(_action_trace)
    if last_n > 0:
        entries = entries[-last_n:]
    if clear:
        _action_trace.clear()
    return {"trace": entries, "count": len(entries)}


# ── File System Tools ──────────────────────────────────────────────────────────

@mcp.tool()
def file_read(path: str, lines: int = 0) -> dict:
    """Read a file. Returns content as text.

    Args:
        path: File path to read
        lines: Number of lines to read from start (0 = all)
    """
    p = Path(path)
    if not p.is_file():
        return {"error": f"File not found: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        if lines > 0:
            content = "\n".join(content.split("\n")[:lines])
        return {"path": str(p), "size": len(content), "content": content}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def file_write(path: str, content: str) -> dict:
    """Write content to a file. Creates parent directories if needed.

    Args:
        path: File path to write
        content: Text content to write
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"written": str(p), "size": len(content)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def file_list(path: str = ".", pattern: str = "*") -> dict:
    """List files in a directory.

    Args:
        path: Directory path (default: current)
        pattern: Glob pattern to filter (default: *)
    """
    p = Path(path)
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    try:
        entries = []
        for f in sorted(p.glob(pattern)):
            entry = {"name": f.name, "path": str(f)}
            if f.is_dir():
                entry["type"] = "dir"
            else:
                entry["type"] = "file"
                entry["size"] = f.stat().st_size
            entries.append(entry)
        return {"path": str(p), "entries": entries, "count": len(entries)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def file_exists(path: str) -> dict:
    """Check if a file or directory exists.

    Args:
        path: Path to check
    """
    p = Path(path)
    return {"exists": p.exists(), "is_file": p.is_file(), "is_dir": p.is_dir(),
            "path": str(p)}


# ── Main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
