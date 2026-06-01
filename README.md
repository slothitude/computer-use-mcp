# computer-use-mcp

MCP server for desktop automation on Windows. PyAutoGUI + OpenCV + Vision AI + Win32.

48 tools for mouse, keyboard, screenshots, screen understanding, window management,
pixel color, OCR, clipboard, template matching, UI element detection, recording, and more.

## Setup

```bash
pip install mcp pyautogui opencv-contrib-python Pillow pywin32 psutil pywinauto
```

> Use `opencv-contrib-python` (not `opencv-python`) for ORB feature matching.
> `pywinauto` required for UIA/accessibility tools.

**Optional** (for local OCR fallback):
```bash
pip install rapidocr-onnxruntime
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_BACKEND` | `nvidia` | Vision provider: `ollama` or `nvidia` |
| `COMPUTER_VISION_MODEL` | `qwen2.5vl:3b` | Ollama vision model |
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama API base URL |
| `NVIDIA_VISION_URL` | *(NIM endpoint)* | NVIDIA NIM endpoint |
| `NVIDIA_VISION_MODEL` | `meta/llama-3.2-90b-vision-instruct` | NVIDIA model |
| `NVIDIA_API_KEY` | *(from .mcp.json)* | NVIDIA API bearer token |
| `VISION_TIMEOUT` | `300` | Vision API timeout (seconds) |
| `SCREEN_MAX_DIMENSION` | `1280` | Max dimension for vision screenshots |
| `COMPUTER_USE_DATA_DIR` | `data` | Directory for templates and screenshots |
| `OCR_SERVICE_URL` | `http://192.168.0.33:8100/ocr` | Remote RapidOCR endpoint |
| `TRACE_LOG_PATH` | *(disabled)* | Set to a JSONL path to persist action trace to disk |

## Adding to Claude Desktop

In `claude_desktop_config.json` (or via `claude mcp add`):

```json
{
  "mcpServers": {
    "computer-use": {
      "command": "python",
      "args": ["-m", "computer_use_mcp"],
      "cwd": "C:/Users/aaron/computer-use-mcp",
      "env": {
        "NVIDIA_API_KEY": "your-key-here"
      }
    }
  }
}
```

## Tools

### Screenshots & Vision

| Tool | Description |
|------|-------------|
| `computer_screenshot(monitor, region, base64)` | Screenshot. `monitor=0` = all, `1+` = specific. `region="x,y,w,h"` crop. `base64=True` returns inline data URI for remote clients. |
| `analyze_screen(question, monitor)` | Send screenshot to vision AI. Returns text description. |
| `get_monitors()` | List connected monitors with bounds, resolution, primary flag. |
| `screen_record(duration, fps, region, monitor)` | Record short video to `data/videos/` for agent replay debugging. |

### Template Matching

| Tool | Description |
|------|-------------|
| `save_template(name, x, y, width, height, monitor)` | Crop a screen region and save as reusable template (`data/templates/`). |
| `find_on_screen(template, threshold, multi_scale, monitor, method)` | Find a template on screen. Results are NMS-deduplicated. |
| | `method="auto"` (default) — ORB feature matching first, falls back to template |
| | `method="feature"` — ORB only. Best for rotation/scale. |
| | `method="template"` — Multi-scale pixel matching only. |
| `find_and_click_all(template, button, threshold, min_distance, multi_scale)` | Find all instances and click each. Clusters nearby matches. |

### UI Element Detection (template-free)

| Tool | Description |
|------|-------------|
| `find_elements(query, region, min_area, monitor)` | Find UI elements by color, shape, or both — no template. Returns scored, sorted, NMS-deduplicated results. |
| `click_element(query, index, min_area, monitor)` | Find elements and click one at its center. |

**Query language:**

| Query | What it finds |
|-------|--------------|
| `"blue buttons"` | Blue-colored rectangles |
| `"red circles"` | Red circles (close buttons, indicators) |
| `"close button"` | Red circles (close/X buttons) |
| `"yellow"` | All yellow-colored regions |
| `"icons"` | Any blob-shaped contours |
| `"Save button"` | Rectangle-shaped elements |

**Colors:** red, blue, green, yellow, orange, purple, white, gray, black
**Shapes:** rectangle (button, rect, square, box, bar), circle (round, dot, oval, ellipse), blob (icon, shape)

### Mouse

| Tool | Description |
|------|-------------|
| `mouse_position()` | Get current mouse cursor position `{x, y}`. |
| `computer_click(x, y, button, clicks)` | Click at coordinates. Button: `left`/`right`/`middle`. |
| `computer_move(x, y, duration)` | Move mouse without clicking. |
| `computer_scroll(amount, direction)` | Scroll at current position. |
| `computer_drag(x1, y1, x2, y2, duration)` | Drag from point A to B. |

### Keyboard

| Tool | Description |
|------|-------------|
| `computer_type(text, interval)` | Type text or hotkeys. Supports `ctrl+a`, `alt+tab`, `win+d`, `f1`-`f24`, `delete`, `backspace`, arrows, numpad, and all standard keys. Unknown keys in combos produce an error instead of silently typing. |

### Window Management (Win32)

| Tool | Description |
|------|-------------|
| `window_list(title_filter, visible_only)` | Enumerate open windows. |
| `window_focus(title_or_handle, bring_to_front)` | Focus window. Uses edit-distance ranking when multiple partial matches exist. Unminimizes. |
| `window_move(title_or_handle, x, y)` | Move window to coordinates. |
| `window_resize(title_or_handle, width, height)` | Resize window. |
| `window_maximize(title_or_handle)` | Maximize window. |
| `window_minimize(title_or_handle)` | Minimize window. |
| `window_close(title_or_handle)` | Close window gracefully. |
| `window_screenshot(title_or_handle)` | Screenshot a specific window. |
| `window_enumerate_controls(title_or_handle, control_type, depth)` | List all interactive controls with type, text, rect, AutomationId, enabled/visible state. Win32 accessibility snapshot. |

### Pixel Color

| Tool | Description |
|------|-------------|
| `pixel_color(x, y)` | Get RGB and hex at a coordinate. |
| `pixel_color_region(x, y, w, h)` | Average color of a region. |

### Screen Change Detection

| Tool | Description |
|------|-------------|
| `wait_for_change(region, timeout, interval, threshold)` | Block until a screen region changes visually. |
| `screen_diff(region, baseline_path, describe)` | Compare screen against saved baseline. Optionally send diff to vision model. |

### OCR (three-tier)

| Tool | Description |
|------|-------------|
| `screen_ocr(region, question)` | Extract text: remote RapidOCR → local ONNX → vision model. Structured output from local tier includes bounding boxes. |
| `find_on_screen_text(text, region, monitor, case_sensitive)` | Find a text string on screen via OCR. Returns match positions, line numbers, context. |

### Clipboard

| Tool | Description |
|------|-------------|
| `clipboard_get_image()` | Copy image from clipboard to `data/images/`. |
| `clipboard_set_image(path)` | Copy image file to clipboard. |
| `clipboard_get_text()` | Get text content from system clipboard. |
| `clipboard_set_text(text)` | Copy text to system clipboard. |

### Accessibility (UIA)

| Tool | Description |
|------|-------------|
| `accessibility_tree(title_or_handle, depth)` | Get UI Automation tree. Names, types, AutomationIds, rects. |
| `click_by_automation_id(automation_id, title_or_handle, action)` | Click element by AutomationId. Actions: `click`, `double_click`, `right_click`, `invoke`. |
| `ui_find(name, control_type, title_or_handle, depth)` | Find elements by name substring and/or control type. No AutomationId needed. Returns matched elements with rects. |
| `ui_get_value(automation_id, title_or_handle)` | Read current value/text of a UI element. Useful for text fields, checkboxes, dropdowns. |
| `ui_wait(automation_id, timeout, title_or_handle)` | Wait for a UI element to appear. Polls UIA tree until found or timeout. |

### System

| Tool | Description |
|------|-------------|
| `shell_run(command, timeout, cwd)` | Run shell command, return output. |
| `launch_app(name, args)` | Launch application by name. |
| `process_list(name_filter)` | List running processes. |
| `process_kill(pid, force)` | Kill process by PID. |
| `file_read(path, lines)` | Read a file. |
| `file_write(path, content)` | Write a file. Creates parent dirs. |
| `file_list(path, pattern)` | List files in a directory. |
| `file_exists(path)` | Check if file/directory exists. |
| `action_trace(clear, last_n)` | Action trace log — every tool call recorded for crash diagnosis. Persist to disk via `TRACE_LOG_PATH`. |

## Multi-Monitor

Uses `ImageGrab.grab(all_screens=True)` for multi-monitor setups. `monitor=1` or `monitor=2` for specific display, `monitor=0` (default) for all.

## Template Matching Methods

### Multi-Scale Template Matching (`method="template"`)

Brute-force pixel correlation at 7 scales `[0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25]`.
Results are NMS-deduplicated to prevent overlapping detections.

### ORB Feature Matching (`method="feature"`)

ORB keypoints → BFMatcher + Lowe ratio test (0.75) → findHomography(RANSAC) →
perspectiveTransform for bounding box. Resilient to rotation, scale, partial occlusion.

### Auto Mode (`method="auto"`, default)

ORB first, fall back to template matching. Best of both worlds.

## OCR Pipeline

Three-tier fallback chain:

1. **Remote RapidOCR** (HTTP) — fastest, <100ms, requires OCR_SERVICE_URL host
2. **Local ONNX** (`rapidocr-onnxruntime`) — no network needed, returns structured bounding boxes
3. **Vision model** (Ollama/NVIDIA NIM) — slowest, best for Q&A about screen content

## UIA Workflow for Native Apps

For form-fill or data-extraction over native Windows apps:

1. `window_enumerate_controls(title)` — see all controls with their IDs and rects
2. `ui_find(name="Username", control_type="Edit")` — find elements without knowing IDs
3. `ui_get_value(automation_id)` — read current text/value
4. `click_by_automation_id(automation_id, action="invoke")` — click buttons
5. `ui_wait(automation_id, timeout=5)` — wait for elements to appear
6. `accessibility_tree(title, depth=3)` — full tree for complex navigation
