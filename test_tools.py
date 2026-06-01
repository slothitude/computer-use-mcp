"""Test computer-use MCP tools on Rog."""
import sys, json, time
sys.path.insert(0, r"C:\Users\aaron\computer-use-mcp")

from computer_use_mcp import (
    computer_screenshot, analyze_screen, computer_click, computer_move,
    computer_scroll, computer_drag, computer_type, save_template, find_on_screen,
    get_monitors, window_list, window_focus, pixel_color, pixel_color_region,
    wait_for_change, screen_diff, screen_ocr, clipboard_set_image, clipboard_get_image,
    window_screenshot, window_move, window_resize, window_maximize, window_minimize,
    window_close, find_and_click_all,
)

results = []

def run(name, fn, *args, **kwargs):
    try:
        t = time.time()
        r = fn(*args, **kwargs)
        elapsed = time.time() - t
        s = json.dumps(r)
        results.append(f"PASS {name} ({elapsed:.2f}s): {s[:150]}")
    except Exception as e:
        results.append(f"FAIL {name}: {e}")

# ── Monitor ──
run("get_monitors", get_monitors)

# ── Screenshot (primary, full) ──
run("screenshot primary", computer_screenshot)

# ── Mouse ──
run("move", computer_move, 500, 500)
run("click", computer_click, 500, 500)
run("type text", computer_type, "test")
run("type hotkey", computer_type, "ctrl+a")
run("scroll", computer_scroll, 3, "down")
run("drag", computer_drag, 100, 100, 200, 200)

# ── Template ──
run("save_template", save_template, "test_mcp", 0, 0, 100, 50)
run("find_on_screen", find_on_screen, "test_mcp", 0.7)
run("find_on_screen multi_scale", find_on_screen, "test_mcp", 0.7, True)
run("find_and_click_all", find_and_click_all, "test_mcp", threshold=0.7)

# ── Window management ──
run("window_list", window_list)
run("window_list filter", window_list, "explorer")

# ── Pixel color ──
run("pixel_color", pixel_color, 100, 100)
run("pixel_color_region", pixel_color_region, 100, 100, 20, 20)

# ── Screen diff (save baseline) ──
run("screen_diff baseline", screen_diff)

# ── OCR (skip if Ollama not running) ──
try:
    run("screen_ocr", screen_ocr, "0,0,400,100")
except Exception:
    pass

# ── Vision (long, skip if Ollama not running) ──
try:
    run("analyze_screen", analyze_screen, "Describe in 1 sentence")
except Exception:
    pass

# ── Window screenshot ──
run("window_screenshot", window_screenshot, "explorer")

for line in results:
    print(line)
print(f"\n{sum(1 for r in results if r.startswith('PASS'))}/{len(results)} passed")
