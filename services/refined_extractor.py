import logging
import asyncio
import json
import google.generativeai as genai
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable, InternalServerError
from services.rate_limiter import get_rate_limiter, release_rate_limit
from services.prompt_config import OCR_LOGIC_PROMPT
from pypdf import PdfReader

logger = logging.getLogger("Invoice_refined")

def get_page_count(pdf_path: str) -> int:
    """Reads PDF page count efficiently."""
    try:
        with open(pdf_path, 'rb') as f:
            reader = PdfReader(f)
            return len(reader.pages)
    except Exception:
        return 10 # Fallback

async def get_raw_text_from_pdf(pdf_path: str, model_name: str = "gemini-2.5-flash") -> str:
    """
    Step 1: Vision Agent. 
    Transcribe the PDF to raw text using a Vision Model.
    Includes Dynamic Timeout & Retry Logic for large files.
    """
    # 1. Calculate Dynamic Timeout
    # Base 60s + 10s per page (e.g., 32 pages -> 380s)
    page_count = get_page_count(pdf_path)
    timeout = 60 + (page_count * 10)
    
    MAX_RETRIES = 3

    for attempt in range(MAX_RETRIES):
        try:
            await get_rate_limiter().acquire(model_name)
            
            pdf_file = genai.upload_file(pdf_path, mime_type="application/pdf")
            model = genai.GenerativeModel(model_name)
            
            prompt = "Transcribe ALL text from this document exactly as it appears. Maintain spatial layout where possible. Return ONLY the raw text."
            
            response = await model.generate_content_async(
                [prompt, pdf_file],
                request_options={"timeout": timeout}
            )
            return response.text
            
        except (DeadlineExceeded, ServiceUnavailable, InternalServerError) as e:
            logger.warning(f"Vision Agent Timeout/Error (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 * (attempt + 1))
            else:
                logger.error(f"Vision Agent Failed after {MAX_RETRIES} attempts.")
        except Exception as e:
            logger.error(f"Vision Agent Critical Error: {e}")
            return ""
        finally:
            release_rate_limit(model_name)
            
    return ""


async def extract_header_from_text(raw_text: str, model_name: str = "gemini-2.5-flash") -> dict:
    """
    Step 2: Logic Agent.
    Extract structured header fields from raw text.
    """
    if not raw_text:
        return {}

    try:
        await get_rate_limiter().acquire(model_name)
        
        model = genai.GenerativeModel(model_name)
        
        # We expect JSON output
        config = genai.GenerationConfig(response_mime_type="application/json")
        
        response = await model.generate_content_async(
            [OCR_LOGIC_PROMPT, raw_text],
            generation_config=config,
            request_options={"timeout": 30}
        )
        
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Logic Agent Failed: {e}")
        return {}
    finally:
        release_rate_limit(model_name)


async def run_header_ocr_flow(pdf_path: str) -> dict:
    """
    Orchestrator for the 2-Stage Header Extraction.
    Returns a dictionary with keys: invoice_toi, invoice_po_date, item_mfg_name, item_mfg_addr
    """
    # 1. Vision Agent
    raw_text = await get_raw_text_from_pdf(pdf_path)
    
    if not raw_text:
        return {}

    # 2. Logic Agent
    header_data = await extract_header_from_text(raw_text)
    
    logger.info(f"Header Extraction Result: {header_data}")
    return header_data
