"""
Document Extractor Service (Adaptable Edition)
==============================================
Engineered for Gemini 3.0 Pro with DYNAMIC TIMEOUTS.
- Calculates PDF page count before processing.
- Adapts timeout: Small docs = Fast fail / Large docs = Long wait.
- 1 Page -> ~3 min timeout
- 140 Pages -> ~2 hours timeout
"""

import time
import io
import logging
import google.generativeai as genai
from typing import Tuple, Optional, Dict, Any
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable, InternalServerError, ResourceExhausted
from pypdf import PdfReader

from models.schemas import Invoice, AirwayBill
from models.gemini_config import get_generation_config
from services.prompt_config import INVOICE_PROMPT, AWB_PROMPT

logger = logging.getLogger("Invoice_extractor")

# --- ADAPTABLE CONFIGURATION ---
BASE_TIMEOUT = 120        # Base overhead for "thinking" (2 mins)
SECONDS_PER_PAGE = 60     # Add 60s per page (Conservative for Reasoning models)
MAX_RETRIES = 5
INITIAL_BACKOFF = 5       # Start with 5s wait (faster recovery)

def get_page_count(pdf_path: str) -> int:
    """Reads PDF page count efficiently."""
    try:
        with open(pdf_path, 'rb') as f:
            reader = PdfReader(f)
            return len(reader.pages)
    except Exception:
        return 10 # Fallback safe assumption

def calculate_timeout(pages: int) -> int:
    """
    Dynamically allocates time based on document length.
    1 page  -> 180s (3m)
    50 pages -> 3120s (52m)
    140 pages -> 8520s (2.3h)
    """
    timeout = BASE_TIMEOUT + (pages * SECONDS_PER_PAGE)
    return timeout

def execute_with_adaptable_retry(func, operation_name: str, pages: int):
    """
    Executes with a timeout tailored to the document size.
    """
    delay = INITIAL_BACKOFF
    last_exception = None
    
    # Calculate specific timeout for this run
    timeout_limit = calculate_timeout(pages)
    print(f"🔹 {operation_name}: Processing {pages} pages. Timeout set to {timeout_limit}s.")
    
    for attempt in range(MAX_RETRIES):
        try:
            return func(timeout=timeout_limit)
        except (DeadlineExceeded, ServiceUnavailable, InternalServerError, ResourceExhausted) as e:
            last_exception = e
            # Log specific waiting message
            print(f"⏳ {operation_name}: Busy/Timeout (Attempt {attempt+1}/{MAX_RETRIES}). Waiting {delay}s...")
            time.sleep(delay)
            delay *= 2  # Exponential backoff
        except Exception as e:
            print(f"❌ {operation_name} Critical Error: {e}")
            raise e
            
    print(f"❌ {operation_name}: Failed after {MAX_RETRIES} attempts.")
    raise last_exception

# =============================================================================
# DATA CLEANING RULES
# =============================================================================

INVALID_LITERALS = {"", "null", "string", "number", "integer", "float", "boolean", "None"}

def clean_value(value):
    if value is None: return None
    if isinstance(value, str) and value.strip() in INVALID_LITERALS: return None
    return value

def post_process_invoice(data: Dict[str, Any]) -> Dict[str, Any]:
    toi = data.get("invoice_toi", "") or ""
    if toi and str(toi).upper().strip().startswith("FCA"):
        data["invoice_toi"] = "FOB"
    
    if "items" in data and isinstance(data["items"], list):
        for item in data["items"]:
            desc = (item.get("item_description") or "").strip()
            part = (item.get("item_part_no") or "").strip()
            if part and part not in desc:
                item["item_description"] = f"{desc} {part}".strip()
            for key in list(item.keys()):
                item[key] = clean_value(item[key])
    
    for key in list(data.keys()):
        if key != "items":
            data[key] = clean_value(data[key])
    return data

def post_process_airway_bill(data: Dict[str, Any]) -> Dict[str, Any]:
    def clean_mawb(value):
        if not value: return None
        digits = "".join(filter(str.isdigit, str(value)))
        return digits if digits else None
    
    def clean_hawb(value):
        if not value: return None
        return "".join(filter(str.isalnum, str(value)))

    if "master_awb_no" in data: 
        data["master_awb_no"] = clean_mawb(data.get("master_awb_no"))
    if "house_awb_no" in data: 
        data["house_awb_no"] = clean_hawb(data.get("house_awb_no"))
    
    for field in ["pkg_in_qty", "gross_weight_kg", "chargeable_weight_kg", "total_frieght"]:
        if field in data and data[field] is not None:
            try:
                if isinstance(data[field], str):
                    val = data[field].replace(",", ".")
                    data[field] = float(val)
            except: pass
                
    for key in list(data.keys()):
        data[key] = clean_value(data[key])
    return data

# =============================================================================
# EXTRACTION FUNCTIONS (Adaptable)
# =============================================================================

async def extract_invoice(pdf_path: str, model_name: str) -> Tuple[Invoice, Optional[dict]]:
    """Extract invoice data with dynamic timeout."""
    try:
        pages = get_page_count(pdf_path)
        pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
        model = genai.GenerativeModel(model_name)
        config = get_generation_config(response_schema=Invoice)
        
        # Function accepts 'timeout' arg to be passed by the retry wrapper
        def _api_call(timeout):
            return model.generate_content(
                [INVOICE_PROMPT, pdf_file], 
                generation_config=config, 
                request_options={"timeout": timeout} 
            )
            
        response = execute_with_adaptable_retry(_api_call, "Invoice Extraction", pages)
        
        usage = {
            "prompt_token_count": response.usage_metadata.prompt_token_count,
            "candidates_token_count": response.usage_metadata.candidates_token_count,
            "total_token_count": response.usage_metadata.total_token_count
        } if response.usage_metadata else None
        
        invoice_obj = Invoice.model_validate_json(response.text)
        processed_data = post_process_invoice(invoice_obj.model_dump())
        return Invoice.model_validate(processed_data), usage

    except Exception as e:
        print(f"❌ Invoice Extraction Failed: {e}")
        return Invoice(), None


async def extract_airway_bill(pdf_path: str, model_name: str) -> Tuple[AirwayBill, Optional[dict]]:
    """Extract AWB data with dynamic timeout."""
    try:
        pages = get_page_count(pdf_path)
        pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
        model = genai.GenerativeModel(model_name)
        config = get_generation_config(response_schema=AirwayBill)
        
        def _api_call(timeout):
            return model.generate_content(
                [AWB_PROMPT, pdf_file], 
                generation_config=config, 
                request_options={"timeout": timeout} 
            )

        response = execute_with_adaptable_retry(_api_call, "AWB Extraction", pages)
        
        usage = {
            "prompt_token_count": response.usage_metadata.prompt_token_count,
            "candidates_token_count": response.usage_metadata.candidates_token_count,
            "total_token_count": response.usage_metadata.total_token_count
        } if response.usage_metadata else None

        awb_obj = AirwayBill.model_validate_json(response.text)
        processed_data = post_process_airway_bill(awb_obj.model_dump())
        return AirwayBill.model_validate(processed_data), usage

    except Exception as e:
        print(f"❌ AWB Extraction Failed: {e}")
        return AirwayBill(), None