import os
import random
import numpy as np
from pdf2image import convert_from_path
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

# Configuration
INPUT_FOLDERS = ["inv-grn", "inv-grn-po", "inv-po"]
OUTPUT_ROOT = "scanned_dataset"

def apply_scanner_noise(image):
    """
    Applies realistic scanner artifacts to an image.
    """
    # 1. Convert to Grayscale (Most scans are B&W or Grayscale)
    img = image.convert("L")

    # 2. Resize / Resample (Simulate lower DPI scan)
    # Scale down to 70% then back up to lose detail
    w, h = img.size
    img = img.resize((int(w * 0.7), int(h * 0.7)), resample=Image.BICUBIC)
    img = img.resize((w, h), resample=Image.BICUBIC)

    # 3. Rotation / Skew (Paper is never perfectly straight)
    angle = random.uniform(-1.5, 1.5)  # Slight rotation between -1.5 and 1.5 degrees
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)

    # 4. Blur (Simulate scanner focus issues)
    img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.3, 0.8)))

    # 5. Noise (Salt and Pepper / Grain)
    # Convert to numpy array to add noise efficiently
    img_array = np.array(img)
    
    # Generate random noise
    noise = np.random.normal(0, 10, img_array.shape) # Mean 0, Std Dev 10
    noisy_img_array = img_array + noise
    
    # Clip values to 0-255 range and convert back to uint8
    noisy_img_array = np.clip(noisy_img_array, 0, 255).astype(np.uint8)
    img = Image.fromarray(noisy_img_array)

    # 6. Contrast / Brightness (Scanners often blow out highlights)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(random.uniform(1.2, 1.5)) # Increase contrast
    
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(random.uniform(1.0, 1.1)) # Slight brightness bump

    return img

def process_pdfs():
    # Create output root if it doesn't exist
    if not os.path.exists(OUTPUT_ROOT):
        os.makedirs(OUTPUT_ROOT)

    for folder in INPUT_FOLDERS:
        input_path = os.path.join(os.getcwd(), folder)
        output_path = os.path.join(os.getcwd(), OUTPUT_ROOT, folder)

        # Check if input folder exists
        if not os.path.exists(input_path):
            print(f"Skipping '{folder}' - Folder not found.")
            continue

        # Create corresponding output folder
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        print(f"Processing folder: {folder}...")

        files = [f for f in os.listdir(input_path) if f.lower().endswith('.pdf')]
        
        for filename in files:
            file_path = os.path.join(input_path, filename)
            
            try:
                # Convert PDF pages to images (300 DPI for high quality base)
                pages = convert_from_path(file_path, dpi=200)
                
                noisy_pages = []
                for page in pages:
                    noisy_page = apply_scanner_noise(page)
                    noisy_pages.append(noisy_page)

                # Save back as PDF
                output_file = os.path.join(output_path, filename)
                
                if noisy_pages:
                    noisy_pages[0].save(
                        output_file, "PDF", resolution=100.0, save_all=True, append_images=noisy_pages[1:]
                    )
                    print(f"  -> Converted: {filename}")
            
            except Exception as e:
                print(f"  -> Error processing {filename}: {e}")
                print("     (Make sure Poppler is installed and added to PATH)")

    print("\nProcessing Complete! Check the 'scanned_dataset' folder.")

if __name__ == "__main__":
    process_pdfs()