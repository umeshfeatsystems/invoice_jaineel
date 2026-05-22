import logging
import asyncio
import json
from google import genai
from google.genai import types
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable, InternalServerError
from services.rate_limiter import get_rate_limiter, release_rate_limit
from services.prompt_config import OCR_LOGIC_PROMPT
from models.gemini_config import get_http_options
from pypdf import PdfReader

logger = logging.getLogger("Invoice_refined")

# Create a global client instance
client = genai.Client()

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
            
            pdf_file = client.files.upload(file=pdf_path, config={"mime_type": "application/pdf"})
            
            prompt = "Transcribe ALL text from this document exactly as it appears. Maintain spatial layout where possible. Return ONLY the raw text."
            
            config = types.GenerateContentConfig(
                http_options=get_http_options(timeout)
            )
            
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=[prompt, pdf_file],
                config=config
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
        
        # We expect JSON output
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            http_options=get_http_options(30)
        )
        
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[OCR_LOGIC_PROMPT, raw_text],
            config=config
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
