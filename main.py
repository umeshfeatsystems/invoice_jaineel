"""
Optimized Invoice Document Processing Pipeline
=================================================
Key Features:
1. Universal Routing (Invoice, PO, GRN) - Vendor Agnostic
2. Parallel extraction (asyncio.gather) with Concurrency Control
3. Dynamic Prompt Selection based on Classification
4. Robust Error Handling for API Instability
"""

import os
import io
import time
import uuid
import logging
import asyncio
import aiofiles
import random
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime

import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from dotenv import load_dotenv

# Import prompts from centralized config
from services.prompt_config import (
    INVOICE_PROMPT, 
    PO_PROMPT,
    GRN_PROMPT, 
    CLASSIFICATION_PROMPT
)

# Import enhanced rate limiter with release function
from services.rate_limiter import initialize_rate_limiter, get_rate_limiter, release_rate_limit, get_rate_limit_stats

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT
# ==========================================
load_dotenv()

def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger("Invoice_optimized")

logger = setup_logging()

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment variables.")
else:
    genai.configure(api_key=API_KEY)

CONFIG = {
    # === MODEL CONFIGURATION ===
    # ONLY use these models - they have favorable rate limits
    # gemini-2.5-flash: 1000 RPM (classification) 
    # gemini-2.5-pro: 150 RPM (extraction)
    "CLASSIFIER_MODEL": "gemini-2.5-flash",    # Fast classification
    "EXTRACTOR_MODEL": "gemini-2.5-pro",       # Accurate extraction
    
    # === TIMEOUT CONFIGURATION ===
    "CLASSIFICATION_TIMEOUT": 120,           
    "BASE_TIMEOUT_SECONDS": 180,             
    "TIMEOUT_PER_PAGE": 30,                  
    "MAX_TIMEOUT_SECONDS": 1200,             
    
    # === RATE LIMIT SAFE PARALLEL CONFIGURATION ===
    # With gemini-2.5-pro at 150 RPM (effective 105 with 70% margin):
    # - Max 1.75 requests/second
    # - Minimum 571ms between requests
    # For 40+ splits: keeps us well under limits
    "MAX_CONCURRENT_EXTRACTIONS": 2,         # Only 2 concurrent to avoid bursts
    "BATCH_SIZE": 2,                         # Process 2 at a time
    "INTER_BATCH_DELAY_SECONDS": 3.0,        # Reduced from 5s (rate limiter handles pacing)
    "MIN_DELAY_BETWEEN_CALLS": 1.0,          # Backup delay (rate limiter is primary)
    
    # === ROBUST RETRY CONFIGURATION ===
    "MAX_RETRIES": 5,                        
    "INITIAL_RETRY_DELAY": 10.0,             
    "MAX_RETRY_DELAY": 60.0,                 
    "RETRY_MULTIPLIER": 2.0,                 
}

PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "output": 3.75}, 
    "default": {"input": 0.10, "output": 0.40}
}

QUOTA_ERROR_PATTERNS = [
    "quota", "rate limit", "resource exhausted", "429",
    "too many requests", "exceeded", "limit exceeded",
    "503", "service unavailable", "internal server error",
    "504", "cancelled", "deadline exceeded"
]

thread_pool = ThreadPoolExecutor(max_workers=4)
_api_semaphore = None

def get_api_semaphore():
    global _api_semaphore
    if _api_semaphore is None:
        _api_semaphore = asyncio.Semaphore(CONFIG["MAX_CONCURRENT_EXTRACTIONS"])
    return _api_semaphore

# ==========================================
# 2. UTILITY FUNCTIONS
# ==========================================
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

def get_pdf_page_count(file_content: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(file_content))
        return len(reader.pages)
    except Exception:
        return 0

def split_pdf_sync(original_pdf_path: str, ranges: List[List[int]], output_dir: str, prefix: str) -> List[str]:
    reader = PdfReader(original_pdf_path)
    output_paths = []
    if not os.path.exists(output_dir): 
        os.makedirs(output_dir)
    
    for i, (start, end) in enumerate(ranges):
        writer = PdfWriter()
        if start < 1: start = 1
        if end > len(reader.pages): end = len(reader.pages)
        for page_num in range(start - 1, end):
            writer.add_page(reader.pages[page_num])
        output_filename = f"{prefix}_{i+1}_{start}-{end}.pdf"
        output_path = os.path.join(output_dir, output_filename)
        with open(output_path, "wb") as f:
            writer.write(f)
        output_paths.append(output_path)
    return output_paths

async def split_pdf_async(original_pdf_path: str, ranges: List[List[int]], output_dir: str, prefix: str) -> List[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        thread_pool, 
        split_pdf_sync, 
        original_pdf_path, ranges, output_dir, prefix
    )

def is_transient_error(error: Exception) -> bool:
    error_str = str(error).lower()
    return any(pattern in error_str for pattern in QUOTA_ERROR_PATTERNS)

async def execute_with_exponential_backoff(
    async_func,
    max_retries: int = None,
    initial_delay: float = None,
    max_delay: float = None,
    multiplier: float = None,
    operation_name: str = "API call"
):
    max_retries = max_retries or CONFIG["MAX_RETRIES"]
    initial_delay = initial_delay or CONFIG["INITIAL_RETRY_DELAY"]
    max_delay = max_delay or CONFIG["MAX_RETRY_DELAY"]
    multiplier = multiplier or CONFIG["RETRY_MULTIPLIER"]
    
    delay = initial_delay
    
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(CONFIG["MIN_DELAY_BETWEEN_CALLS"])
            return await async_func()
        except Exception as e:
            if attempt >= max_retries:
                logger.error(f"[{operation_name}] Failed after {max_retries} retries: {e}")
                raise e
            if is_transient_error(e):
                logger.warning(
                    f"[{operation_name}] Transient Error (attempt {attempt+1}/{max_retries}). "
                    f"Waiting {delay:.1f}s... Error: {str(e)[:100]}"
                )
                jitter = delay * 0.2 * (random.random() * 2 - 1)
                await asyncio.sleep(delay + jitter)
                delay = min(delay * multiplier, max_delay)
            else:
                raise e

# ==========================================
# 3. PYDANTIC SCHEMAS
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

class Invoice(BaseModel):
    name: str = Field(..., description="Vendor Name")
    vendor_addr: Optional[str] = Field(None, description="Vendor Address")
    billing_name: Optional[str] = Field(None, description="Billing Name")
    billing_addr: Optional[str] = Field(None, description="Billing Address")
    shipping_addr: Optional[str] = Field(None, description="Shipping Address")
    invoice_no: str = Field(..., description="Invoice Number")
    po_no: Optional[str] = Field(None, description="Purchase Order Number")
    vendor_vat_no: Optional[str] = Field(None, description="Vendor VAT Number")
    billing_vat_no: Optional[str] = Field(None, description="Billing VAT Number")
    date: str = Field(..., description="Document Date (YYYY-MM-DD)")
    due_date: Optional[str] = Field(None, description="Due Date (YYYY-MM-DD)")
    shipping_date: Optional[str] = Field(None, description="Shipping Date (YYYY-MM-DD)")
    payment_terms: Optional[str] = Field(None, description="Payment Terms")
    payment_addr: Optional[str] = Field(None, description="Payment Address")
    tax: Optional[float] = Field(None, description="Tax Amount")
    tax_rate: Optional[float] = Field(None, description="Tax Rate")
    net_amount: Optional[float] = Field(None, description="Net Amount")
    discount: Optional[float] = Field(None, description="Discount Amount")
    shipping_charges: Optional[float] = Field(None, description="Shipping Charges")
    total: float = Field(..., description="Total Amount")
    currency: str = Field(..., description="Invoice Currency")
    vendor_email: Optional[str] = Field(None, description="Vendor Email Address")
    shipping_method: Optional[str] = Field(None, description="Shipping Method")
    bank_details: Optional[BankDetails] = Field(None, description="Vendor Bank Details")
    items: List[InvoiceItem] = Field(default_factory=list, description="Invoice Line Items")

# --- PURCHASE ORDER MODELS ---
class BuyerDetails(BaseModel):
    client_name: Optional[str] = Field(None, description="Buyer Company Name")
    client_address: Optional[str] = Field(None, description="Buyer Company Address")
    client_vat_no: Optional[str] = Field(None, description="Buyer VAT Number")
    client_phone: Optional[str] = Field(None, description="Buyer Phone Number")
    client_email: Optional[str] = Field(None, description="Buyer Email Address")
    client_fax: Optional[str] = Field(None, description="Buyer Fax")
    client_website: Optional[str] = Field(None, description="Buyer Website")
    client_contact_name: Optional[str] = Field(None, description="Buyer Contact Name")

class VendorDetails(BaseModel):
    vendor_name: Optional[str] = Field(None, description="Vendor Name")
    vendor_address: Optional[str] = Field(None, description="Vendor Address")
    supplier_code: Optional[str] = Field(None, description="Supplier Code")

class BillingDetails(BaseModel):
    billing_name: Optional[str] = Field(None, description="Billing Name")
    billing_address: Optional[str] = Field(None, description="Billing Address")

class DeliveryDetails(BaseModel):
    shipping_name: Optional[str] = Field(None, description="Delivery Name")
    shipping_address: Optional[str] = Field(None, description="Delivery Address")
    shipping_method: Optional[str] = Field(None, description="Shipping Method")
    incoterms: Optional[str] = Field(None, description="Incoterms")
    delivery_by_date: Optional[str] = Field(None, description="Delivery By Date")

class AmountSummary(BaseModel):
    net_amount: Optional[float] = Field(None, description="Total Net Amount")
    discount: Optional[float] = Field(None, description="Total Discount Amount")
    tax_rate: Optional[float] = Field(None, description="Tax Rate")
    tax_amount: Optional[float] = Field(None, description="Total Tax Amount")
    total_amount: Optional[float] = Field(None, description="Total Amount")

class POItem(BaseModel):
    line_number: Optional[float] = Field(None, description="Line Number")
    description: str = Field(..., description="Item Description")
    product_code: Optional[str] = Field(None, description="Product Code / Part Number")
    delivery_date: Optional[str] = Field(None, description="Item Delivery Date")
    unit_measure: Optional[str] = Field(None, description="Unit of Measure")
    unit_price: float = Field(..., description="Unit Price")
    quantity: float = Field(..., description="Quantity")
    line_net_amount: Optional[float] = Field(None, description="Line Net Amount")
    line_tax_rate: Optional[float] = Field(None, description="Line Tax Rate")
    line_tax_amount: Optional[float] = Field(None, description="Line Tax Amount")
    line_amount: float = Field(..., description="Line Amount")

class PurchaseOrder(BaseModel):
    po_number: str = Field(..., description="Purchase Order Number")
    date: str = Field(..., description="Purchase Order Date")
    expiry_date: Optional[str] = Field(None, description="Purchase Order Expiry Date")
    payment_terms: Optional[str] = Field(None, description="Payment Terms")
    
    buyer: BuyerDetails = Field(..., description="Buyer Company Details")
    vendor: VendorDetails = Field(..., description="Vendor / Supplier Details")
    billing: Optional[BillingDetails] = Field(None, description="Billing Details")
    delivery: Optional[DeliveryDetails] = Field(None, description="Delivery / Shipping Details")
    amounts: Optional[AmountSummary] = Field(None, description="Order Amount Summary")
    
    currency: str = Field(..., description="Purchase Order Currency")
    total_amount: float = Field(..., description="Total Amount")
    
    items: List[POItem] = Field(default_factory=list, description="Purchase Order Line Items")

# --- GRN MODELS ---
class GRNItem(BaseModel):
    item_description: Optional[str] = None
    item_part_no: Optional[str] = None
    qty_ordered: Optional[float] = None
    qty_received: Optional[float] = None
    qty_rejected: Optional[float] = None
    qty_accepted: Optional[float] = None
    remarks: Optional[str] = None
    batch_no: Optional[str] = None

class GoodsReceivedNote(BaseModel):
    grn_number: Optional[str] = None
    date_received: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    warehouse_location: Optional[str] = None
    carrier_name: Optional[str] = None
    vehicle_reg: Optional[str] = None
    waybill_no: Optional[str] = None
    receiver_name: Optional[str] = None
    total_packages: Optional[int] = None
    items: List[GRNItem] = Field(default_factory=list)


class DocumentClassification(BaseModel):
    """
    Classification result showing page ranges for each document type.
    Universal categories only.
    """
    invoices: List[List[int]] = Field(default_factory=list, description="Commercial Invoices")
    po: List[List[int]] = Field(default_factory=list, description="Purchase Orders")
    grn: List[List[int]] = Field(default_factory=list, description="Goods Received Notes")


class ExtractionResult(BaseModel):
    document_type: str 
    page_range: List[int]
    data: Optional[dict] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    cost_usd: Optional[float] = 0.0
    extraction_time_seconds: Optional[float] = None

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    processing_time_seconds: Optional[float] = None
    file_name: Optional[str] = None
    model_used: Optional[str] = None
    classification: Optional[DocumentClassification] = None
    results: Optional[List[ExtractionResult]] = None
    total_tokens: Optional[int] = 0
    total_cost_usd: Optional[float] = 0.0
    error: Optional[str] = None
    classification_time_seconds: Optional[float] = None
    extraction_parallelism: Optional[int] = None

# ==========================================
# 4. AI CONFIG HELPERS
# ==========================================
def resolve_refs(schema, defs=None):
    if defs is None: defs = schema.get("$defs", {}) or schema.get("definitions", {})
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            name = ref.split("/")[-1]
            if name in defs: return resolve_refs(defs[name], defs)
        new_schema = {}
        for k, v in schema.items():
            if k in ["$defs", "definitions"]: continue
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
                    if opt.get("type") != "null": return _clean(opt)
            for key in ["default", "title", "$defs", "definitions"]:
                if key in s: del s[key]
            for key, value in s.items(): s[key] = _clean(value)
        elif isinstance(s, list):
            for i, item in enumerate(s): s[i] = _clean(item)
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
        except Exception:
            config["response_schema"] = response_schema
    return config

# ==========================================
# 5. SERVICE FUNCTIONS
# ==========================================

# Pre-create model instances (reusable)
_classifier_model = None
_extractor_model = None

def get_classifier_model():
    global _classifier_model
    if _classifier_model is None:
        _classifier_model = genai.GenerativeModel(CONFIG["CLASSIFIER_MODEL"])
    return _classifier_model

def get_extractor_model():
    global _extractor_model
    if _extractor_model is None:
        _extractor_model = genai.GenerativeModel(CONFIG["EXTRACTOR_MODEL"])
    return _extractor_model

async def classify_documents_optimized(pdf_path: str) -> Tuple[DocumentClassification, float]:
    """Optimized classification with timing."""
    start_time = time.time()
    
    try:
        with open(pdf_path, "rb") as f:
            total_pages = get_pdf_page_count(f.read())
    except: 
        total_pages = 1000

    pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
    model = get_classifier_model()
    
    config = get_generation_config(response_schema=DocumentClassification)
    
    try:
        # Rate limit before classification
        await get_rate_limiter().acquire(CONFIG["CLASSIFIER_MODEL"])

        response = await model.generate_content_async(
            [CLASSIFICATION_PROMPT, pdf_file],
            generation_config=config,
            request_options={"timeout": CONFIG["CLASSIFICATION_TIMEOUT"]}
        )
        raw_class = DocumentClassification.model_validate_json(response.text)
        
        # Sanitize output
        def clean_ranges(ranges_list):
            cleaned = []
            for r in ranges_list:
                valid_pages = [p for p in r if isinstance(p, int) and 1 <= p <= total_pages]
                if valid_pages: cleaned.append([min(valid_pages), max(valid_pages)])
            return cleaned

        raw_class.invoices = clean_ranges(raw_class.invoices)
        raw_class.po = clean_ranges(raw_class.po)
        raw_class.grn = clean_ranges(raw_class.grn)
        
        elapsed = time.time() - start_time
        return raw_class, elapsed
        
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        elapsed = time.time() - start_time
        # Default fallback if classification completely fails
        return DocumentClassification(invoices=[[1, total_pages]]), elapsed


async def extract_single_document(
    pdf_path: str, 
    doc_type: str, 
    page_range: List[int],
    doc_index: int
) -> ExtractionResult:
    """
    Extract a single document using the correct prompt based on doc_type.
    """
    start_time = time.time()
    num_pages = page_range[1] - page_range[0] + 1 if len(page_range) == 2 else 1
    dynamic_timeout = calculate_timeout_for_pages(num_pages)
    
    # 1. SELECT PROMPT & SCHEMA
    if doc_type == "po":
        prompt = PO_PROMPT
        schema = PurchaseOrder
        log_type = "Purchase Order"
    elif doc_type == "grn":
        prompt = GRN_PROMPT
        schema = GoodsReceivedNote
        log_type = "Goods Received Note"
    else:
        # Default to Invoice
        prompt = INVOICE_PROMPT
        schema = Invoice
        log_type = "Commercial Invoice"
    
    logger.info(f"Doc {doc_index}: Starting {log_type} (pages {page_range[0]}-{page_range[1]}, {num_pages} pages, timeout={dynamic_timeout}s)")
    
    async with get_api_semaphore():  # Concurrency control via semaphore
        try:
            # --- PARALLEL EXECUTION: MAIN EXTRACTION + HEADER OCR ---
            
            # TASK A: Main Table Extraction (Gemini Pro)
            async def run_main_extraction():
                # Rate limit before extraction (sliding window + burst protection)
                await get_rate_limiter().acquire(CONFIG["EXTRACTOR_MODEL"])
                
                pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
                model = get_extractor_model()
                
                config = get_generation_config(response_schema=schema)
                
                response = await model.generate_content_async(
                    [prompt, pdf_file],
                    generation_config=config,
                    request_options={"timeout": dynamic_timeout}
                )
                return response
            
            # TASK B: Header OCR Flow (Gemini Flash x2)
            # Only run for Invoices, not POs or GRNs
            async def run_header_extraction():
                if schema == Invoice:
                    # Assuming refined_extractor module exists and is compatible
                    try:
                        from services.refined_extractor import run_header_ocr_flow
                        return await run_header_ocr_flow(pdf_path)
                    except ImportError:
                        logger.warning("Refined extractor module not found, skipping header flow.")
                        return {}
                return {}

            # Execute Parallel
            main_response_data, header_data = await asyncio.gather(
                execute_with_exponential_backoff(run_main_extraction, operation_name=f"Extract doc {doc_index}"),
                run_header_extraction(),
                return_exceptions=False
            )
            
            # Usage Tracking (Main Model)
            usage = None
            if main_response_data.usage_metadata:
                usage = {
                    "prompt_token_count": main_response_data.usage_metadata.prompt_token_count,
                    "candidates_token_count": main_response_data.usage_metadata.candidates_token_count,
                    "total_token_count": main_response_data.usage_metadata.total_token_count
                }
            
            # Parse Main Data
            pydantic_obj = schema.model_validate_json(main_response_data.text)
            extracted_data = pydantic_obj.dict()
            
            # --- MERGE & BROADCAST LOGIC (Invoices Only) ---
            # NOTE: Logic updated/removed because the new Invoice schema 
            # does not support the old fields (invoice_toi, item_mfg_name, etc.)
            if schema == Invoice and header_data:
                # We can try to map what exists, but most fields are different now.
                # For now, we skip the old legacy merge to avoid errors.
                pass

            # --- POST PROCESSING ---
            if schema == Invoice:
                extracted_data = post_process_invoice(extracted_data)
            # Add post-processing for PO/GRN if needed here
            
            elapsed = time.time() - start_time
            cost = calculate_cost(
                CONFIG["EXTRACTOR_MODEL"], 
                usage.get("prompt_token_count", 0) if usage else 0, 
                usage.get("candidates_token_count", 0) if usage else 0
            )
            
            logger.info(f"Doc {doc_index} ({doc_type}) extracted in {elapsed:.2f}s")
            
            return ExtractionResult(
                document_type=doc_type,
                page_range=page_range,
                data=extracted_data,
                token_usage=usage,
                cost_usd=cost,
                extraction_time_seconds=round(elapsed, 2)
            )
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"Extraction failed for doc {doc_index} after all retries: {e}")
            return ExtractionResult(
                document_type=doc_type,
                page_range=page_range,
                error=str(e),
                extraction_time_seconds=round(elapsed, 2)
            )
        finally:
            # CRITICAL: Release rate limiter slot when extraction completes
            release_rate_limit(CONFIG["EXTRACTOR_MODEL"])


def post_process_invoice(data: Dict[str, Any]) -> Dict[str, Any]:
    INVALID_LITERALS = {"", "null", "string", "number", "integer", "float", "boolean", "None"}
    
    def clean_value(value):
        if value is None: return None
        if isinstance(value, str) and value.strip() in INVALID_LITERALS: return None
        return value
    
    if "items" in data and isinstance(data["items"], list):
        for idx, item in enumerate(data["items"], 1):
            
            # 2. Extract Fields (mapped to new schema)
            desc = (item.get("description") or "").strip()
            part = (item.get("part_no") or "").strip()

            # 3. Description Cleaning (Remove Part Number if present at end)
            if desc and part and desc.endswith(part):
                clean_desc = desc[:-len(part)].strip()
                if clean_desc:
                    item["description"] = clean_desc
            
            # Clean all values
            for key in list(item.keys()): 
                item[key] = clean_value(item[key])
    
    for key in list(data.keys()):
        if key != "items": data[key] = clean_value(data[key])
    return data


# ==========================================
# 6. OPTIMIZED PIPELINE (PARALLEL + DASHBOARD)
# ==========================================
async def run_pipeline_optimized(job_id: str, file_path: str, model_name: str):
    """
    Optimized pipeline with PARALLEL extraction and Terminal Dashboard.
    """
    try:
        JOBS[job_id]["status"] = "classifying"
        pipeline_start = time.time()
        
        # Step 1: Classification
        logger.info(f"Job {job_id}: Classifying...")
        classification, class_time = await classify_documents_optimized(file_path)
        JOBS[job_id]["classification"] = classification.dict()
        JOBS[job_id]["classification_time_seconds"] = round(class_time, 2)
        
        # ===== DASHBOARD: CLASSIFICATION =====
        def count_pages(ranges): return sum((r[1] - r[0] + 1) for r in ranges) if ranges else 0
        
        print("\n" + "="*60)
        print(f"📋 CLASSIFICATION RESULT (Job: {job_id[:8]}...)")
        print("="*60)
        print(f"⏱️  Classification time: {class_time:.2f}s")
        print(f"\n📦 INVOICES: {len(classification.invoices)} docs ({count_pages(classification.invoices)} pages)")
        print(f"📝 POs:      {len(classification.po)} docs ({count_pages(classification.po)} pages)")
        print(f"🚚 GRNs:     {len(classification.grn)} docs ({count_pages(classification.grn)} pages)")
        print("="*60 + "\n")
        
        # Step 2: Split PDF & Build Tasks
        JOBS[job_id]["status"] = "splitting"
        split_tasks = []
        
        for r in classification.invoices: split_tasks.append({"type": "invoice", "range": r})
        for r in classification.po:       split_tasks.append({"type": "po", "range": r})
        for r in classification.grn:      split_tasks.append({"type": "grn", "range": r})
        
        ranges = [t["range"] for t in split_tasks]
        if not ranges:
            split_tasks = [{"type": "invoice", "range": [1, 1]}]
            split_paths = [file_path]
        else:
            split_paths = await split_pdf_async(
                file_path, ranges, 
                os.path.join(SPLIT_DIR, job_id), job_id
            )
        
        # Step 3: PARALLEL EXTRACTION
        JOBS[job_id]["status"] = "extracting"
        JOBS[job_id]["extraction_parallelism"] = min(len(split_paths), CONFIG["MAX_CONCURRENT_EXTRACTIONS"])
        
        extraction_start = time.time()
        batch_size = CONFIG["BATCH_SIZE"]
        inter_batch_delay = CONFIG["INTER_BATCH_DELAY_SECONDS"]
        
        extraction_tasks = [
            {
                "pdf_path": split_paths[i],
                "doc_type": split_tasks[i]["type"],
                "page_range": split_tasks[i]["range"],
                "doc_index": i + 1
            }
            for i in range(len(split_paths))
            if i < len(split_tasks)
        ]
        
        all_results = []
        num_batches = (len(extraction_tasks) + batch_size - 1) // batch_size
        
        logger.info(f"Job {job_id}: Processing {len(extraction_tasks)} documents in {num_batches} batches (Parallelism={CONFIG['MAX_CONCURRENT_EXTRACTIONS']})")
        
        for batch_idx in range(num_batches):
            batch_start = batch_idx * batch_size
            batch_end = min(batch_start + batch_size, len(extraction_tasks))
            batch = extraction_tasks[batch_start:batch_end]
            
            logger.info(f"Job {job_id}: Starting batch {batch_idx + 1}/{num_batches} ({len(batch)} docs)")
            
            # [FIXED] PARALLEL EXECUTION
            # 1. Create list of coroutine objects (tasks) but do not await them yet
            tasks = [
                extract_single_document(
                    pdf_path=task["pdf_path"],
                    doc_type=task["doc_type"],
                    page_range=task["page_range"],
                    doc_index=task["doc_index"]
                )
                for task in batch
            ]
            
            # 2. Fire all tasks in this batch AT THE SAME TIME using gather
            # return_exceptions=True prevents one failure from crashing the batch
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Collect results
            for res in batch_results:
                if isinstance(res, Exception):
                    logger.error(f"Batch task failed: {res}")
                    all_results.append(res)
                else:
                    all_results.append(res)
            
            if batch_idx < num_batches - 1:
                logger.debug(f"Job {job_id}: Waiting {inter_batch_delay}s before next batch...")
                await asyncio.sleep(inter_batch_delay)
        
        extraction_time = time.time() - extraction_start
        logger.info(f"Job {job_id}: All {len(all_results)} extractions completed in {extraction_time:.2f}s")
        
        # Process results
        final_results = []
        total_tokens = 0
        total_cost = 0.0
        
        for result in all_results:
            if isinstance(result, Exception):
                logger.error(f"Extraction exception: {result}")
                continue
            if isinstance(result, ExtractionResult):
                final_results.append(result)
                if result.token_usage:
                    total_tokens += result.token_usage.get("total_token_count", 0)
                total_cost += result.cost_usd or 0.0
        
        # Finalize
        JOBS[job_id]["results"] = [r.dict() for r in final_results]
        JOBS[job_id]["total_tokens"] = total_tokens
        JOBS[job_id]["total_cost_usd"] = round(total_cost, 6)
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["completed_at"] = datetime.now()
        JOBS[job_id]["processing_time_seconds"] = round(time.time() - pipeline_start, 2)
        
        # ===== DASHBOARD: COMPLETION =====
        success_count = sum(1 for r in final_results if r.error is None)
        fail_count = sum(1 for r in final_results if r.error is not None)
        
        print("\n" + "="*60)
        print(f"✅ JOB COMPLETED (Job: {job_id[:8]}...)")
        print("="*60)
        print(f"⏱️  Total time: {JOBS[job_id]['processing_time_seconds']}s")
        print(f"📄 Documents extracted: {success_count}/{len(final_results)}")
        if fail_count > 0:
            print(f"❌ Failed: {fail_count}")
            for r in final_results:
                if r.error:
                    print(f"   - {r.document_type} (pages {r.page_range}): {r.error[:50]}...")
        print(f"💰 Total cost: ${total_cost:.4f}")
        print(f"🔢 Total tokens: {total_tokens}")
        
        # Rate limiter stats
        try:
            stats = get_rate_limit_stats()
            print("\n🛡️ Rate Limiter Stats:")
            for model, st in stats.items():
                print(f"   {model}: {st['current_window']}/{st['limit_rpm']} RPM, waited {st['total_wait_seconds']}s total")
        except:
            pass
        
        print("="*60 + "\n")
        
        logger.info(f"Job {job_id}: COMPLETED in {JOBS[job_id]['processing_time_seconds']}s")
        
    except Exception as e:
        logger.error(f"Job {job_id} Failed: {e}")
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)

# ==========================================
# 7. FASTAPI APPLICATION
# ==========================================
app = FastAPI(title="Invoice Extraction API (Universal Routing + Parallel)", version="3.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """
    Initialize bulletproof rate limiter on application startup.
    
    Rate Limits (from Google AI Studio dashboard):
    - gemini-2.5-flash: 1000 RPM, 1M TPM, 10K RPD
    - gemini-2.5-pro: 150 RPM, 2M TPM, 10K RPD
    
    With 70% safety margin:
    - Flash: 700 effective RPM
    - Pro: 105 effective RPM (~1.75 req/sec)
    """
    initialize_rate_limiter(
        model_limits={
            "gemini-2.5-flash": 1000,  # Classification model
            "gemini-2.5-pro": 150,     # Extraction model - THIS IS THE BOTTLENECK
        },
        safety_margin=0.7,             # 70% of limit (more conservative)
        min_request_gap_ms=500,        # Minimum 500ms between requests to same model
        max_concurrent_per_model=2     # Max 2 concurrent requests per model
    )
    logger.info("✅ Bulletproof rate limiter initialized for 40+ split handling")

JOBS = {}
UPLOAD_DIR = "uploads"
SPLIT_DIR = "splits"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SPLIT_DIR, exist_ok=True)

@app.post("/api/v1/process-document", response_model=JobStatusResponse)
async def process_document(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF allowed.")

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}.pdf")
    
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
    
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.now(),
        "file_name": file.filename,
        "model_used": CONFIG["EXTRACTOR_MODEL"]
    }
    
    await run_pipeline_optimized(job_id, file_path, CONFIG["EXTRACTOR_MODEL"])
    
    return JobStatusResponse(**JOBS[job_id])

# ==========================================
# NEW ENDPOINTS FOR BREAKDOWN TASKS
# ==========================================

@app.post("/api/v1/classify-document", response_model=DocumentClassification)
async def api_classify_document(file: UploadFile = File(...)):
    """
    Standalone endpoint to just CLASSIFY the document without extraction.
    Returns page ranges for Invoices, POs, and GRNs.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF allowed.")

    temp_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"temp_classify_{temp_id}.pdf")
    
    try:
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
            
        classification, _ = await classify_documents_optimized(file_path)
        return classification
        
    except Exception as e:
        logger.error(f"Classify API failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup
        if os.path.exists(file_path):
            os.remove(file_path)

@app.post("/api/v1/extract-document", response_model=ExtractionResult)
async def api_extract_document(
    file: UploadFile = File(...),
    doc_type: str = Form(..., description="Type of document: 'invoice', 'po', or 'grn'", regex="^(invoice|po|grn)$"),
    start_page: int = Form(..., description="Start page number (1-based)"),
    end_page: int = Form(..., description="End page number (1-based)")
):
    """
    Standalone endpoint to EXTRACT text from a specific page range of a document.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF allowed.")
    
    if start_page < 1 or end_page < start_page:
        raise HTTPException(status_code=400, detail="Invalid page range.")

    temp_id = str(uuid.uuid4())
    original_path = os.path.join(UPLOAD_DIR, f"temp_extract_orig_{temp_id}.pdf")
    
    # We must split the specific pages to a new file because extract_single_document 
    # sends the whole file at pdf_path to the AI.
    split_dir = os.path.join(SPLIT_DIR, f"temp_extract_{temp_id}")
    split_path = None

    try:
        # 1. Save Original
        content = await file.read()
        async with aiofiles.open(original_path, "wb") as f:
            await f.write(content)
            
        # 2. Split PDF to get only the relevant pages (saves tokens/cost)
        # split_pdf_async returns a list of paths, we expect 1 here
        split_paths = await split_pdf_async(
            original_path, 
            [[start_page, end_page]], 
            split_dir, 
            f"extract_{temp_id}"
        )
        
        if not split_paths:
            raise HTTPException(status_code=500, detail="Failed to slice PDF.")
            
        split_path = split_paths[0]
        
        # 3. Extract
        result = await extract_single_document(
            pdf_path=split_path,
            doc_type=doc_type,
            page_range=[start_page, end_page],
            doc_index=1
        )
        
        return result

    except Exception as e:
        logger.error(f"Extract API failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup
        if os.path.exists(original_path):
            os.remove(original_path)
        if split_path and os.path.exists(split_path):
            os.remove(split_path)
        if os.path.exists(split_dir):
            import shutil
            shutil.rmtree(split_dir, ignore_errors=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5612, timeout_keep_alive=300)