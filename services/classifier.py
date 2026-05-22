"""
Document Classifier Service
===========================
Updated for 5-Way Specific Vendor Routing.
"""

from google import genai
from models.schemas import DocumentClassification
from models.gemini_config import get_generation_config, client
from utils.pdf_utils import get_pdf_page_count
from services.prompt_config import CLASSIFICATION_PROMPT

async def classify_documents(pdf_path: str, model_name: str = "gemini-2.5-flash") -> DocumentClassification:
    try:
        with open(pdf_path, "rb") as f:
            pdf_content = f.read()
        total_pages = get_pdf_page_count(pdf_content)
    except Exception:
        total_pages = 1000

    pdf_file = client.files.upload(file=pdf_path, config={"mime_type": "application/pdf"})
    config = get_generation_config(response_schema=DocumentClassification)
    
    try:
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[CLASSIFICATION_PROMPT, pdf_file],
            config=config
        )
        raw_class = DocumentClassification.model_validate_json(response.text)
        
        def clean_ranges(ranges_list):
            cleaned = []
            for r in ranges_list:
                valid = [p for p in r if isinstance(p, int) and 1 <= p <= total_pages]
                if valid: cleaned.append([min(valid), max(valid)])
            return cleaned

        # Clean ALL 7 categories
        raw_class.commin_invoices = clean_ranges(raw_class.commin_invoices)
        raw_class.type1_invoices = clean_ranges(raw_class.type1_invoices)
        raw_class.type2_invoices = clean_ranges(raw_class.type2_invoices)
        raw_class.crown_invoices = clean_ranges(raw_class.crown_invoices)
        raw_class.abb_invoices = clean_ranges(raw_class.abb_invoices)
        raw_class.invoices = clean_ranges(raw_class.invoices)
        raw_class.airway_bills = clean_ranges(raw_class.airway_bills)
        
        return raw_class

    except Exception as e:
        print(f"Classification Error: {e}")
        return DocumentClassification(invoices=[[1, total_pages]])