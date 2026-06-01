"""RapidOCR HTTP service — GPU-accelerated OCR on Lappy with idle VRAM unload.

POST /ocr  — JSON body: {"image": "<base64 encoded image>"}
         Returns: {"text": "extracted text", "lines": 150}

GET /health — health check, shows provider and idle state.

Unloads GPU models after IDLE_TIMEOUT seconds of no requests to free VRAM.
Re-initializes with CUDA on the next request.
"""

import os
import sys
import time
import threading
import weakref

# Prepend nvidia cuDNN bin dir to PATH so onnxruntime can find cudnn64_9.dll
_cudnn_bin = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "cudnn", "bin")
if os.path.isdir(_cudnn_bin):
    os.environ["PATH"] = _cudnn_bin + os.pathsep + os.environ.get("PATH", "")

import base64
import json

import cv2
import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

IDLE_TIMEOUT = 120  # seconds before unloading GPU models

app = FastAPI(title="RapidOCR Service")

# ── Managed OCR instance ────────────────────────────────────────────────────────

_ocr = None
_last_request = time.time()
_lock = threading.Lock()
_timer: threading.Timer | None = None


def _create_ocr():
    """Create a new RapidOCR instance with CUDA."""
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)


def _get_ocr():
    """Get or create the OCR instance (thread-safe)."""
    global _ocr, _timer
    with _lock:
        if _ocr is None:
            _ocr = _create_ocr()
        _last_request = time.time()
        _schedule_unload()
        return _ocr


def _schedule_unload():
    """Schedule unloading after IDLE_TIMEOUT seconds."""
    global _timer
    if _timer is not None:
        _timer.cancel()
    _timer = threading.Timer(IDLE_TIMEOUT, _unload_if_idle)
    _timer.daemon = True
    _timer.start()


def _unload_if_idle():
    """Unload OCR instance to free GPU VRAM if idle."""
    global _ocr, _timer
    with _lock:
        elapsed = time.time() - _last_request
        if elapsed >= IDLE_TIMEOUT:
            _ocr = None
            _timer = None
            import gc
            gc.collect()


# ── Routes ──────────────────────────────────────────────────────────────────────

class OCRRequest(BaseModel):
    image: str  # base64 encoded image


@app.post("/ocr")
def ocr_endpoint(req: OCRRequest):
    ocr = _get_ocr()

    img_bytes = base64.b64decode(req.image)
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "Failed to decode image"}

    t0 = time.time()
    results, _ = ocr(img)
    elapsed = round((time.time() - t0) * 1000)

    if not results:
        return {"text": "", "lines": 0, "elapsed_ms": elapsed}

    lines = [line[1] for line in results]
    return {"text": "\n".join(lines), "lines": len(lines), "elapsed_ms": elapsed}


@app.get("/health")
def health():
    with _lock:
        loaded = _ocr is not None
        idle_s = time.time() - _last_request if loaded else IDLE_TIMEOUT + 1
        providers = []
        if loaded:
            for attr in ["text_rec"]:
                obj = getattr(_ocr, attr, None)
                if obj and hasattr(obj, "session"):
                    p = obj.session.session.get_providers()
                    providers.append(f"{attr}={p[0]}")
    return {
        "status": "ok",
        "model": "RapidOCR ONNX (CUDA)",
        "loaded": loaded,
        "idle_seconds": round(idle_s, 1),
        "idle_timeout": IDLE_TIMEOUT,
        "providers": providers,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
