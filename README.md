# computer-use-mcp

Computer Use MCP Server: PyAutoGUI + OpenCV + Ollama vision + Win32 window management.

26 tools for desktop automation: mouse, keyboard, screenshots, screen understanding,
window management, pixel color, OCR, clipboard images, and template matching.

## Setup

```bash
pip install mcp pyautogui opencv-python Pillow pywin32
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPUTER_VISION_MODEL` | `qwen2.5vl:3b` | Ollama vision model for screen understanding |
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama API base URL |
| `VISION_TIMEOUT` | `300` | Vision API timeout (seconds) |
| `SCREEN_MAX_DIMENSION` | `1280` | Max dimension for vision screenshots |
| `COMPUTER_USE_DATA_DIR` | `data` | Directory for templates and screenshots |

## Tools

### Vision
- `computer_screenshot(monitor, region)` — Screenshot specific monitor or all screens
- `analyze_screen(question, monitor)` — Vision model description of screen

### Template Matching
- `save_template(name, x, y, width, height, monitor)` — Save screen region as template
- `find_on_screen(template, threshold, multi_scale, monitor)` — Find template with multi-scale support
- `find_and_click_all(template, button, threshold, min_distance, multi_scale)` — Click all matches

### Mouse
- `computer_click(x, y, button, clicks)` — Click at coordinates
- `computer_move(x, y, duration)` — Move mouse without clicking
- `computer_scroll(amount, direction)` — Scroll at current position
- `computer_drag(x1, y1, x2, y2, duration)` — Drag between points

### Keyboard
- `computer_type(text, interval)` — Type text or hotkeys (e.g. `ctrl+a`, `alt+tab`)

### Window Management (Win32)
- `window_list(title_filter, visible_only)` — Enumerate open windows
- `window_focus(title_or_handle)` — Bring window to foreground
- `window_move(title_or_handle, x, y)` — Move window
- `window_resize(title_or_handle, width, height)` — Resize window
- `window_maximize(title_or_handle)` — Maximize window
- `window_minimize(title_or_handle)` — Minimize window
- `window_close(title_or_handle)` — Close window gracefully
- `window_screenshot(title_or_handle)` — Capture specific window

### Monitor
- `get_monitors()` — List connected monitors with bounds

### Pixel Color
- `pixel_color(x, y)` — Get RGB + hex at coordinates
- `pixel_color_region(x, y, w, h)` — Average color of a region

### Screen Change Detection
- `wait_for_change(region, timeout, interval, threshold)` — Wait for visual change
- `screen_diff(region, baseline_path, describe)` — Compare against saved baseline

### OCR
- `screen_ocr(region, question)` — Extract text via Ollama vision

### Clipboard
- `clipboard_get_image()` — Get image from clipboard
- `clipboard_set_image(path)` — Copy image file to clipboard

## Multi-Monitor

The server uses `ImageGrab.grab(all_screens=True)` instead of `pyautogui.screenshot()`
to support dual-monitor setups (3840x1080 on Rog). Use `monitor=1` or `monitor=2` to
target a specific display, or `monitor=0` (default) for all screens.

## Multi-Scale Template Matching

`find_on_screen` supports multi-scale matching (default `True`). When the initial match
fails, it retries at scales `[0.75, 0.8, 0.9, 1.0, 1.1, 1.2, 1.25]` to handle DPI
differences, zoom levels, and window resizing.
