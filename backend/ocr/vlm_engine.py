"""Optional PaddleOCR-VL document-understanding adapter."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from services.image_processing import quality_gate
from services.text_normalization import currency_amount, normalize_currency_text

logger = logging.getLogger(__name__)

_PADDLEOCR_VL = None
_IMPORT_ERROR: Optional[str] = None
try:
    from paddleocr import PaddleOCRVL as _PADDLEOCR_VL
except Exception as exc:
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


FIELD_NAMES = (
    "product_name",
    "mrp",
    "net_quantity",
    "manufacturer_packer_importer",
    "country_of_origin",
    "date_of_manufacture_or_packing",
    "best_before_or_use_by",
    "consumer_care",
    "unit_sale_price",
    "dimensions",
    "other_visible_mandatory_declarations",
)


def _empty_field(source_image: str) -> Dict[str, Any]:
    return {
        "value": None,
        "confidence": None,
        "evidence_text": None,
        "source_image": source_image,
        "bounding_box": None,
        "status": "NOT_DETECTED",
    }


def _field(value: Optional[str], block: Optional[Dict[str, Any]], source_image: str) -> Dict[str, Any]:
    if not value:
        return _empty_field(source_image)
    return {
        "value": value.strip(),
        "confidence": None,
        "evidence_text": (block or {}).get("block_content"),
        "source_image": source_image,
        "bounding_box": (block or {}).get("block_bbox"),
        "status": "DETECTED",
    }


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n:;,-")


def _find_block(blocks: Iterable[Dict[str, Any]], pattern: str) -> Optional[Dict[str, Any]]:
    compiled = re.compile(pattern, re.IGNORECASE)
    return next(
        (
            block for block in blocks
            if compiled.search(str(block.get("block_content", "")))
            or compiled.search(str(block.get("block_label", "")))
        ),
        None,
    )


def _match_value(block: Optional[Dict[str, Any]], pattern: str) -> Optional[str]:
    if not block:
        return None
    match = re.search(pattern, str(block.get("block_content", "")), re.IGNORECASE | re.DOTALL)
    return _clean(match.group(1)) if match else None


def _parse_blocks(blocks: List[Dict[str, Any]], source_image: str) -> Dict[str, Any]:
    fields = {name: _empty_field(source_image) for name in FIELD_NAMES}
    mrp_block = _find_block(blocks, r"\b(?:MRP|MAXIMUM\s+RETAIL\s+PRICE)\b")
    quantity_block = _find_block(blocks, r"\bNET\s*(?:QTY|QUANTITY|WEIGHT|WT|VOLUME|VOL)\b")
    manufacturer_block = _find_block(blocks, r"\b(?:MANUFACTURED|PACKED|MFG\.?\s*(?:BY|&\s*MKTD\.?\s*BY|&)|MANUFACTURER|PACKER|IMPORTED)\b")
    date_block = _find_block(blocks, r"\b(?:MFD|MFG\.?\s*DATE|MANUFACTURED|PACKED\s+ON)\b")
    best_before_block = _find_block(blocks, r"\b(?:BEST\s+BEFORE|USE\s+BY|EXP(?:IRY|\.?\s*DATE))\b")
    care_block = _find_block(blocks, r"\b(?:CONSUMER|CUSTOMER|CARE|HELPLINE|FEEDBACK|COMPLAINT)\b")
    unit_price_block = _find_block(blocks, r"\bUNIT\s+SALE\s+PRICE\b|\bPRICE\s+PER\b")
    country_block = _find_block(blocks, r"\b(?:COUNTRY\s+OF\s+ORIGIN|MADE\s+IN|PRODUCT\s+OF)\b")
    product_block = _find_block(blocks, r"\b(?:PROPRIETARY\s+FOOD|PRODUCT\s+NAME|PRODUCT|DOC_TITLE|PARAGRAPH_TITLE)\b")
    dimensions_block = _find_block(blocks, r"\b(?:DIMENSIONS?|SIZE)\b")

    fields["mrp"] = _field(
        currency_amount(_match_value(mrp_block, r"(?:MRP|MAXIMUM\s+RETAIL\s+PRICE)(.*)") or ""),
        mrp_block,
        source_image,
    )
    fields["net_quantity"] = _field(
        _match_value(quantity_block, r"(?:NET\s*(?:QTY|QUANTITY|WEIGHT|WT|VOLUME|VOL)).*?([0-9]+(?:\.[0-9]+)?\s*(?:mg|g|gm|kg|ml|l|litre|liter|pieces?|pcs|units?))"),
        quantity_block,
        source_image,
    )
    fields["manufacturer_packer_importer"] = _field(
        _match_value(manufacturer_block, r"(?:MANUFACTURED\s*(?:&\s*PACKED)?\s*BY|PACKED\s*BY|MFG\.?\s*(?:BY|&\s*MKTD\.?\s*BY|&)|MANUFACTURER|PACKER|IMPORTED\s*BY)\s*:?\s*(.*)"),
        manufacturer_block,
        source_image,
    )
    fields["country_of_origin"] = _field(
        _match_value(country_block, r"(?:COUNTRY\s+OF\s+ORIGIN|MADE\s+IN|PRODUCT\s+OF)\s*:?\s*([A-Za-z][A-Za-z -]{1,80})"),
        country_block,
        source_image,
    )
    fields["date_of_manufacture_or_packing"] = _field(
        _match_value(date_block, r"(?:MFD|MFG\.?\s*DATE|MANUFACTURED|PACKED\s+ON)\s*:?\s*((?:\d{1,2}[/-]){1,2}\d{2,4}|[A-Za-z]{3,9}\s+\d{4})"),
        date_block,
        source_image,
    )
    fields["best_before_or_use_by"] = _field(
        _match_value(best_before_block, r"(?:BEST\s+BEFORE|USE\s+BY|EXP(?:IRY|\.?\s*DATE))\s*:?\s*(.*)"),
        best_before_block,
        source_image,
    )
    fields["consumer_care"] = _field(care_block.get("block_content") if care_block else None, care_block, source_image)
    fields["unit_sale_price"] = _field(
        _match_value(unit_price_block, r"(?:UNIT\s+SALE\s+PRICE|PRICE\s+PER).*?((?:₹|RS\.?|INR)?\s*[0-9]+(?:\.[0-9]{1,2})?\s*(?:PER\s+)?[A-Za-z]+)"),
        unit_price_block,
        source_image,
    )
    fields["dimensions"] = _field(
        _match_value(dimensions_block, r"(?:DIMENSIONS?|SIZE)\s*:?\s*(.*)"),
        dimensions_block,
        source_image,
    )
    product_value = _match_value(product_block, r"(?:PROPRIETARY\s+FOOD|PRODUCT\s+NAME|PRODUCT)\s*:?\s*([^\n]+)")
    if not product_value and product_block and str(product_block.get("block_label", "")).lower() in {"doc_title", "paragraph_title"}:
        product_value = _clean(str(product_block.get("block_content", "")))
    fields["product_name"] = _field(
        product_value,
        product_block,
        source_image,
    )

    known_blocks = {id(block) for block in (mrp_block, quantity_block, manufacturer_block, date_block, best_before_block, care_block, unit_price_block, country_block, product_block, dimensions_block) if block}
    other = [str(block.get("block_content", "")).strip() for block in blocks if id(block) not in known_blocks and block.get("block_content")]
    fields["other_visible_mandatory_declarations"] = _field("\n".join(other) if other else None, None, source_image)
    if other:
        fields["other_visible_mandatory_declarations"]["evidence_text"] = "\n".join(other)

    return fields


class PaddleOCRVLEngine:
    """Lazy, optional PaddleOCR-VL runner that never decides compliance."""

    def __init__(self, pipeline_version: Optional[str] = None):
        self.pipeline_version = pipeline_version or os.getenv("SMARTLM_VLM_PIPELINE_VERSION", "v1.6")
        self._pipeline = None

    @staticmethod
    def availability(probe_model: bool = False) -> Dict[str, Any]:
        configured = os.getenv("SMARTLM_PADDLE_PYTHON")
        default = Path(__file__).resolve().parents[3] / ".paddleocr-venv" / "Scripts" / "python.exe"
        worker_available = Path(configured).is_file() if configured else default.is_file()
        result = {
            "available": _PADDLEOCR_VL is not None or worker_available,
            "engine": "paddleocr-vl" if _PADDLEOCR_VL is not None else "paddleocr-vl-external" if worker_available else "none",
            "pipeline_version": os.getenv("SMARTLM_VLM_PIPELINE_VERSION", "v1.6"),
            "error": _IMPORT_ERROR,
            "external_worker_available": worker_available,
            "python_executable": sys.executable,
            "configured_worker_python": str(configured or default),
            "import_available": _PADDLEOCR_VL is not None,
            "model_initialized": False,
        }
        if probe_model and _PADDLEOCR_VL is not None:
            try:
                PaddleOCRVLEngine()._get_pipeline()
                result["model_initialized"] = True
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def diagnostics(self, probe_model: bool = True) -> Dict[str, Any]:
        result = self.availability(probe_model=False)
        if probe_model and _PADDLEOCR_VL is not None:
            try:
                self._get_pipeline()
                result["model_initialized"] = True
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
        raw_setting = os.getenv("SMARTLM_ENABLE_VLM")
        normalized = (raw_setting or "0").strip().lower()
        result.update({
            "vlm_enabled": normalized not in {"0", "false", "no", "off"},
            "configured_value": raw_setting,
            "disable_reason": "SMARTLM_ENABLE_VLM explicitly disables VLM" if normalized in {"0", "false", "no", "off"} else None,
        })
        return result

    def _get_pipeline(self):
        if self._pipeline is None:
            if _PADDLEOCR_VL is None:
                raise RuntimeError(_IMPORT_ERROR or "PaddleOCR-VL is not installed")
            self._pipeline = _PADDLEOCR_VL(
                pipeline_version=self.pipeline_version,
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                use_layout_detection=True,
                use_chart_recognition=False,
                use_seal_recognition=False,
                use_ocr_for_image_block=True,
            )
        return self._pipeline

    def run(self, image_path: str, output_dir: Optional[str] = None, quality_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        path = Path(image_path)
        gate = quality_gate(quality_result) if quality_result is not None else {"status": "READABLE", "message": "Quality assessment not provided."}
        base = {"status": "failed", "engine": "paddleocr-vl", "pipeline_version": self.pipeline_version, "fields": {name: _empty_field(str(path)) for name in FIELD_NAMES}, "source_image": str(path), "error": None, "quality_status": gate["status"], "quality_message": gate["message"], "blocks": [], "markdown": ""}
        if not path.is_file():
            base["error"] = f"Image not found: {path}"
            return base
        try:
            if _PADDLEOCR_VL is None:
                return self._run_external_worker(path, output_dir, base)
            result = next(iter(self._get_pipeline().predict(str(path), use_doc_orientation_classify=True, use_doc_unwarping=True)))
            payload = result.json if isinstance(result.json, dict) else json.loads(result.json)
            data = payload.get("res", payload)
            blocks = data.get("parsing_res_list", [])
            base.update({
                "status": "success",
                "fields": _parse_blocks(blocks, str(path)),
                "markdown": (result.markdown or {}).get("markdown_texts", "") if isinstance(result.markdown, dict) else "",
                "blocks": blocks,
                "error": None,
            })
            if gate["status"] == "NOT_VERIFIABLE":
                for field in base["fields"].values():
                    if field["value"] is None:
                        field["status"] = "NOT_VERIFIABLE"
            if output_dir:
                output = Path(output_dir)
                output.mkdir(parents=True, exist_ok=True)
                result.save_to_json(str(output))
                result.save_to_img(str(output))
                result.save_to_markdown(str(output))
            return base
        except Exception as exc:
            logger.exception("PaddleOCR-VL failed for %s", path)
            base["error"] = f"{type(exc).__name__}: {exc}"
            return base

    def _run_external_worker(self, path: Path, output_dir: Optional[str], base: Dict[str, Any]) -> Dict[str, Any]:
        """Run PaddleOCR-VL from the designated OCR environment when needed."""
        configured = os.getenv("SMARTLM_PADDLE_PYTHON")
        default = Path(__file__).resolve().parents[3] / ".paddleocr-venv" / "Scripts" / "python.exe"
        python_executable = Path(configured) if configured else default
        worker = Path(__file__).with_name("vlm_worker.py")
        if not python_executable.is_file():
            base["error"] = f"PaddleOCR-VL unavailable and OCR Python was not found: {python_executable}"
            return base
        command = [str(python_executable), str(worker), str(path), self.pipeline_version]
        if output_dir:
            command.append(str(output_dir))
        try:
            worker_timeout = max(1.0, float(os.getenv("SMARTLM_VLM_TIMEOUT_SECONDS", "15")))
        except ValueError:
            worker_timeout = 15.0
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=worker_timeout, check=False)
        except subprocess.TimeoutExpired:
            base["error"] = f"PaddleOCR-VL worker timed out after {worker_timeout:.1f} seconds."
            return base
        if completed.returncode != 0:
            base["error"] = completed.stderr.strip() or completed.stdout.strip() or "PaddleOCR-VL worker failed."
            return base
        try:
            result = json.loads(completed.stdout)
            result.setdefault("quality_status", base["quality_status"])
            result.setdefault("quality_message", base["quality_message"])
            if result.get("quality_status") == "NOT_VERIFIABLE":
                for field in result.get("fields", {}).values():
                    if field.get("value") is None:
                        field["status"] = "NOT_VERIFIABLE"
            return result
        except json.JSONDecodeError as exc:
            base["error"] = f"Invalid PaddleOCR-VL worker response: {exc}"
            return base