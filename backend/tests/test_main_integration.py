import io
import asyncio

import pytest
from fastapi import UploadFile

import main


class FakeDatabase:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True


class FakeVLM:
    pipeline_version = "v1.5"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run(self, image_path, output_dir=None, quality_result=None):
        self.calls += 1
        return self.result


def _quality(quality="GOOD"):
    return {
        "quality": quality,
        "blur_score": 100.0,
        "brightness": 120.0,
        "contrast": 40.0,
        "message": "quality",
        "width": 800,
        "height": 600,
    }


def _ocr():
    return {
        "words": [],
        "full_text": "",
        "engine": "tesseract",
        "success": True,
        "error": None,
        "source_image": "uploaded.jpg",
    }


def _extraction(value=None):
    empty = {"value": None, "confidence": "LOW", "evidence_text": None}
    result = {key: dict(empty) for key in (
        "product_name", "mrp", "net_quantity", "manufacturer",
        "manufacturing_date", "consumer_care", "country_of_origin",
        "best_before", "unit_sale_price",
    )}
    if value:
        result["product_name"] = {"value": value, "confidence": "HIGH", "evidence_text": value}
    return result


def _compliance():
    return {
        "overall_status": "NEEDS REVIEW",
        "overall_confidence": "LOW",
        "evaluations": [],
        "summary": "review",
        "field_statuses": {},
    }


async def _run(monkeypatch, vlm_result, quality="GOOD", enabled="1", extraction=None, ocr_result=None):
    monkeypatch.setenv("SMARTLM_ENABLE_VLM", enabled)
    monkeypatch.setattr(main, "UPLOAD_DIR", str(monkeypatch.tmpdir))
    monkeypatch.setattr(main, "load_and_preprocess", lambda path: {**_quality(quality), "_preprocessed_bgr": None})
    ocr_result = ocr_result or _ocr()
    monkeypatch.setattr(main, "run_ocr", lambda path: ocr_result)
    extraction = extraction or _extraction()
    monkeypatch.setattr(main, "extract_all", lambda result: extraction)
    compliance = _compliance()
    compliance_calls = []

    def fake_compliance(extracted, ocr_result, image_quality, inspection_context=None):
        compliance_calls.append((extracted, ocr_result, image_quality))
        return compliance

    monkeypatch.setattr(main, "run_compliance", fake_compliance)
    fake_vlm = FakeVLM(vlm_result)
    monkeypatch.setattr(main, "_vlm_engine", fake_vlm)
    db = FakeDatabase()
    file = UploadFile(filename="package.jpg", file=io.BytesIO(b"image bytes"))
    response = await main.analyze_image(file=file, db=db)
    return response, db, fake_vlm, compliance_calls


def test_analysis_vlm_disabled_returns_backward_compatible_sections(monkeypatch, tmp_path):
    monkeypatch.tmpdir = tmp_path
    response, db, vlm, compliance_calls = asyncio.run(_run(monkeypatch, {}, enabled="0"))
    assert response.vlm.status == "disabled"
    assert response.hybrid_extraction["mrp"].status == "NOT_DETECTED"
    assert vlm.calls == 0
    assert compliance_calls[0][0] is not None
    assert db.committed is True


def test_analysis_vlm_enabled_returns_hybrid_without_changing_compliance(monkeypatch, tmp_path):
    monkeypatch.tmpdir = tmp_path
    vlm_result = {
        "status": "success",
        "engine": "paddleocr-vl",
        "pipeline_version": "v1.5",
        "quality_status": "READABLE",
        "fields": {"product_name": {"value": "Visual Product", "evidence_text": "Visual Product"}},
    }
    response, db, vlm, compliance_calls = asyncio.run(_run(monkeypatch, vlm_result))
    assert response.vlm.status == "success"
    assert response.hybrid_extraction["product_name"].status == "VLM_ONLY"
    assert vlm.calls == 1
    assert compliance_calls[0][0]["product_name"]["value"] == "Visual Product"
    assert compliance_calls[0][0]["product_name"]["confidence"] == "LOW"
    stored_inspection = next(item for item in db.added if isinstance(item, main.Inspection))
    assert stored_inspection.product_name == "Visual Product"


def test_poor_quality_preserves_not_verifiable_hybrid_status(monkeypatch, tmp_path):
    monkeypatch.tmpdir = tmp_path
    vlm_result = {
        "status": "success",
        "engine": "paddleocr-vl",
        "pipeline_version": "v1.5",
        "quality_status": "NOT_VERIFIABLE",
        "fields": {"mrp": {"value": None}},
    }
    response, _, _, compliance_calls = asyncio.run(_run(monkeypatch, vlm_result, quality="POOR"))
    assert response.hybrid_extraction["mrp"].status == "NOT_VERIFIABLE"
    assert response.hybrid_extraction["mrp"].needs_review is True
    assert compliance_calls[0][2] == "POOR"
    assert response.compliance.overall_status == "NEEDS REVIEW"


def test_good_quality_does_not_run_vlm_by_default(monkeypatch, tmp_path):
    monkeypatch.tmpdir = tmp_path
    vlm_result = {
        "status": "success",
        "engine": "paddleocr-vl",
        "pipeline_version": "v1.5",
        "quality_status": "READABLE",
        "fields": {"product_name": {"value": "Visual Product"}},
    }
    def fake_ocr(path):
        payload = _ocr()
        payload["full_text"] = "mrp rs 120 net quantity 500g manufacturer acme best before 2026"
        return payload

    response, db, vlm, _ = asyncio.run(_run(monkeypatch, vlm_result, quality="GOOD", ocr_result=fake_ocr("unused")))
    assert response.vlm.status == "skipped"
    assert vlm.calls == 0
    assert db.committed is True
