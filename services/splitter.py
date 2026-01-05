import os
from pypdf import PdfReader, PdfWriter
from typing import List, Tuple

def split_pdf(original_pdf_path: str, ranges: List[List[int]], output_dir: str, prefix: str) -> List[str]:
    """
    Splits a PDF into multiple files based on page ranges.
    ranges: List of [start_page, end_page] (1-based indexing from Gemini, need to convert to 0-based)
    Returns list of paths to split PDFs.
    """
    reader = PdfReader(original_pdf_path)
    output_paths = []
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    for i, (start, end) in enumerate(ranges):
        writer = PdfWriter()
        # Gemini returns 1-based indexing usually.
        # pypdf uses 0-based.
        # So start-1 to end.
        
        # Validate bounds
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
