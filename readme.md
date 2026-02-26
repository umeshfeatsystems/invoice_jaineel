# Intelligent Document Extraction API

A high-performance, asynchronous REST API for the automated classification and data extraction of Commercial Invoices, Purchase Orders (POs), and Goods Received Notes (GRNs). Built on FastAPI and powered by Google's Gemini LLMs (2.5-Flash and 3.0-Pro), this system utilizes a two-phase "Split API" architecture (Classify → Extract) with dynamic timeouts, bulletproof rate limiting, and aggressive anti-hallucination sanitization.

---

## 🏗️ System Architecture

- **Phase 1: Classification (`/api/v1/classify`)** — Uploads the PDF to Google Gemini File Storage, identifies document boundaries, and maps page ranges to specific document types (Invoices, POs, GRNs) using `gemini-2.5-flash`.
- **Phase 2: Extraction (`/api/v1/extract`)** — Processes specific page chunks concurrently using heavy-duty models (`gemini-3-pro-preview` / `gemini-2.5-pro`). Implements complex structured Pydantic schemas, Python-based post-processing, and fallback GSTIN extraction.

---

## 📋 Prerequisites

- **Python 3.9+**
- **Node.js & npm** (Required for PM2 process management in production)
- **Google Gemini API Key**

---

## ⚙️ Installation & Setup

**1. Clone the repository and navigate to the directory:**
```bash
git clone <repository-url>
cd <project-directory>
```

**2. Create and activate a virtual environment:**
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate
```

**3. Install Python dependencies:**
```bash
pip install -r requirements.txt
```

**4. Configure Environment Variables:**

Create a `.env` file in the root directory and add your Gemini API key:
```env
GEMINI_API_KEY=your_actual_gemini_api_key_here
LOG_LEVEL=INFO
```

---

## 🚀 Running the Application

### Development Mode (Standard)

To run the server locally with hot-reloading enabled:
```bash
uvicorn main:app --host 0.0.0.0 --port 5612 --reload
```

The API will be accessible at `http://localhost:5612`. Swagger UI documentation is available at `http://localhost:5612/docs`.

### Production Mode (PM2)

For production environments, use PM2 to ensure high availability, automatic restarts, and process monitoring.

**1. Install PM2 globally:**
```bash
npm install -g pm2
```

**2. Start the application via the ecosystem file:**
```bash
pm2 start ecosystem.config.js
```

**3. PM2 Management Commands:**
```bash
pm2 status                  # View running processes
pm2 logs invoice-grn-api    # View real-time application logs
pm2 save                    # Save process list to start on server boot
pm2 restart invoice-grn-api # Restart the service
```

---

## 🔌 API Integration Flow

### 1. Classify Document

- **Endpoint:** `POST /api/v1/classify`
- **Content-Type:** `multipart/form-data`
- **Payload:** `file` (PDF Document)

**Response:** Returns a `gemini_file_name` reference and a `classification` mapping object containing page ranges for `invoices`, `po`, and `grn`.

### 2. Extract Data

- **Endpoint:** `POST /api/v1/extract`
- **Content-Type:** `application/json`
- **Payload:** Pass the exact response objects from Phase 1.
```json
{
  "gemini_file_name": "files/abc123xyz",
  "classification": {
    "invoices": [[1, 2]],
    "po": [[3, 3]],
    "grn": []
  }
}
```

**Response:** Returns an array of `ExtractionResult` objects containing heavily sanitized, strongly-typed JSON data matching the Pydantic schemas defined in `models/schemas.py`.


## 🛠️ Included Utilities

- **Dataset Generation (`docgen.py`):** Merges individual PDFs from `inv-grn-po`, `inv-grn`, and `inv-po` subfolders to create multi-document test packets.
- **Noise Simulation (`dgen.py`):** Converts clean PDFs into images, applies realistic scanner artifacts (salt & pepper noise, Gaussian blur, skew, contrast manipulation), and converts them back to PDFs to stress-test the OCR capabilities.