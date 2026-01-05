import io
from pypdf import PdfReader

def validate_pdf(file_content: bytes) -> bool:
    try:
        reader = PdfReader(io.BytesIO(file_content))
        if len(reader.pages) > 0:
            return True
        return False
    except Exception:
        return False

def get_pdf_page_count(file_content: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(file_content))
        return len(reader.pages)
    except Exception:
        return 0
