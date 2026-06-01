# computer-use-mcp

MCP server for desktop automation on Windows. PyAutoGUI + OpenCV + Vision AI + Win32.

39 tools for mouse, keyboard, screenshots, screen understanding, window management,
pixel color, OCR, clipboard, template matching, UI element detection, and more.

## Setup

```bash
pip install mcp pyautogui opencv-contrib-python Pillow pywin32 psutil
```

> Use `opencv-contrib-python` (not `opencv-python`) for ORB feature matching.

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
| `computer_screenshot(monitor, region)` | Screenshot. `monitor=0` = all screens, `1+` = specific. Optional `region="x,y,w,h"` crop. |
| `analyze_screen(question, monitor)` | Send screenshot to vision AI (Ollama or NVIDIA NIM). Returns text description. |
| `get_monitors()` | List all connected monitors with bounds, resolution, and primary flag. |

### Template Matching

| Tool | Description |
|------|-------------|
| `save_template(name, x, y, width, height, monitor)` | Crop a screen region and save as a reusable template (stored in `data/templates/`). |
| `find_on_screen(template, threshold, multi_scale, monitor, method)` | Find a template on screen. Three methods: |
| | `method="auto"` (default) — ORB feature matching first, falls back to template matching |
| | `method="feature"` — ORB only. Best for rotation/scale changes. |
| | `method="template"` — Multi-scale pixel matching only. Original behavior. |
| `find_and_click_all(template, button, threshold, min_distance, multi_scale)` | Find all instances of a template and click each one. Clusters nearby matches to avoid duplicates. |

### UI Element Detection (template-free)

| Tool | Description |
|------|-------------|
| `find_elements(query, region, min_area, monitor)` | Find UI elements by color, shape, or both — no template needed. |
| `click_element(query, index, min_area, monitor)` | Find elements and click one at its center. |

**Query language for `find_elements`:**

Combines color and shape keywords. Examples:

| Query | What it finds |
|-------|--------------|
| `"blue buttons"` | Blue-colored rectangles |
| `"red circles"` | Red circles (close buttons, indicators) |
| `"close button"` | Red circles (close/X buttons) |
| `"yellow"` | All yellow-colored regions |
| `"icons"` | Any blob-shaped contours |
| `"Save button"` | Rectangle-shaped elements |

**Supported colors:** red, blue, green, yellow, orange, purple, white, gray, black
**Supported shapes:** rectangle (button, rect, square, box, bar), circle (round, dot, oval, ellipse), blob (icon, shape)

### Mouse

| Tool | Description |
|------|-------------|
| `computer_click(x, y, button, clicks)` | Click at coordinates. Button: `left`/`right`/`middle`. |
| `computer_move(x, y, duration)` | Move mouse without clicking. |
| `computer_scroll(amount, direction)` | Scroll at current position. |
| `computer_drag(x1, y1, x2, y2, duration)` | Drag from point A to B. |

### Keyboard

| Tool | Description |
|------|-------------|
| `computer_type(text, interval)` | Type text character-by-character. Use key combos: `ctrl+a`, `alt+tab`, `enter`, `escape`. |

### Window Management (Win32)

| Tool | Description |
|------|-------------|
| `window_list(title_filter, visible_only)` | Enumerate open windows. Filter by title substring. |
| `window_focus(title_or_handle, bring_to_front)` | Bring window to foreground. Unminimizes if needed. |
| `window_move(title_or_handle, x, y)` | Move window to coordinates. |
| `window_resize(title_or_handle, width, height)` | Resize window. |
| `window_maximize(title_or_handle)` | Maximize window. |
| `window_minimize(title_or_handle)` | Minimize window. |
| `window_close(title_or_handle)` | Close window gracefully. |
| `window_screenshot(title_or_handle)` | Screenshot a specific window. |

### Pixel Color

| Tool | Description |
|------|-------------|
| `pixel_color(x, y)` | Get RGB and hex at a coordinate. |
| `pixel_color_region(x, y, w, h)` | Average color of a region. |

### Screen Change Detection

| Tool | Description |
|------|-------------|
| `wait_for_change(region, timeout, interval, threshold)` | Block until a screen region changes visually. Polls pixel difference. |
| `screen_diff(region, baseline_path, describe)` | Compare screen against a saved baseline image. Optionally send diff to vision model. |

### OCR

| Tool | Description |
|------|-------------|
| `screen_ocr(region, question)` | Extract text via RapidOCR (fast, <100ms). Falls back to vision model for Q&A. |

### Clipboard

| Tool | Description |
|------|-------------|
| `clipboard_get_image()` | Copy image from system clipboard to `data/images/`. |
| `clipboard_set_image(path)` | Copy image file to system clipboard. |

### Accessibility (UIA)

| Tool | Description |
|------|-------------|
| `accessibility_tree(title_or_handle, depth)` | Get the UI Automation tree for a window. Shows element names, types, AutomationIds, bounding rects. |
| `click_by_automation_id(automation_id, title_or_handle, action)` | Click a UI element by its AutomationId. Works regardless of window position or DPI. |

### System

| Tool | Description |
|------|-------------|
| `shell_run(command, timeout, cwd)` | Run a shell command and return output. |
| `launch_app(name, args)` | Launch an application by name. |
| `process_list(name_filter)` | List running processes. |
| `process_kill(pid, force)` | Kill a process by PID. |
| `file_read(path, lines)` | Read a file. |
| `file_write(path, content)` | Write a file. |
| `file_list(path, pattern)` | List files in a directory. |
| `file_exists(path)` | Check if a file/directory exists. |
| `action_trace(clear, last_n)` | Get the action trace log (records every tool call for crash diagnosis). |

## Multi-Monitor

Uses `ImageGrab.grab(all_screens=True)` to support multi-monitor setups. Use `monitor=1` or `monitor=2` to target a specific display, or `monitor=0` (default) for all screens.

## Template Matching Methods

### Multi-Scale Template Matching (`method="template"`)

Brute-force pixel correlation at 7 scales `[0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25]`.
Fast for exact matches but breaks on rotation and large scale differences.

### ORB Feature Matching (`method="feature"`)

Detects ORB keypoints, matches with BFMatcher + Lowe ratio test (0.75), then uses
`findHomography(RANSAC)` to project template corners onto the screen. Resilient to
rotation, scale changes, and partial occlusion. Confidence = inlier ratio.

### Auto Mode (`method="auto"`, default)

Tries ORB feature matching first. If it fails (not enough keypoints, low confidence),
falls back to multi-scale template matching. Best of both worlds.

## How It Works: find_elements

Template-free element detection in three steps:

1. **Parse** — Extract color and shape keywords from the query string
2. **Detect** — Apply the appropriate OpenCV pipeline:
   - **Color only**: HSV color masking → contour detection → area filter
   - **Shape only**: Canny edge detection → contour detection → shape classification (approxPolyDP for rectangles, circularity > 0.85 for circles)
   - **Color + shape**: HSV mask → contours → shape filter on masked regions
   - **Default**: Edge detection → all significant contours
3. **Deduplicate** — IoU-based non-max suppression removes overlapping detections

Returns bounding boxes with center coordinates for clicking.
