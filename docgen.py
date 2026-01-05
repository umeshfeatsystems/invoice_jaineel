import os
import random
from PyPDF2 import PdfMerger

# --- CONFIGURATION ---
# Parent folder name
PARENT_DIR = "scanned_dataset"

# Map the subfolders strictly as you requested
SOURCE_FOLDERS = {
    "invoice": os.path.join(PARENT_DIR, "inv-grn-po"),  # Folder with Invoices
    "grn":     os.path.join(PARENT_DIR, "inv-grn"),     # Folder with GRNs
    "po":      os.path.join(PARENT_DIR, "inv-po")       # Folder with POs
}

OUTPUT_DIR = "merged_output"
NUM_PACKETS = 5  # Generates 5 of each type (15 total)

def get_files_from_folder(folder_path):
    """Returns a list of full paths to PDF files in a folder."""
    if not os.path.exists(folder_path):
        print(f"Warning: Folder '{folder_path}' not found.")
        return []
    # Case-insensitive check for .pdf
    return [os.path.join(folder_path, f) for f in os.listdir(folder_path) if f.lower().endswith('.pdf')]

def create_packet(file_list, output_name):
    """Merges a list of file paths into a single PDF."""
    merger = PdfMerger()
    try:
        for file_path in file_list:
            merger.append(file_path)
        
        merger.write(output_name)
        print(f"Generated: {output_name}")
    except Exception as e:
        print(f"Error creating {output_name}: {e}")
    finally:
        merger.close()

def main():
    # Create output directory if it doesn't exist
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    print(f"Scanning parent folder: '{PARENT_DIR}'...")

    # 1. Load files from the specific subfolders
    invoices = get_files_from_folder(SOURCE_FOLDERS["invoice"])
    grns = get_files_from_folder(SOURCE_FOLDERS["grn"])
    pos = get_files_from_folder(SOURCE_FOLDERS["po"])

    # Validate inputs
    if not invoices:
        print(f"Error: No invoice PDFs found in '{SOURCE_FOLDERS['invoice']}'. Check your folder structure.")
        return

    print(f"Found: {len(invoices)} Invoices, {len(grns)} GRNs, {len(pos)} POs.\n")

    # --- Packet Type 1: Invoice + GRN ---
    if grns:
        print("--- Creating Invoice + GRN Packets ---")
        for i in range(1, NUM_PACKETS + 1):
            files_to_merge = [random.choice(invoices), random.choice(grns)]
            output_filename = os.path.join(OUTPUT_DIR, f"Packet_Inv_GRN_{i}.pdf")
            create_packet(files_to_merge, output_filename)
    else:
        print("Skipping Inv+GRN (No GRN files found)")

    # --- Packet Type 2: Invoice + PO ---
    if pos:
        print("\n--- Creating Invoice + PO Packets ---")
        for i in range(1, NUM_PACKETS + 1):
            files_to_merge = [random.choice(invoices), random.choice(pos)]
            output_filename = os.path.join(OUTPUT_DIR, f"Packet_Inv_PO_{i}.pdf")
            create_packet(files_to_merge, output_filename)
    else:
        print("Skipping Inv+PO (No PO files found)")

    # --- Packet Type 3: Invoice + GRN + PO ---
    if grns and pos:
        print("\n--- Creating Invoice + GRN + PO Packets ---")
        for i in range(1, NUM_PACKETS + 1):
            # Order: Invoice -> GRN -> PO
            files_to_merge = [
                random.choice(invoices), 
                random.choice(grns), 
                random.choice(pos)
            ]
            output_filename = os.path.join(OUTPUT_DIR, f"Packet_Inv_GRN_PO_{i}.pdf")
            create_packet(files_to_merge, output_filename)

if __name__ == "__main__":
    main()