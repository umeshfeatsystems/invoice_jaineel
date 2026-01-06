"""
Single Endpoint Invoice Extraction API (Synchronous)
====================================================
Input: PDF File via POST
Output: Consolidated JSON Response (No Polling)
"""

import os
import io
import time
import uuid
import logging
import asyncio
import aiofiles
import shutil
import random
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime

import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from dotenv import load_dotenv

# Import prompts from centralized config (assuming services folder exists)
from services.prompt_config import (
    INVOICE_PROMPT, 
    PO_PROMPT,
    GRN_PROMPT, 
    CLASSIFICATION_PROMPT
)

# Import enhanced rate limiter (assuming services folder exists)
from services.rate_limiter import (
    initialize_rate_limiter, 
    get_rate_limiter, 
    release_rate_limit, 
    get_rate_limit_stats
)

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT
# ==========================================
load_dotenv()

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger("Invoice_API")

logger = setup_logging()

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    logger.warning("⚠️ GEMINI_API_KEY not found in environment variables.")
else:
    genai.configure(api_key=API_KEY)

CONFIG = {
    "CLASSIFIER_MODEL": "gemini-2.5-flash",
    "EXTRACTOR_MODEL": "gemini-2.5-pro",
    "CLASSIFICATION_TIMEOUT": 120,           
    "BASE_TIMEOUT_SECONDS": 180,             
    "TIMEOUT_PER_PAGE": 30,                  
    "MAX_TIMEOUT_SECONDS": 1200,             
    "MAX_CONCURRENT_EXTRACTIONS": 2,         
    "BATCH_SIZE": 2,                         
    "INTER_BATCH_DELAY_SECONDS": 1.0,        
    "MAX_RETRIES": 3,                        
    "INITIAL_RETRY_DELAY": 5.0,
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
        # Handle 1-based to 0-based conversion logic safely
        s = max(1, start)
        e = min(len(reader.pages), end)
        
        for page_num in range(s - 1, e):
            writer.add_page(reader.pages[page_num])
        
        output_filename = f"{prefix}_{i+1}_{s}-{e}.pdf"
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
                await asyncio.sleep(CONFIG["MIN_DELAY_BETWEEN_CALLS"] if "MIN_DELAY_BETWEEN_CALLS" in CONFIG else 0.5)
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
# 3. PYDANTIC SCHEMAS (Preserved Completely)
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
    
    # --- India GST Fields ---
    vendor_gstin: Optional[str] = Field(None, description="Vendor GST Number")
    billing_gstin: Optional[str] = Field(None, description="Billing/Customer GST Number")
    
    vendor_vat_no: Optional[str] = Field(None, description="Vendor VAT Number")
    billing_vat_no: Optional[str] = Field(None, description="Billing VAT Number")
    date: str = Field(..., description="Document Date (YYYY-MM-DD)")
    due_date: Optional[str] = Field(None, description="Due Date (YYYY-MM-DD)")
    shipping_date: Optional[str] = Field(None, description="Shipping Date (YYYY-MM-DD)")
    payment_terms: Optional[str] = Field(None, description="Payment Terms")
    payment_addr: Optional[str] = Field(None, description="Payment Address")
    
    # --- Tax Details ---
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

# --- RESPONSE MODELS ---

class DocumentClassification(BaseModel):
    """
    Classification result showing page ranges for each document type.
    """
    invoices: List[List[int]] = Field(default_factory=list, description="Commercial Invoices")
    po: List[List[int]] = Field(default_factory=list, description="Purchase Orders")
    grn: List[List[int]] = Field(default_factory=list, description="Goods Received Notes")

class ExtractionResult(BaseModel):
    document_type: str 
    page_range: List[int]
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None
    cost_usd: float = 0.0
    extraction_time_seconds: float = 0.0

class FullApiResponse(BaseModel):
    status: str
    processing_time_seconds: float
    total_cost_usd: float
    classification: DocumentClassification
    results: List[ExtractionResult]

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

async def classify_document(pdf_path: str) -> Tuple[DocumentClassification, float]:
    start = time.time()
    try:
        with open(pdf_path, "rb") as f:
            total_pages = get_pdf_page_count(f.read())
    except:
        total_pages = 1000

    pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
    model = get_classifier_model()
    config = get_generation_config(response_schema=DocumentClassification)
    
    try:
        await get_rate_limiter().acquire(CONFIG["CLASSIFIER_MODEL"])
        
        response = await model.generate_content_async(
            [CLASSIFICATION_PROMPT, pdf_file],
            generation_config=config,
            request_options={"timeout": CONFIG["CLASSIFICATION_TIMEOUT"]}
        )
        result = DocumentClassification.model_validate_json(response.text)
        
        # Sanitize
        def clean_ranges(ranges_list):
            cleaned = []
            for r in ranges_list:
                valid_pages = [p for p in r if isinstance(p, int) and 1 <= p <= total_pages]
                if valid_pages: cleaned.append([min(valid_pages), max(valid_pages)])
            return cleaned

        result.invoices = clean_ranges(result.invoices)
        result.po = clean_ranges(result.po)
        result.grn = clean_ranges(result.grn)
        
        return result, time.time() - start
        
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        # Return fallback classification (Assume whole doc is invoice)
        return DocumentClassification(invoices=[[1, total_pages]]), time.time() - start

async def extract_document_chunk(
    pdf_path: str, 
    doc_type: str, 
    page_range: List[int],
    doc_index: int
) -> ExtractionResult:
    start_time = time.time()
    
    # 1. Select Prompt & Schema
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
    
    logger.info(f"Extracting {doc_type} (pages {page_range[0]}-{page_range[1]})")

    async with get_api_semaphore():
        try:
            await get_rate_limiter().acquire(CONFIG["EXTRACTOR_MODEL"])
            
            async def run_extraction():
                pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
                model = get_extractor_model()
                config = get_generation_config(response_schema=schema)
                return await model.generate_content_async(
                    [prompt, pdf_file],
                    generation_config=config,
                    request_options={"timeout": timeout}
                )

            # Use retry logic
            response = await execute_with_exponential_backoff(run_extraction, operation_name=f"Extract doc {doc_index}")
            
            # Metadata
            usage = None
            if response.usage_metadata:
                usage = {
                    "prompt_token_count": response.usage_metadata.prompt_token_count,
                    "candidates_token_count": response.usage_metadata.candidates_token_count,
                    "total_token_count": response.usage_metadata.total_token_count
                }
            
            # Validate & Parse
            data_obj = schema.model_validate_json(response.text)
            data_dict = data_obj.dict()
            
            # Post Process (simple cleanup)
            data_dict = post_process_data(data_dict)
            
            cost = calculate_cost(CONFIG["EXTRACTOR_MODEL"], usage["prompt_token_count"] if usage else 0, usage["candidates_token_count"] if usage else 0)
            
            return ExtractionResult(
                document_type=doc_type,
                page_range=page_range,
                data=data_dict,
                token_usage=usage,
                cost_usd=cost,
                extraction_time_seconds=time.time() - start_time
            )
            
        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return ExtractionResult(
                document_type=doc_type,
                page_range=page_range,
                error=str(e),
                extraction_time_seconds=time.time() - start_time
            )
        finally:
            release_rate_limit(CONFIG["EXTRACTOR_MODEL"])

def post_process_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Basic cleanup logic shared across docs."""
    INVALID_LITERALS = {"", "null", "string", "number", "None"}
    
    def clean_value(value):
        if value is None: return None
        if isinstance(value, str) and value.strip() in INVALID_LITERALS: return None
        return value

    if "items" in data and isinstance(data["items"], list):
        for item in data["items"]:
            # Special Invoice logic: Clean description
            if "description" in item and "part_no" in item:
                desc = (item.get("description") or "").strip()
                part = (item.get("part_no") or "").strip()
                if desc and part and desc.endswith(part):
                    clean_desc = desc[:-len(part)].strip()
                    if clean_desc: item["description"] = clean_desc
            
            for k in list(item.keys()):
                item[k] = clean_value(item[k])

    for k in list(data.keys()):
        if k != "items":
            data[k] = clean_value(data[k])
            
    return data

# ==========================================
# 6. API DEFINITION
# ==========================================
app = FastAPI(title="Invoice Extraction API (Synchronous)", version="4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    # Initialize rate limiter
    initialize_rate_limiter(
        model_limits={
            "gemini-2.5-flash": 1000, 
            "gemini-2.5-pro": 150
        },
        safety_margin=0.7,
        min_request_gap_ms=500,
        max_concurrent_per_model=2
    )

JOBS = {} # Removed logic, but keeping variable if needed for extension
UPLOAD_DIR = "uploads"
SPLIT_DIR = "splits"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SPLIT_DIR, exist_ok=True)

@app.post("/api/v1/extract", response_model=FullApiResponse)
async def extract_document_sync(file: UploadFile = File(...)):
    """
    Synchronous Endpoint:
    1. Uploads PDF
    2. Classifies Doc
    3. Splits PDF
    4. Extracts Data
    5. Returns JSON
    """
    request_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{request_id}.pdf")
    split_dir_path = os.path.join(SPLIT_DIR, request_id)
    
    start_total = time.time()
    
    try:
        # 1. Save uploaded file
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
            
        # 2. Classify
        classification, _ = await classify_document(file_path)
        
        # 3. Prepare Split Tasks
        split_tasks = []
        for r in classification.invoices: split_tasks.append({"type": "invoice", "range": r})
        for r in classification.po:       split_tasks.append({"type": "po", "range": r})
        for r in classification.grn:      split_tasks.append({"type": "grn", "range": r})
        
        ranges = [t["range"] for t in split_tasks]
        
        # If no classification found, default to invoice
        if not ranges:
            page_count = get_pdf_page_count(content)
            split_tasks = [{"type": "invoice", "range": [1, page_count]}]
            split_paths = [file_path] # No split needed
            # Update classification object for response
            classification.invoices = [[1, page_count]]
        else:
            split_paths = await split_pdf_async(
                file_path, ranges, 
                split_dir_path, request_id
            )
        
        # 4. Extract (Parallel)
        extraction_tasks = []
        for i, path in enumerate(split_paths):
            if i < len(split_tasks):
                meta = split_tasks[i]
                extraction_tasks.append(
                    extract_document_chunk(path, meta["type"], meta["range"], i+1)
                )
        
        results = await asyncio.gather(*extraction_tasks)
        
        # 5. Aggregate
        total_cost = sum(r.cost_usd for r in results)
        
        return FullApiResponse(
            status="success",
            processing_time_seconds=time.time() - start_total,
            total_cost_usd=round(total_cost, 6),
            classification=classification,
            results=results
        )
        
    except Exception as e:
        logger.error(f"Global pipeline failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    finally:
        # Cleanup
        if os.path.exists(file_path):
            os.remove(file_path)
        if os.path.exists(split_dir_path):
            shutil.rmtree(split_dir_path, ignore_errors=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5612)