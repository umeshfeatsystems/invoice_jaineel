"""
Split API System: Classification + Extraction
==============================================
Uses Gemini File Storage (48h retention)
"""

import os
import io
import time
import uuid
import logging
import asyncio
import aiofiles
import shutil
from typing import List, Optional, Dict, Any
from datetime import datetime

import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pypdf import PdfReader
from dotenv import load_dotenv

from services.prompt_config import (
    INVOICE_PROMPT, 
    PO_PROMPT,
    GRN_PROMPT, 
    CLASSIFICATION_PROMPT
)
from services.rate_limiter import initialize_rate_limiter, get_rate_limiter, release_rate_limit

# ==========================================
# CONFIGURATION
# ==========================================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SplitAPI")

API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

CONFIG = {
    "CLASSIFIER_MODEL": "gemini-2.5-flash",
    "EXTRACTOR_MODEL": "gemini-2.5-pro",
    "CLASSIFICATION_TIMEOUT": 120,
    "BASE_TIMEOUT_SECONDS": 180,
    "TIMEOUT_PER_PAGE": 30,
    "MAX_TIMEOUT_SECONDS": 1200,
    "MAX_CONCURRENT_EXTRACTIONS": 2,
}

PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "output": 3.75},
    "default": {"input": 0.10, "output": 0.40}
}

# ==========================================
# SCHEMAS
# ==========================================
class DocumentClassification(BaseModel):
    invoices: List[List[int]] = Field(default_factory=list)
    po: List[List[int]] = Field(default_factory=list)
    grn: List[List[int]] = Field(default_factory=list)

class ClassificationResponse(BaseModel):
    status: str
    gemini_file_name: str
    classification: DocumentClassification
    processing_time_seconds: float

class ExtractionRequest(BaseModel):
    gemini_file_name: str
    classification: DocumentClassification  # Pass classification from classify endpoint

class ExtractionResult(BaseModel):
    document_type: str 
    page_range: List[int]
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cost_usd: float = 0.0

class ExtractionResponse(BaseModel):
    status: str
    processing_time_seconds: float
    total_cost_usd: float
    results: List[ExtractionResult]

# ==========================================
# UTILITIES
# ==========================================
def get_pdf_page_count(file_content: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(file_content))
        return len(reader.pages)
    except:
        return 0

def calculate_cost(model_name, input_tokens, output_tokens):
    pricing = PRICING.get("default")
    for key in PRICING:
        if key in model_name:
            pricing = PRICING[key]
            break
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)

def calculate_timeout_for_pages(num_pages: int) -> int:
    base = CONFIG["BASE_TIMEOUT_SECONDS"]
    per_page = CONFIG["TIMEOUT_PER_PAGE"]
    max_timeout = CONFIG["MAX_TIMEOUT_SECONDS"]
    return min(base + (num_pages * per_page), max_timeout)

def resolve_refs(schema, defs=None):
    if defs is None: 
        defs = schema.get("$defs", {}) or schema.get("definitions", {})
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            name = ref.split("/")[-1]
            if name in defs: 
                return resolve_refs(defs[name], defs)
        new_schema = {}
        for k, v in schema.items():
            if k in ["$defs", "definitions"]: 
                continue
            new_schema[k] = resolve_refs(v, defs)
        return new_schema
    elif isinstance(schema, list):
        return [resolve_refs(item, defs) for item in schema]
    return schema

def clean_schema(schema):
    schema = resolve_refs(schema)
    def _clean(s):
        if isinstance(s, dict):
            if "anyOf" in s:
                for opt in s["anyOf"]:
                    if opt.get("type") != "null": 
                        return _clean(opt)
            for key in ["default", "title", "$defs", "definitions"]:
                if key in s: 
                    del s[key]
            for key, value in s.items(): 
                s[key] = _clean(value)
        elif isinstance(s, list):
            for i, item in enumerate(s): 
                s[i] = _clean(item)
        return s
    return _clean(schema)

def get_generation_config(response_schema=None):
    config = {"response_mime_type": "application/json", "temperature": 0.0}
    if response_schema:
        try:
            if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
                raw_schema = response_schema.model_json_schema()
                config["response_schema"] = clean_schema(raw_schema)
            else:
                config["response_schema"] = response_schema
        except:
            config["response_schema"] = response_schema
    return config

# ==========================================
# 3. PYDANTIC SCHEMAS (From Original main.py)
# ==========================================

# --- INVOICE MODELS ---
class BankDetails(BaseModel):
    bank_name: Optional[str] = None
    bank_account_no: Optional[str] = None
    iban: Optional[str] = None
    swift_code: Optional[str] = None
    bank_routing_no: Optional[str] = None
    bank_addr: Optional[str] = None

class InvoiceItem(BaseModel):
    line_no: Optional[str] = Field(None, description="Line Number")
    description: str = Field(..., description="Item Description")
    quantity: float = Field(..., description="Quantity")
    unit_price: float = Field(..., description="Unit Price")
    line_amount: float = Field(..., description="Line Amount")
    item_po_no: Optional[str] = Field(None, description="Customer Purchase Order Number")
    part_no: Optional[str] = Field(None, description="Part Number")
    hsn_code: Optional[str] = Field(None, description="HSN/SAC Code")

class Invoice(BaseModel):
    name: str = Field(..., description="Vendor Name")
    vendor_addr: Optional[str] = Field(None, description="Vendor Address")
    billing_name: Optional[str] = Field(None, description="Billing Name")
    billing_addr: Optional[str] = Field(None, description="Billing Address")
    shipping_addr: Optional[str] = Field(None, description="Shipping Address")
    invoice_no: str = Field(..., description="Invoice Number")
    po_no: Optional[str] = Field(None, description="Purchase Order Number")
    vendor_gstin: Optional[str] = Field(None, description="Vendor GST Number")
    billing_gstin: Optional[str] = Field(None, description="Billing/Customer GST Number")
    vendor_vat_no: Optional[str] = Field(None, description="Vendor VAT Number")
    billing_vat_no: Optional[str] = Field(None, description="Billing VAT Number")
    date: str = Field(..., description="Document Date (YYYY-MM-DD)")
    due_date: Optional[str] = Field(None, description="Due Date (YYYY-MM-DD)")
    shipping_date: Optional[str] = Field(None, description="Shipping Date (YYYY-MM-DD)")
    payment_terms: Optional[str] = Field(None, description="Payment Terms")
    payment_addr: Optional[str] = Field(None, description="Payment Address")
    tax: Optional[float] = Field(None, description="Tax Amount")
    tax_rate: Optional[float] = Field(None, description="Tax Rate")
    sgst_percentage: Optional[float] = Field(None, description="SGST Rate %")
    cgst_percentage: Optional[float] = Field(None, description="CGST Rate %")
    igst_percentage: Optional[float] = Field(None, description="IGST Rate %")
    sgst_total: Optional[float] = Field(None, description="Total SGST Amount")
    cgst_total: Optional[float] = Field(None, description="Total CGST Amount")
    igst_total: Optional[float] = Field(None, description="Total IGST Amount")
    net_amount: Optional[float] = Field(None, description="Net Amount")
    discount: Optional[float] = Field(None, description="Discount Amount")
    shipping_charges: Optional[float] = Field(None, description="Shipping Charges")
    total: float = Field(..., description="Total Amount")
    currency: str = Field(..., description="Invoice Currency")
    vendor_email: Optional[str] = Field(None, description="Vendor Email Address")
    shipping_method: Optional[str] = Field(None, description="Shipping Method")
    bank_details: Optional[BankDetails] = Field(None, description="Vendor Bank Details")
    items: List[InvoiceItem] = Field(default_factory=list, description="Invoice Line Items")

# --- PO & GRN MODELS ---
class POItem(BaseModel):
    line_number: Optional[float] = None
    description: str
    product_code: Optional[str] = None
    unit_price: float
    quantity: float
    line_amount: float

class PurchaseOrder(BaseModel):
    po_number: str
    date: str
    currency: str
    total_amount: float
    items: List[POItem] = Field(default_factory=list)

class GRNItem(BaseModel):
    item_description: Optional[str] = None
    item_part_no: Optional[str] = None
    qty_ordered: Optional[float] = None
    qty_received: Optional[float] = None

class GoodsReceivedNote(BaseModel):
    grn_number: Optional[str] = None
    date_received: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    items: List[GRNItem] = Field(default_factory=list)

# ==========================================
# CLASSIFICATION SERVICE
# ==========================================
async def classify_document(gemini_file, total_pages: int) -> DocumentClassification:
    model = genai.GenerativeModel(CONFIG["CLASSIFIER_MODEL"])
    config = get_generation_config(response_schema=DocumentClassification)
    
    try:
        await get_rate_limiter().acquire(CONFIG["CLASSIFIER_MODEL"])
        response = await model.generate_content_async(
            [CLASSIFICATION_PROMPT, gemini_file],
            generation_config=config,
            request_options={"timeout": CONFIG["CLASSIFICATION_TIMEOUT"]}
        )
        result = DocumentClassification.model_validate_json(response.text)
        
        # If no classification found, default to full document as invoice
        if not result.invoices and not result.po and not result.grn:
            logger.warning("No classification found, defaulting to invoice")
            result.invoices = [[1, total_pages]]
        
        return result
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        # Fallback: treat as invoice
        return DocumentClassification(invoices=[[1, total_pages]])
    finally:
        release_rate_limit(CONFIG["CLASSIFIER_MODEL"])

# ==========================================
# EXTRACTION SERVICE
# ==========================================
async def extract_document_chunk(gemini_file, doc_type: str, page_range: List[int]) -> ExtractionResult:
    start_time = time.time()
    
    if doc_type == "po":
        prompt = PO_PROMPT
        schema = PurchaseOrder
    elif doc_type == "grn":
        prompt = GRN_PROMPT
        schema = GoodsReceivedNote
    else:
        prompt = INVOICE_PROMPT
        schema = Invoice

    num_pages = page_range[1] - page_range[0] + 1
    timeout = calculate_timeout_for_pages(num_pages)
    
    # Add page range instruction to prompt
    page_instruction = f"\n\nIMPORTANT: Extract data ONLY from pages {page_range[0]} to {page_range[1]} of this document. Ignore all other pages."
    full_prompt = prompt + page_instruction
    
    try:
        await get_rate_limiter().acquire(CONFIG["EXTRACTOR_MODEL"])
        
        model = genai.GenerativeModel(CONFIG["EXTRACTOR_MODEL"])
        config = get_generation_config(response_schema=schema)
        
        response = await model.generate_content_async(
            [full_prompt, gemini_file],
            generation_config=config,
            request_options={"timeout": timeout}
        )
        
        usage = None
        if response.usage_metadata:
            usage = {
                "prompt_token_count": response.usage_metadata.prompt_token_count,
                "candidates_token_count": response.usage_metadata.candidates_token_count,
            }
        
        data_obj = schema.model_validate_json(response.text)
        cost = calculate_cost(
            CONFIG["EXTRACTOR_MODEL"], 
            usage["prompt_token_count"] if usage else 0, 
            usage["candidates_token_count"] if usage else 0
        )
        
        return ExtractionResult(
            document_type=doc_type,
            page_range=page_range,
            data=data_obj.dict(),
            cost_usd=cost,
        )
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return ExtractionResult(
            document_type=doc_type,
            page_range=page_range,
            error=str(e),
        )
    finally:
        release_rate_limit(CONFIG["EXTRACTOR_MODEL"])

# ==========================================
# FASTAPI APP
# ==========================================
app = FastAPI(title="Invoice Extraction API (Split)", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    initialize_rate_limiter(
        model_limits={
            "gemini-2.5-flash": 1000, 
            "gemini-2.5-pro": 150
        },
        safety_margin=0.7,
        min_request_gap_ms=500,
        max_concurrent_per_model=2
    )

# ==========================================
# API 1: CLASSIFICATION
# ==========================================
@app.post("/api/v1/classify", response_model=ClassificationResponse)
async def classify_endpoint(file: UploadFile = File(...)):
    """
    Step 1: Upload PDF to Gemini, classify, return file reference
    """
    start = time.time()
    temp_path = None
    
    try:
        # Read file
        content = await file.read()
        total_pages = get_pdf_page_count(content)
        
        # Save temporarily (use current directory for Windows compatibility)
        temp_path = f"{uuid.uuid4()}.pdf"
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(content)
        
        # Upload to Gemini (stored for 48h)
        gemini_file = genai.upload_file(temp_path, mime_type="application/pdf")
        
        # Classify
        classification = await classify_document(gemini_file, total_pages)
        
        # Clean ranges
        def clean_ranges(ranges_list):
            cleaned = []
            for r in ranges_list:
                valid = [p for p in r if isinstance(p, int) and 1 <= p <= total_pages]
                if valid: 
                    cleaned.append([min(valid), max(valid)])
            return cleaned

        classification.invoices = clean_ranges(classification.invoices)
        classification.po = clean_ranges(classification.po)
        classification.grn = clean_ranges(classification.grn)
        
        # If all empty after cleaning, default to invoice
        if not classification.invoices and not classification.po and not classification.grn:
            logger.warning("All classifications empty after cleaning, defaulting to invoice")
            classification.invoices = [[1, total_pages]]
        
        return ClassificationResponse(
            status="success",
            gemini_file_name=gemini_file.name,  # e.g. "files/abc123"
            classification=classification,
            processing_time_seconds=time.time() - start
        )
        
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass

# ==========================================
# API 2: EXTRACTION
# ==========================================
@app.post("/api/v1/extract", response_model=ExtractionResponse)
async def extract_endpoint(request: ExtractionRequest):
    """
    Step 2: Use Gemini file reference to extract data
    """
    start = time.time()
    
    try:
        # Get file from Gemini using file name
        gemini_file = genai.get_file(request.gemini_file_name)
        
        # Use classification passed from frontend
        classification = request.classification
        
        # Prepare extraction tasks
        tasks = []
        for r in classification.invoices:
            if len(r) >= 2:  # Validate range has start and end
                tasks.append(extract_document_chunk(gemini_file, "invoice", r))
        for r in classification.po:
            if len(r) >= 2:
                tasks.append(extract_document_chunk(gemini_file, "po", r))
        for r in classification.grn:
            if len(r) >= 2:
                tasks.append(extract_document_chunk(gemini_file, "grn", r))
        
        # If no valid tasks, return empty results
        if not tasks:
            return ExtractionResponse(
                status="success",
                processing_time_seconds=time.time() - start,
                total_cost_usd=0.0,
                results=[]
            )
        
        # Extract in parallel
        results = await asyncio.gather(*tasks)
        
        total_cost = sum(r.cost_usd for r in results)
        
        return ExtractionResponse(
            status="success",
            processing_time_seconds=time.time() - start,
            total_cost_usd=round(total_cost, 6),
            results=results
        )
        
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Run with: uvicorn main:app --host 0.0.0.0 --port 5612 --reload