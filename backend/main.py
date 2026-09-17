"""
SMART-LM FastAPI Backend
=========================
Main application entry point.
Run with: uvicorn main:app --reload --port 8000
"""

import os
import uuid
import shutil
import logging
import asyncio
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

# Internal imports
from database.database import init_db, get_db, Inspection, Declaration, RuleEvaluationModel, Report
from models.schemas import (
    AnalysisResponse, InspectionSummary, InspectionDetail,
    DashboardStats, DeclarationOut, RuleEvaluationOut,
)
from services.image_processing import load_and_preprocess
from services.ocr import run_ocr, get_ocr_health
from services.extraction import extract_all
from services.hybrid_extraction import build_hybrid_extraction, build_compliance_extraction
from services.compliance import run_compliance
from ocr.vlm_engine import PaddleOCRVLEngine
from services.reports import generate_pdf, generate_docx
from routers import auth, admin, reviewer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("smart_lm")
_vlm_engine = PaddleOCRVLEngine()


def _should_run_vlm(quality_result: dict, ocr_result: dict) -> bool:
    """Run the expensive VLM layer only when OCR quality is poor or ambiguous."""
    quality = str(quality_result.get("quality", "GOOD") or "GOOD").upper()
    if quality in {"POOR", "DIFFICULT"}:
        logger.info("[SMART-LM] VLM trigger: image quality is %s", quality)
        return True

    if not ocr_result.get("success", False):
        logger.info("[SMART-LM] VLM trigger: primary OCR failed")
        return True

    text = (ocr_result.get("full_text") or "").lower()
    if len(text.strip()) < 20:
        logger.info("[SMART-LM] VLM trigger: OCR text is too short")
        return True

    required_signals = ["mrp", "net", "qty", "quantity", "manufact", "packed", "best before", "use by", "country", "origin"]
    present_count = sum(1 for signal in required_signals if signal in text)
    if present_count < 2:
        logger.info("[SMART-LM] VLM trigger: OCR has too few declaration cues; signals found=%s", present_count)
        return True

    return False


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORTS_DIR = os.path.join(BASE_DIR, "generated_reports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

MAX_UPLOAD_SIZE_MB = 15
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SMART-LM API",
    description="Smart Legal Metrology Compliance & Inspection System - SIH 2026 Prototype",
    version="1.0.0-prototype",
)

# CORS - allow frontend dev servers
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite default
        "http://localhost:3000",  # CRA default
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded images as static files
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(reviewer.router)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    logger.info("Initialising database...")
    init_db()
    db = next(get_db())
    try:
        auth.ensure_demo_users(db)
    finally:
        db.close()
    logger.info("SMART-LM backend ready.")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    ocr_health = get_ocr_health()
    return {
        "status": "ok",
        "service": "SMART-LM",
        "version": "1.0.0-prototype",
        "timestamp": datetime.utcnow().isoformat(),
        "ocr": ocr_health,
    }


@app.get("/api/ocr/health")
def ocr_health_endpoint():
    return get_ocr_health()


@app.get("/api/vlm/health")
def vlm_health_endpoint():
    return _vlm_engine.diagnostics(probe_model=False)


# ---------------------------------------------------------------------------
# Analysis endpoint (main pipeline)
# ---------------------------------------------------------------------------

@app.post("/api/analyze", response_model=AnalysisResponse, dependencies=[Depends(auth.get_current_user)])
async def analyze_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    origin: str = Form("UNKNOWN"),
    package_type: str = Form("UNKNOWN"),
    sales_channel: str = Form("UNKNOWN"),
):
    """
    Full analysis pipeline:
    Upload -> image quality -> OCR -> extraction -> compliance -> save to DB
    """
    # --- Validate file ---
    ext = os.path.splitext(file.filename or "image.jpg")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # --- Save uploaded file with unique name ---
    inspection_id = f"INSP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6].upper()}"
    safe_filename = f"{inspection_id}{ext}"
    image_path = os.path.join(UPLOAD_DIR, safe_filename)

    try:
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB} MB."
            )
        with open(image_path, "wb") as f:
            f.write(content)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("File save error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file.")

    # --- Image Quality ---
    logger.info("[%s] Running image quality check...", inspection_id)
    quality_result = load_and_preprocess(image_path)
    image_quality = quality_result["quality"]

    # --- OCR ---
    logger.info("[%s] Running OCR (quality=%s)...", inspection_id, image_quality)
    try:
        ocr_timeout = max(5.0, float(os.getenv("SMARTLM_OCR_TIMEOUT_SECONDS", "45")))
    except ValueError:
        ocr_timeout = 45.0
    try:
        ocr_result = await asyncio.wait_for(
            asyncio.to_thread(run_ocr, image_path),
            timeout=ocr_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("[%s] OCR exceeded %.1f seconds; continuing with fallback handling.", inspection_id, ocr_timeout)
        ocr_result = {
            "words": [],
            "full_text": "",
            "engine": "none",
            "success": False,
            "error": f"OCR timed out after {ocr_timeout:.1f} seconds.",
            "source_image": image_path,
        }

    vlm_setting = os.getenv("SMARTLM_ENABLE_VLM")
    vlm_enabled = (vlm_setting or "0").strip().lower() not in {"0", "false", "no", "off"}
    should_run_vlm = _should_run_vlm(quality_result, ocr_result)

    if not vlm_enabled:
        logger.info("[%s] VLM disabled by configuration; skipping fallback layer.", inspection_id)
        vlm_result = {
            "status": "disabled",
            "engine": "paddleocr-vl",
            "pipeline_version": _vlm_engine.pipeline_version,
            "fields": {},
            "source_image": image_path,
            "error": "VLM disabled by SMARTLM_ENABLE_VLM explicit configuration.",
            "quality_status": "NOT_VERIFIABLE" if image_quality == "POOR" else "READABLE",
            "quality_message": quality_result.get("message"),
            "blocks": [],
            "markdown": "",
        }
    elif should_run_vlm:
        logger.info("[%s] Running PaddleOCR-VL fallback for difficult/ambiguous OCR.", inspection_id)
        try:
            vlm_timeout = max(1.0, float(os.getenv("SMARTLM_VLM_TIMEOUT_SECONDS", "15")))
        except ValueError:
            vlm_timeout = 15.0
        try:
            vlm_result = await asyncio.wait_for(
                asyncio.to_thread(_vlm_engine.run, image_path, None, quality_result),
                timeout=vlm_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[%s] PaddleOCR-VL exceeded %.1f seconds; continuing with OCR results.", inspection_id, vlm_timeout)
            vlm_result = {
                "status": "failed",
                "engine": "paddleocr-vl",
                "pipeline_version": _vlm_engine.pipeline_version,
                "fields": {},
                "source_image": image_path,
                "error": f"VLM timed out after {vlm_timeout:.1f} seconds.",
                "quality_status": "NOT_VERIFIABLE" if image_quality == "POOR" else "READABLE",
                "quality_message": quality_result.get("message"),
                "blocks": [],
                "markdown": "",
            }
        if vlm_result.get("status") == "failed":
            logger.warning("[%s] PaddleOCR-VL failed: %s", inspection_id, vlm_result.get("error"))
    else:
        logger.info("[%s] Skipping VLM: OCR quality and signals are sufficient for the normal path.", inspection_id)
        vlm_result = {
            "status": "skipped",
            "engine": "paddleocr-vl",
            "pipeline_version": _vlm_engine.pipeline_version,
            "fields": {},
            "source_image": image_path,
            "error": None,
            "quality_status": "READABLE",
            "quality_message": quality_result.get("message"),
            "blocks": [],
            "markdown": "",
        }

    # --- Extraction ---
    logger.info("[%s] Extracting declarations...", inspection_id)
    extraction = extract_all(ocr_result)
    hybrid_extraction = build_hybrid_extraction(extraction, vlm_result)
    compliance_extraction = build_compliance_extraction(
        hybrid_extraction,
        image_quality,
        ocr_result.get("success", False),
    )

    # --- Inspection Context ---
    inspection_context = {
        "origin": (origin if isinstance(origin, str) else "UNKNOWN").upper(),
        "package_type": (package_type if isinstance(package_type, str) else "UNKNOWN").upper(),
        "sales_channel": (sales_channel if isinstance(sales_channel, str) else "UNKNOWN").upper(),
    }

    logger.info(
        "[%s] Inspection context: origin=%s package_type=%s sales_channel=%s",
        inspection_id,
        inspection_context["origin"],
        inspection_context["package_type"],
        inspection_context["sales_channel"],
    )

    # --- Compliance ---
    logger.info("[%s] Running compliance checks...", inspection_id)
    compliance = run_compliance(
        compliance_extraction,
        ocr_result,
        image_quality,
        inspection_context=inspection_context,
    )

    # --- Persist to database ---
    now = datetime.utcnow()
    product_name = compliance_extraction.get("product_name", {}).get("value")
    hybrid_product = hybrid_extraction.get("product_name", {})
    if not product_name and hybrid_product.get("status") == "VLM_ONLY":
        product_name = hybrid_product.get("final_value")

    db_inspection = Inspection(
        inspection_id=inspection_id,
        created_at=now,
        product_name=product_name,
        status=compliance["overall_status"],
        overall_confidence=compliance["overall_confidence"],
        image_path=image_path,
        image_quality=image_quality,
        ocr_text_length=len(ocr_result.get("full_text", "")),
        is_demo=False,
    )
    db.add(db_inspection)

    # Declarations
    field_map = {
        "product_name": "Product Name",
        "mrp": "MRP",
        "net_quantity": "Net Quantity",
        "manufacturer": "Manufacturer",
        "manufacturing_date": "Manufacturing Date",
        "consumer_care": "Consumer Care",
        "country_of_origin": "Country of Origin",
        "best_before": "Best Before",
        "unit_sale_price": "Unit Sale Price",
    }
    field_statuses = compliance.get("field_statuses", {})

    for field_key, field_label in field_map.items():
        field_data = compliance_extraction.get(field_key, {})
        # Find status for this field from compliance
        field_status = None
        for rule_id, status in field_statuses.items():
            if field_key.replace("_", "") in rule_id.lower().replace("-", "").replace("_", ""):
                field_status = status
                break

        db.add(Declaration(
            inspection_id=inspection_id,
            field_name=field_key,
            extracted_value=field_data.get("value"),
            confidence=field_data.get("confidence", "LOW"),
            status=field_status,
            evidence_text=field_data.get("evidence_text"),
        ))

    # Rule Evaluations
    for eval_item in compliance["evaluations"]:
        db.add(RuleEvaluationModel(
            inspection_id=inspection_id,
            requirement=eval_item["requirement"],
            extracted_value=eval_item.get("extracted_value"),
            expected_requirement=eval_item["expected_requirement"],
            rule_reference=eval_item["rule_reference"],
            evidence=eval_item.get("evidence"),
            confidence=eval_item.get("confidence", "LOW"),
            result=eval_item["result"],
        ))

    db.commit()
    logger.info("[%s] Saved to DB. Status=%s", inspection_id, compliance["overall_status"])

    # --- Build response ---
    from models.schemas import (
        ImageQualityResult, OCRResult, OCRWord, ExtractionResult,
        ExtractedField, ComplianceResult, RuleEvaluation, AnalysisResponse,
        VLMResult, VLMField, HybridField,
    )

    def _to_extracted_field(d: dict) -> ExtractedField:
        return ExtractedField(
            value=d.get("value"),
            confidence=d.get("confidence", "LOW"),
            evidence_text=d.get("evidence_text"),
        )

    response = AnalysisResponse(
        inspection_id=inspection_id,
        image_quality=ImageQualityResult(
            quality=quality_result["quality"],
            blur_score=quality_result["blur_score"],
            brightness=quality_result["brightness"],
            contrast=quality_result["contrast"],
            message=quality_result["message"],
            width=quality_result["width"],
            height=quality_result["height"],
        ),
        ocr=OCRResult(
            words=[OCRWord(**w) for w in ocr_result.get("words", [])],
            full_text=ocr_result.get("full_text", ""),
            engine=ocr_result.get("engine", "none"),
            success=ocr_result.get("success", False),
            error=ocr_result.get("error"),
        ),
        extraction=ExtractionResult(
            product_name=_to_extracted_field(extraction.get("product_name", {})),
            mrp=_to_extracted_field(extraction.get("mrp", {})),
            net_quantity=_to_extracted_field(extraction.get("net_quantity", {})),
            manufacturer=_to_extracted_field(extraction.get("manufacturer", {})),
            manufacturing_date=_to_extracted_field(extraction.get("manufacturing_date", {})),
            consumer_care=_to_extracted_field(extraction.get("consumer_care", {})),
            country_of_origin=_to_extracted_field(extraction.get("country_of_origin", {})),
            best_before=_to_extracted_field(extraction.get("best_before", {})),
            unit_sale_price=_to_extracted_field(extraction.get("unit_sale_price", {})),
        ),
        vlm=VLMResult(
            status=vlm_result.get("status", "failed"),
            engine=vlm_result.get("engine", "paddleocr-vl"),
            pipeline_version=vlm_result.get("pipeline_version"),
            fields={key: VLMField(**value) for key, value in vlm_result.get("fields", {}).items()},
            source_image=vlm_result.get("source_image"),
            error=vlm_result.get("error"),
            quality_status=vlm_result.get("quality_status", "READABLE"),
            quality_message=vlm_result.get("quality_message"),
            blocks=vlm_result.get("blocks", []),
            markdown=vlm_result.get("markdown"),
        ),
        hybrid_extraction={key: HybridField(**value) for key, value in hybrid_extraction.items()},
        compliance=ComplianceResult(
            overall_status=compliance["overall_status"],
            overall_confidence=compliance["overall_confidence"],
            evaluations=[RuleEvaluation(**e) for e in compliance["evaluations"]],
            summary=compliance["summary"],
        ),
        created_at=now,
    )

    return response


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------

@app.get("/api/inspections", response_model=List[InspectionSummary], dependencies=[Depends(auth.get_current_user)])
def list_inspections(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    """Return all inspections, newest first."""
    inspections = (
        db.query(Inspection)
        .order_by(Inspection.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    result = []
    for insp in inspections:
        result.append(InspectionSummary(
            inspection_id=insp.inspection_id,
            created_at=insp.created_at,
            product_name=insp.product_name,
            status=insp.status,
            overall_confidence=insp.overall_confidence,
            image_quality=insp.image_quality,
            violation_count=sum(1 for evaluation in insp.evaluations if evaluation.result == "NON-COMPLIANT"),
            is_demo=insp.is_demo,
        ))
    return result


@app.get("/api/inspections/{inspection_id}", response_model=InspectionDetail, dependencies=[Depends(auth.get_current_user)])
def get_inspection(inspection_id: str, db: Session = Depends(get_db)):
    """Return full details for one inspection."""
    insp = db.query(Inspection).filter(Inspection.inspection_id == inspection_id).first()
    if not insp:
        raise HTTPException(status_code=404, detail=f"Inspection '{inspection_id}' not found.")

    # Build relative image URL
    image_url = None
    if insp.image_path and os.path.exists(insp.image_path):
        fname = os.path.basename(insp.image_path)
        image_url = f"/uploads/{fname}"

    return InspectionDetail(
        inspection_id=insp.inspection_id,
        created_at=insp.created_at,
        product_name=insp.product_name,
        status=insp.status,
        overall_confidence=insp.overall_confidence,
        image_quality=insp.image_quality,
        image_path=image_url,
        is_demo=insp.is_demo,
        declarations=[DeclarationOut.model_validate(d) for d in insp.declarations],
        evaluations=[RuleEvaluationOut.model_validate(e) for e in insp.evaluations],
    )


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@app.get("/api/dashboard", response_model=DashboardStats, dependencies=[Depends(auth.get_current_user)])
def dashboard_stats(db: Session = Depends(get_db)):
    inspections = db.query(Inspection).all()
    total = len(inspections)
    compliant = sum(1 for i in inspections if i.status == "VERIFIED_COMPLIANT")
    violations = sum(1 for i in inspections if i.status == "POTENTIAL_VIOLATION")
    needs_review = sum(1 for i in inspections if i.status == "NEEDS_HUMAN_REVIEW")

    recent_raw = (
        db.query(Inspection)
        .order_by(Inspection.created_at.desc())
        .limit(10)
        .all()
    )
    recent = [
        InspectionSummary(
            inspection_id=i.inspection_id,
            created_at=i.created_at,
            product_name=i.product_name,
            status=i.status,
            overall_confidence=i.overall_confidence,
            image_quality=i.image_quality,
            violation_count=sum(1 for evaluation in i.evaluations if evaluation.result == "NON-COMPLIANT"),
            is_demo=i.is_demo,
        )
        for i in recent_raw
    ]
    return DashboardStats(
        total=total,
        compliant=compliant,
        violations=violations,
        needs_review=needs_review,
        recent=recent,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _build_report_data(insp: Inspection) -> dict:
    image_path = insp.image_path if insp.image_path and os.path.exists(insp.image_path) else None
    return {
        "inspection_id": insp.inspection_id,
        "created_at": insp.created_at,
        "product_name": insp.product_name,
        "overall_status": insp.status,
        "overall_confidence": insp.overall_confidence,
        "image_quality": insp.image_quality,
        "image_path": image_path,
        "declarations": [
            {
                "field_name": d.field_name,
                "extracted_value": d.extracted_value,
                "confidence": d.confidence,
                "status": d.status,
                "evidence_text": d.evidence_text,
            }
            for d in insp.declarations
        ],
        "violations": [
            {
                "field_name": evaluation.rule_reference,
                "reason": evaluation.requirement,
                "severity": evaluation.result,
                "confidence": evaluation.confidence,
                "rule_id": evaluation.rule_reference,
                "evidence": evaluation.evidence,
            }
            for evaluation in insp.evaluations
            if evaluation.result == "NON-COMPLIANT"
        ],
    }


@app.post("/api/reports/{inspection_id}/pdf", dependencies=[Depends(auth.get_current_user)])
def create_pdf_report(inspection_id: str, db: Session = Depends(get_db)):
    insp = db.query(Inspection).filter(Inspection.inspection_id == inspection_id).first()
    if not insp:
        raise HTTPException(status_code=404, detail="Inspection not found.")

    report_data = _build_report_data(insp)
    pdf_path = generate_pdf(report_data)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=500, detail="PDF generation failed.")

    # Save report record
    existing = db.query(Report).filter(Report.inspection_id == inspection_id).first()
    if existing:
        existing.pdf_path = pdf_path
    else:
        db.add(Report(inspection_id=inspection_id, pdf_path=pdf_path))
    db.commit()

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"SMART-LM-Report-{inspection_id}.pdf",
    )


@app.post("/api/reports/{inspection_id}/docx", dependencies=[Depends(auth.get_current_user)])
def create_docx_report(inspection_id: str, db: Session = Depends(get_db)):
    insp = db.query(Inspection).filter(Inspection.inspection_id == inspection_id).first()
    if not insp:
        raise HTTPException(status_code=404, detail="Inspection not found.")

    report_data = _build_report_data(insp)
    docx_path = generate_docx(report_data)
    if not docx_path or not os.path.exists(docx_path):
        raise HTTPException(status_code=500, detail="DOCX generation failed.")

    existing = db.query(Report).filter(Report.inspection_id == inspection_id).first()
    if existing:
        existing.docx_path = docx_path
    else:
        db.add(Report(inspection_id=inspection_id, docx_path=docx_path))
    db.commit()

    return FileResponse(
        docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"SMART-LM-Report-{inspection_id}.docx",
    )


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)




