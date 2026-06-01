"""Computer Use MCP Server — PyAutoGUI + OpenCV + Ollama vision.

Controls the local desktop (mouse, keyboard, screenshots, screen understanding,
window management, pixel color, OCR, clipboard).
Runs as stdio MCP server via FastMCP.

Config via environment variables:
    COMPUTER_VISION_MODEL  - Ollama vision model (default: qwen2.5vl:3b)
    OLLAMA_BASE            - Ollama API base URL (default: http://localhost:11434)
    VISION_TIMEOUT         - Vision API timeout seconds (default: 300)
    SCREEN_MAX_DIMENSION   - Max dimension for vision screenshots (default: 1280)
"""

import base64
import json
import os
import re
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("computer-use")

# ── Config ───────────────────────────────────────────────────────────────────────

OLLAMA_BASE = os.getenv("OLLAMA_BASE", "http://localhost:11434")
COMPUTER_VISION_MODEL = os.getenv("COMPUTER_VISION_MODEL", "qwen2.5vl:3b")
VISION_TIMEOUT = int(os.getenv("VISION_TIMEOUT", "300"))
SCREEN_MAX_DIMENSION = int(os.getenv("SCREEN_MAX_DIMENSION", "1280"))
DATA_DIR = Path(os.getenv("COMPUTER_USE_DATA_DIR", "data"))

MULTI_SCALE_STEPS = [0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25]


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
                   multi_scale: bool = True, monitor: int = 0) -> dict:
    """Find a template image on screen via OpenCV template matching.
    Supports multi-scale matching to handle DPI/zoom differences.

    Args:
        template: Template name (without .png extension, must be saved first)
        threshold: Match confidence threshold 0-1 (default 0.8)
        multi_scale: Try multiple scales if initial match fails (default True)
        monitor: Monitor to search on (0=all, 1+=specific)
    """
    import cv2
    import numpy as np

    tpl_dir = DATA_DIR / "templates"
    tpl_path = tpl_dir / f"{template}.png"
    if not tpl_path.is_file():
        return {"found": False, "error": f"Template '{template}' not found. Save one first with save_template."}
    tpl_pil = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl_pil is None:
        return {"found": False, "error": f"Failed to read template '{template}'."}

    img = _grab_monitor(monitor)
    if img is None:
        return {"found": False, "error": f"Invalid monitor index {monitor}."}
    screen_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    # Try at original scale first, then multi-scale if needed
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
        # Count all matches at best scale
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
    combo_match = re.match(r'^([a-z]+(?:\+[a-z]+)+)$', text.strip().lower())
    if combo_match:
        keys = combo_match.group(1).split('+')
        pyautogui.hotkey(*keys, interval=interval)
        return {"hotkey": keys}
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
    """Extract text from the screen using Ollama vision. Crops a region
    and sends it to the vision model with an OCR prompt.

    Args:
        region: Region to OCR as 'x,y,w,h'. Empty = full screen.
        question: Optional specific question (e.g. 'What is the price shown?')
    """
    bbox = None
    if region:
        try:
            parts = [int(p.strip()) for p in region.split(",")]
            if len(parts) == 4:
                bbox = (parts[0], parts[1], parts[0] + parts[2], parts[1] + parts[3])
        except ValueError:
            return json.dumps({"error": f"Invalid region format '{region}'."})

    img = _grab(bbox=bbox)
    prompt = question if question else "Extract all visible text from this image. Return only the text content, preserving layout."
    return _vision_call(img, prompt)


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


# ── Main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
