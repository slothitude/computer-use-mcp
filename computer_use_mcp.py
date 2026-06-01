"""Computer Use MCP Server — PyAutoGUI + OpenCV + Ollama vision.

Controls the local desktop (mouse, keyboard, screenshots, screen understanding).
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


# ── Vision ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def computer_screenshot() -> dict:
    """Take a screenshot of the physical display. Returns path and dimensions."""
    import pyautogui
    img_dir = DATA_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = img_dir / f"screenshot_{ts}.png"
    img = pyautogui.screenshot()
    img.save(str(path))
    return {"path": str(path), "width": img.width, "height": img.height}


@mcp.tool()
def analyze_screen(question: str = "Describe what is on screen and any notable UI elements") -> str:
    """Take a screenshot and send it to a vision model for understanding. Returns a text description."""
    import pyautogui
    img = pyautogui.screenshot()
    # Downscale for faster inference
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


# ── Template Matching ──────────────────────────────────────────────────────────

@mcp.tool()
def save_template(name: str, x: int, y: int, width: int, height: int) -> dict:
    """Crop a region from the current screen as a reusable template for find_on_screen.

    Args:
        name: Template name (alphanumeric, underscores, hyphens)
        x: Left edge X coordinate
        y: Top edge Y coordinate
        width: Region width in pixels
        height: Region height in pixels
    """
    import pyautogui
    tpl_dir = DATA_DIR / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', name)
    tpl_path = tpl_dir / f"{safe_name}.png"
    if tpl_path.resolve().parent != tpl_dir.resolve():
        return {"error": "Invalid template name."}
    img = pyautogui.screenshot()
    cropped = img.crop((x, y, x + width, y + height))
    cropped.save(str(tpl_path))
    return {"saved": safe_name, "path": str(tpl_path), "width": width, "height": height}


@mcp.tool()
def find_on_screen(template: str, threshold: float = 0.8) -> dict:
    """Find a template image on screen via OpenCV template matching.

    Args:
        template: Template name (without .png extension, must be saved first)
        threshold: Match confidence threshold 0-1 (default 0.8)
    """
    import cv2
    import numpy as np
    import pyautogui
    tpl_dir = DATA_DIR / "templates"
    tpl_path = tpl_dir / f"{template}.png"
    if not tpl_path.is_file():
        return {"found": False, "error": f"Template '{template}' not found. Save one first with save_template."}
    tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl is None:
        return {"found": False, "error": f"Failed to read template '{template}'."}
    screen = pyautogui.screenshot()
    screen_cv = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
    result = cv2.matchTemplate(screen_cv, tpl, cv2.TM_CCOEFF_NORMED)
    locs = np.where(result >= threshold)
    points = list(zip(locs[1].tolist(), locs[0].tolist()))
    if not points:
        return {"found": False, "count": 0}
    best_idx = int(np.argmax(result))
    best_y, best_x = np.unravel_index(best_idx, result.shape)
    best_conf = float(result[best_y, best_x])
    return {
        "found": True,
        "best": {"x": int(best_x), "y": int(best_y)},
        "confidence": round(best_conf, 4),
        "count": len(points),
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
    combo_match = re.match(r'^([a-z]+(?:\+[a-z]+)+)$', text.strip().lower())
    if combo_match:
        keys = combo_match.group(1).split('+')
        pyautogui.hotkey(*keys, interval=interval)
        return {"hotkey": keys}
    pyautogui.write(text, interval=interval)
    return {"typed": text}


# ── Main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
