"""
SMART-LM OCR Service
=====================
Primary: PaddleOCR
Fallback: pytesseract (Tesseract 5)

Returns a list of OCRWord dicts:
  { "text": str, "confidence": float, "bbox": [x1, y1, x2, y2] }

If both engines fail, returns a safe error dict — does NOT raise.
"""

import logging
import json
import os
import re
import shutil
import subprocess
from typing import List, Dict, Any, Optional
import numpy as np
import cv2

from services.image_processing import generate_preprocessed_variants

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine detection — import at module level so we know what is available
# ---------------------------------------------------------------------------

_PADDLE_AVAILABLE = False
_TESSERACT_AVAILABLE = False
_TESSERACT_CMD: Optional[str] = None
_pytesseract = None
_PILImage = None

try:
    from paddleocr import PaddleOCR as _PaddleOCR
    _PADDLE_AVAILABLE = True
    logger.info("PaddleOCR detected — will use as primary OCR engine.")
except ImportError:
    logger.warning("PaddleOCR not installed. Trying Tesseract fallback.")

try:
    import pytesseract as _pytesseract
    from PIL import Image as _PILImage
except ImportError:
    logger.error("pytesseract is not installed. OCR will not function until it is installed.")
    _pytesseract = None
    _PILImage = None


def _candidate_tesseract_paths() -> List[str]:
    candidates: List[str] = []
    env_value = os.getenv("TESSERACT_CMD")
    if env_value:
        candidates.append(env_value)

    path_value = os.getenv("PATH", "")
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        candidates.append(os.path.join(entry, "tesseract.exe"))
        candidates.append(os.path.join(entry, "tesseract"))

    candidates.extend([
        shutil.which("tesseract"),
        shutil.which("tesseract.exe"),
    ])

    if os.name == 'nt':
        candidates.extend([
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
            os.path.expandvars(r'%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe'),
            os.path.expandvars(r'%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe'),
        ])

    unique = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = os.path.normpath(candidate)
        if normalized not in seen:
            unique.append(normalized)
            seen.add(normalized)
    return unique


def _apply_tesseract_path() -> Optional[str]:
    global _TESSERACT_AVAILABLE, _TESSERACT_CMD
    if _pytesseract is None:
        _TESSERACT_AVAILABLE = False
        return None

    for candidate in _candidate_tesseract_paths():
        if os.path.exists(candidate):
            _TESSERACT_CMD = candidate
            _pytesseract.pytesseract.tesseract_cmd = candidate
            if os.path.dirname(candidate) not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + os.path.dirname(candidate)
            _TESSERACT_AVAILABLE = True
            return candidate

    _TESSERACT_AVAILABLE = False
    return None


def detect_tesseract() -> Dict[str, Any]:
    info = {
        "available": False,
        "engine": "none",
        "path": None,
        "version": None,
        "message": "Tesseract is not installed or it's not in your PATH. Set TESSERACT_CMD or install Tesseract OCR.",
    }

    path = _apply_tesseract_path()
    if path:
        info["available"] = True
        info["engine"] = "tesseract"
        info["path"] = path
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "Tesseract installed"
                info["version"] = version_line
                info["message"] = "Tesseract is installed and ready."
            else:
                info["message"] = "Tesseract binary was detected but failed to respond to --version."
        except Exception as exc:
            info["message"] = f"Tesseract found but version check failed: {exc}"
    return info


def get_ocr_health() -> Dict[str, Any]:
    tesseract_info = detect_tesseract()
    available = _PADDLE_AVAILABLE or tesseract_info["available"]
    return {
        "available": available,
        "engine": "paddleocr" if _PADDLE_AVAILABLE else "tesseract" if tesseract_info["available"] else "none",
        "tesseract": tesseract_info,
        "paddleocr": {
            "available": _PADDLE_AVAILABLE,
            "engine": "paddleocr" if _PADDLE_AVAILABLE else "none",
        },
        "message": "OCR is ready." if available else "OCR is unavailable. Install Tesseract or PaddleOCR.",
    }

# Lazy-initialised PaddleOCR instance (avoids slow model load at import time)
_paddle_instance: Optional[Any] = None


def _get_paddle() -> Any:
    global _paddle_instance
    if _paddle_instance is None:
        _paddle_instance = _PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _paddle_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_ocr(image_path: str) -> Dict[str, Any]:
    """
    Run OCR on the given image path.

    Returns dict matching OCRResult schema:
    {
        "words": [ {"text", "confidence", "bbox"}, ... ],
        "full_text": str,
        "engine": "paddleocr" | "tesseract" | "none",
        "success": bool,
        "error": str | None,
        "source_image": str
    }
    """
    if _PADDLE_AVAILABLE:
        return _run_paddle(image_path)
    if _apply_tesseract_path() or _TESSERACT_AVAILABLE:
        return _run_tesseract(image_path)
    return _no_engine_result()


# ---------------------------------------------------------------------------
# PaddleOCR implementation
# ---------------------------------------------------------------------------

def _run_paddle(image_path: str) -> Dict[str, Any]:
    try:
        ocr = _get_paddle()
        result = next(iter(ocr.predict(
            image_path,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )), None)

        words: List[Dict[str, Any]] = []

        if result is None:
            return {
                "words": [],
                "full_text": "",
                "engine": "paddleocr",
                "success": True,
                "error": None,
            }

        payload = result.json if isinstance(result.json, dict) else json.loads(result.json)
        texts = payload.get("rec_texts") or []
        scores = payload.get("rec_scores") or []
        boxes = payload.get("rec_boxes") or payload.get("rec_polys") or []
        for index, text in enumerate(texts):
            if not str(text).strip():
                continue
            points = boxes[index] if index < len(boxes) else []
            if points and len(points) >= 4 and isinstance(points[0], (list, tuple)):
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            elif len(points) >= 4:
                bbox = [float(value) for value in points[:4]]
            else:
                bbox = [0.0, 0.0, 0.0, 0.0]
            confidence = float(scores[index]) if index < len(scores) else 0.0
            words.append({
                "text": str(text).strip(),
                "confidence": round(confidence, 4),
                "bbox": [round(value, 1) for value in bbox],
            })

        full_text = " ".join(w["text"] for w in words)
        return {
            "words": words,
            "full_text": full_text,
            "engine": "paddleocr",
            "success": True,
            "error": None,
        }

    except Exception as exc:
        logger.error("PaddleOCR failed: %s", exc)
        # Try tesseract as secondary fallback
        if _apply_tesseract_path() or _TESSERACT_AVAILABLE:
            logger.info("Attempting Tesseract fallback after PaddleOCR failure.")
            result = _run_tesseract(image_path)
            result["engine"] = "tesseract-fallback"
            return result
        return {
            "words": [],
            "full_text": "",
            "engine": "paddleocr",
            "success": False,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Tesseract implementation
# ---------------------------------------------------------------------------

def _parse_tesseract_words(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    words: List[Dict[str, Any]] = []
    if not data or not data.get("text"):
        return words

    for i, text in enumerate(data["text"]):
        token = str(text).strip()
        if not token:
            continue
        try:
            conf = int(str(data.get("conf", [0] * len(data.get("text", [])))[i]))
        except (TypeError, ValueError, IndexError):
            conf = -1
        if conf < 0:
            continue

        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])
        words.append({
            "text": token,
            "confidence": round(max(0.0, min(conf, 100)) / 100.0, 4),
            "bbox": [float(x), float(y), float(x + w), float(y + h)],
        })
    return words


def _net_quantity_signal(words: List[Dict[str, Any]]) -> int:
    """Score OCR variants for explicit, locally supported net quantities."""
    label_pattern = re.compile(r'^(?:net|qty|quantity|wt|weight|vol|volume|quant1ty|we1ght)$', re.I)
    value_pattern = re.compile(r'^\d+(?:[.,]\d+)?\s*(?:mg|g|gm|gram|grams|kg|ml|l|litre|liter|pieces?|pcs|units?)$', re.I)
    number_pattern = re.compile(r'^\d+(?:[.,]\d+)?$', re.I)
    unit_pattern = re.compile(r'^(?:mg|g|gm|gram|grams|kg|ml|l|litre|liter|pieces?|pcs|units?)$', re.I)
    normalized = [str(word.get('text', '')).strip() for word in words]
    best = 0
    for index, token in enumerate(normalized):
        if not label_pattern.fullmatch(token):
            continue
        label_end = index
        if token.lower() == 'net' and index + 1 < len(normalized):
            label_end = index + 1
        score = 2
        for candidate_index in range(label_end + 1, min(len(normalized), label_end + 7)):
            candidate = normalized[candidate_index]
            if value_pattern.fullmatch(candidate):
                score = max(score, 5)
                break
            if number_pattern.fullmatch(candidate) and candidate_index + 1 < len(normalized) and unit_pattern.fullmatch(normalized[candidate_index + 1]):
                score = max(score, 5)
                break
        best = max(best, score)
    return best


def _run_tesseract(image_path: str) -> Dict[str, Any]:
    try:
        if not _pytesseract:
            raise RuntimeError("pytesseract is not installed.")
        if not _apply_tesseract_path() and not _TESSERACT_AVAILABLE:
            raise RuntimeError("Tesseract is not installed or it's not in your PATH. Set TESSERACT_CMD or install Tesseract OCR.")

        img = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"Image not found or unreadable: {image_path}")

        candidates = generate_preprocessed_variants(img)
        if not candidates:
            candidates = [{"name": "original", "image": img}]

        best_words: List[Dict[str, Any]] = []
        best_full_text = ""
        best_conf = -1.0
        best_field_score = -1
        best_source_name = "original"
        engine_name = "tesseract"

        def evaluate_variant(variant_image: np.ndarray, variant_name: str):
            nonlocal best_words, best_full_text, best_conf, best_field_score, best_source_name
            rgb = cv2.cvtColor(variant_image, cv2.COLOR_BGR2RGB)
            pil_img = _PILImage.fromarray(rgb)
            for config in ["--oem 3 --psm 6", "--oem 3 --psm 11"]:
                data = _pytesseract.image_to_data(
                    pil_img,
                    output_type=_pytesseract.Output.DICT,
                    config=config,
                    lang="eng",
                )
                candidate_words = _parse_tesseract_words(data)
                if not candidate_words:
                    continue
                candidate_text = " ".join(w["text"] for w in candidate_words)
                candidate_conf = float(np.mean([w["confidence"] for w in candidate_words])) if candidate_words else 0.0
                field_score = _net_quantity_signal(candidate_words)
                if (field_score, candidate_conf) > (best_field_score, best_conf):
                    best_words = candidate_words
                    best_full_text = candidate_text
                    best_conf = candidate_conf
                    best_field_score = field_score
                    best_source_name = f'{variant_name}:{config}'
                    logger.debug('OCR variant selected: %s field_score=%s confidence=%.3f', best_source_name, field_score, candidate_conf)

        for variant in candidates[:2]:
            evaluate_variant(variant["image"], variant["name"])

        if not best_words:
            enhanced_variants = generate_preprocessed_variants(img, force_enhanced=True)
            for variant in enhanced_variants[:2]:
                evaluate_variant(variant["image"], variant["name"])

        if not best_words:
            return {
                "words": [],
                "full_text": "",
                "engine": engine_name,
                "success": False,
                "error": "No text detected by Tesseract on the provided image.",
                "source_image": image_path,
            }

        return {
            "words": best_words,
            "full_text": best_full_text,
            "engine": engine_name,
            "success": True,
            "error": None,
            "source_image": image_path,
            "variant_used": best_source_name,
        }

    except Exception as exc:
        logger.error("Tesseract OCR failed: %s", exc)
        return {
            "words": [],
            "full_text": "",
            "engine": "tesseract",
            "success": False,
            "error": str(exc),
            "source_image": image_path,
        }


def _no_engine_result() -> Dict[str, Any]:
    return {
        "words": [],
        "full_text": "",
        "engine": "none",
        "success": False,
        "error": "Tesseract is not installed or it's not in your PATH. Set TESSERACT_CMD or install Tesseract OCR.",
        "source_image": None,
    }
