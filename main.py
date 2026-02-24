"""
Split API System: Classification + Extraction
==============================================
Uses Gemini File Storage (48h retention)
"""

import os
import io
import time
import uuid
import logging
import asyncio
import aiofiles
import shutil
import re
import math
from typing import List, Optional, Dict, Any
from datetime import datetime

import uvicorn
import google.generativeai as genai
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pypdf import PdfReader
from dotenv import load_dotenv

from services.prompt_config import (
    INVOICE_PROMPT, 
    PO_PROMPT,
    GRN_PROMPT, 
    CLASSIFICATION_PROMPT
)
from services.rate_limiter import initialize_rate_limiter, get_rate_limiter, release_rate_limit

# ==========================================
# CONFIGURATION
# ==========================================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SplitAPI")

API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

CONFIG = {
    "CLASSIFIER_MODEL": "gemini-2.5-flash",
    "EXTRACTOR_MODEL": "gemini-2.5-pro",
    "CLASSIFICATION_TIMEOUT": 120,
    "BASE_TIMEOUT_SECONDS": 180,
    "TIMEOUT_PER_PAGE": 30,
    "MAX_TIMEOUT_SECONDS": 1200,
    "MAX_CONCURRENT_EXTRACTIONS": 2,
}

PRICING = {
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "output": 3.75},
    "default": {"input": 0.10, "output": 0.40}
}

INVOICE_EXTRA_GUARDRAILS = """
### INVOICE HALLUCINATION GUARDRAILS (STRICT)
- Use evidence only from the requested invoice page range. Never use values from other pages.
- Populate OPTIONAL fields only when the value is explicitly present with a matching label/context.
- If a value is missing, ambiguous, or unreadable, return null.
- Output key mapping:
  - seller_name -> name
  - seller_address -> vendor_addr
  - buyer_name -> billing_name
  - buyer_address -> billing_addr
  - invoice_number -> invoice_no
  - invoice_date -> date
  - invoice_currency -> currency
  - total_amount -> total
  - payment address / remit to -> payment_addr
  - email / e-mail -> vendor_email
  - vendor vat / vat no -> vendor_vat_no
  - buyer vat / billing vat -> billing_vat_no
  - ship to / shipping address -> shipping_addr
  - ship via / shipping method -> shipping_method
  - pan / pan no / permanent account number -> pan_no
  - msme / udyam / udyam aadhaar -> msme_number
- Optional field anchors:
  - `pan_no`: PAN No, PAN Number, Permanent Account Number
  - `msme_number`: MSME, UDYAM, Udyam Aadhaar
  - `payment_addr`: Payment Address, Remit To, Pay To
  - `vendor_email`: Email, E-mail
  - `shipping_method`: Ship Via, Shipping Method, Mode of Transport
- Never repeat one token across unrelated fields (GST/VAT/email/bank/HSN/part/line fields).
- Format checks:
  - vendor_gstin/billing_gstin: 15 alphanumeric chars.
  - pan_no: 10 chars (e.g. ABCDE1234F).
  - vendor_email: must contain '@'.
  - bank_details.swift_code: 8 or 11 alphanumeric chars.
  - bank_details.iban: starts with 2 letters + 2 digits.
  - items[*].hsn_code: 4-8 digits only.
- Do not fabricate line items. If the line-item table is absent, return an empty list.
"""

GLOBAL_EXTRA_GUARDRAILS = """
### UNIVERSAL ANTI-HALLUCINATION RULES (NON-NEGOTIABLE)
- Extract only values explicitly visible on the requested page range.
- Never infer, estimate, backfill, or copy values from nearby fields.
- If label/context is missing or ambiguous, return null for that field.
- Never reuse one value across multiple unrelated fields.
- For numeric fields, return null if the value is not explicitly shown.
"""

PO_EXTRA_GUARDRAILS = """
### PO HALLUCINATION GUARDRAILS
- Populate optional fields only when explicitly present with matching labels.
- `vendor_name`: extract only from explicit seller anchors (Vendor, Supplier, To, Ship From).
- Extract these PO fields only when explicitly labeled:
  - `purchase_order_expiry_date`: Purchase Order Expiry date / PO Expiry Date
  - `delivery_by_date`: Delivery by date / Deliver By Date
  - `vendor_address`: Vendor Address
  - `supplier_code`: Supplier Code
  - `billing_name`: Billing Name
  - `billing_address`: Billing Address
  - `delivery_name`: Delivery Name
  - `delivery_address`: Delivery Address
- For PO line items, use these exact additional keys when columns exist:
  - `delivery_dates` from Delivery Date / Delivery Dates
  - `units_of_measure` from Unit / UOM / Units Of Measure
  - `net_amounts` from Net Amount / Net Amounts
  - `tax_amounts` from Tax Amount / Tax Amounts
  - `tax_rate` from Tax Rate
- Do not synthesize line-level values from totals or vice versa.
- If any optional field is missing/unclear, return null.
"""

GRN_EXTRA_GUARDRAILS = """
### GRN OPTIONAL FINANCIAL FIELDS
- Extract these GRN fields only when explicitly labeled:
  - `merchant_address`: Merchant Address
  - `merchant_phone_number`: Merchant Phone Number
  - `total_amount`: Total Amount
  - `tax_amount`: Tax Amount
- For each GRN line item, extract financial values if explicitly present:
  - `unit_price` from labels: Unit Price, Rate, Price
  - `amount` from labels: Amount, Line Amount, Total
- Quantity labels such as `QTY RECEIVED`, `QTY ACCEPTED`, `REJECTED`, `QTY` are NEVER financial fields.
- Never map quantity values into `unit_price` or `amount`.
- Never derive `amount` from quantity when price/amount columns are absent.
- If GRN has no financial columns, return null for these fields.
"""

INVALID_OPTIONAL_LITERALS = {
    "",
    "null",
    "none",
    "n/a",
    "na",
    "-",
    "--",
    "not available",
    "not applicable",
    "string",
    "number",
    "integer",
    "float",
    "boolean",
}

GSTIN_PATTERN = re.compile(r"^\d{2}[A-Z0-9]{13}$")
EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)
HSN_PATTERN = re.compile(r"^\d{4,8}$")
IBAN_PATTERN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
SWIFT_PATTERN = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")
ROUTING_PATTERN = re.compile(r"^\d{5,12}$")
BANK_ACCOUNT_PATTERN = re.compile(r"^[A-Z0-9\-]{4,34}$", re.IGNORECASE)
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# ==========================================
# SCHEMAS
# ==========================================
class DocumentClassification(BaseModel):
    invoices: List[List[int]] = Field(default_factory=list)
    po: List[List[int]] = Field(default_factory=list)
    grn: List[List[int]] = Field(default_factory=list)

class ClassificationResponse(BaseModel):
    status: str
    gemini_file_name: str
    classification: DocumentClassification
    processing_time_seconds: float

class ExtractionRequest(BaseModel):
    gemini_file_name: str
    classification: DocumentClassification  # Pass classification from classify endpoint

class ExtractionResult(BaseModel):
    document_type: str 
    page_range: List[int]
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cost_usd: float = 0.0

class ExtractionResponse(BaseModel):
    status: str
    processing_time_seconds: float
    total_cost_usd: float
    results: List[ExtractionResult]

# ==========================================
# UTILITIES
# ==========================================
def get_pdf_page_count(file_content: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(file_content))
        return len(reader.pages)
    except:
        return 0

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

def _clean_optional_text(value: Any) -> Optional[str]:
    if value is None or not isinstance(value, str):
        return None if value is None else value
    text = value.strip()
    if text.lower() in INVALID_OPTIONAL_LITERALS:
        return None
    return text or None

def _normalize_token(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()

def _looks_like_repeated_placeholder(value: str) -> bool:
    compact = re.sub(r"[\s\-_/.,:;]", "", value).upper()
    if len(compact) < 6:
        return False
    has_alpha = any(ch.isalpha() for ch in compact)
    has_digit = any(ch.isdigit() for ch in compact)
    if not (has_alpha and has_digit):
        return False
    chunks = [c for c in value.split(" ") if c]
    return len(chunks) >= 3 and all(len(chunk) <= 4 for chunk in chunks)

def _collect_suspicious_invoice_tokens(data: Dict[str, Any]) -> set:
    token_counts: Dict[str, int] = {}

    def _add(value: Any):
        cleaned = _clean_optional_text(value)
        if not isinstance(cleaned, str):
            return
        token = _normalize_token(cleaned)
        token_counts[token] = token_counts.get(token, 0) + 1

    for key in [
        "po_no",
        "pan_no",
        "msme_number",
        "vendor_gstin",
        "billing_gstin",
        "vendor_vat_no",
        "billing_vat_no",
        "vendor_email",
        "shipping_method",
    ]:
        _add(data.get(key))

    bank = data.get("bank_details")
    if isinstance(bank, dict):
        for key in ["iban", "swift_code", "bank_routing_no", "bank_account_no"]:
            _add(bank.get(key))

    items = data.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ["line_no", "item_po_no", "part_no", "hsn_code"]:
                _add(item.get(key))

    return {
        token
        for token, count in token_counts.items()
        if count >= 4 and _looks_like_repeated_placeholder(token)
    }

def _null_if_suspicious(value: Any, suspicious_tokens: set) -> Any:
    if not isinstance(value, str):
        return value
    return None if _normalize_token(value) in suspicious_tokens else value

def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None

def _is_close(a: Optional[float], b: Optional[float], tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol

def _is_zero(value: Optional[float], tol: float = 1e-9) -> bool:
    return value is not None and abs(float(value)) <= tol

def _is_integerish(value: Optional[float], tol: float = 1e-6) -> bool:
    return value is not None and abs(float(value) - round(float(value))) <= tol

def _sanitize_invoice_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw_data)
    suspicious_tokens = _collect_suspicious_invoice_tokens(data)

    for key in [
        "vendor_addr",
        "billing_name",
        "billing_addr",
        "shipping_addr",
        "pan_no",
        "msme_number",
        "due_date",
        "shipping_date",
        "payment_terms",
        "payment_addr",
        "vendor_vat_no",
        "billing_vat_no",
        "shipping_method",
    ]:
        data[key] = _null_if_suspicious(_clean_optional_text(data.get(key)), suspicious_tokens)

    vendor_email = _null_if_suspicious(_clean_optional_text(data.get("vendor_email")), suspicious_tokens)
    data["vendor_email"] = vendor_email if isinstance(vendor_email, str) and EMAIL_PATTERN.fullmatch(vendor_email) else None

    for key in ["vendor_gstin", "billing_gstin"]:
        gst_val = _null_if_suspicious(_clean_optional_text(data.get(key)), suspicious_tokens)
        if isinstance(gst_val, str):
            compact = re.sub(r"[\s\-]", "", gst_val).upper()
            data[key] = compact if GSTIN_PATTERN.fullmatch(compact) else None
        else:
            data[key] = None

    pan_no = _null_if_suspicious(_clean_optional_text(data.get("pan_no")), suspicious_tokens)
    if isinstance(pan_no, str):
        compact = re.sub(r"\s+", "", pan_no).upper()
        data["pan_no"] = compact if PAN_PATTERN.fullmatch(compact) else None
    else:
        data["pan_no"] = None

    # Numeric sanitation for optional finance fields that tend to get placeholder hallucinations.
    for key in [
        "tax",
        "tax_rate",
        "sgst_percentage",
        "cgst_percentage",
        "igst_percentage",
        "sgst_total",
        "cgst_total",
        "igst_total",
        "net_amount",
        "discount",
        "shipping_charges",
        "total",
    ]:
        data[key] = _to_float_or_none(data.get(key))

    # If SGST/CGST are present, IGST should generally be absent on the same invoice.
    sgst_present = bool((data.get("sgst_percentage") or 0) > 0 or (data.get("sgst_total") or 0) > 0)
    cgst_present = bool((data.get("cgst_percentage") or 0) > 0 or (data.get("cgst_total") or 0) > 0)
    if sgst_present and cgst_present:
        data["igst_percentage"] = None
        data["igst_total"] = None

    # Detect repeated tiny placeholder values across unrelated optional numeric fields.
    placeholder_fields = ["tax_rate", "igst_percentage", "igst_total", "discount", "shipping_charges"]
    counts: Dict[float, int] = {}
    for key in placeholder_fields:
        value = data.get(key)
        if value is None:
            continue
        rounded = round(float(value), 4)
        counts[rounded] = counts.get(rounded, 0) + 1
    repeated_placeholders = {
        value for value, cnt in counts.items() if cnt >= 3 and 0 < value <= 5
    }
    if repeated_placeholders:
        for key in placeholder_fields:
            value = data.get(key)
            if value is not None and round(float(value), 4) in repeated_placeholders:
                data[key] = None

    # Derive tax_rate when tax + net_amount are present and extracted rate is missing/invalid.
    tax = data.get("tax")
    net_amount = data.get("net_amount")
    if tax is not None and net_amount is not None and net_amount > 0:
        computed_rate = round((tax / net_amount) * 100, 2)
        current_rate = data.get("tax_rate")
        if current_rate is None or abs(current_rate - computed_rate) > 0.5:
            data["tax_rate"] = computed_rate

    # Tax consistency checks to suppress unsupported/hallucinated values.
    tax = data.get("tax")
    if tax is not None:
        if tax < 0:
            data["tax"] = None
            data["tax_rate"] = None
        else:
            total = data.get("total")
            if total is not None and tax > (total * 0.6):
                data["tax"] = None
                data["tax_rate"] = None

    tax = data.get("tax")
    if tax is not None:
        component_keys = ["sgst_total", "cgst_total", "igst_total"]
        components = [data.get(k) for k in component_keys if data.get(k) is not None]
        if components:
            component_sum = round(sum(float(v) for v in components), 2)
            if abs(tax - component_sum) > 2.0:
                data["tax"] = component_sum if component_sum > 0 else None
                if data["tax"] is None:
                    data["tax_rate"] = None

    tax = data.get("tax")
    total = data.get("total")
    net_amount = data.get("net_amount")
    if tax is not None and total is not None and net_amount is not None:
        shipping = data.get("shipping_charges") or 0.0
        discount = data.get("discount") or 0.0
        expected_total = net_amount + tax + shipping - discount
        tolerance = max(2.0, round(total * 0.02, 2))
        if abs(total - expected_total) > tolerance:
            data["tax"] = None
            data["tax_rate"] = None

    bank = data.get("bank_details")
    if isinstance(bank, dict):
        bank_data = dict(bank)
        bank_data["bank_name"] = _clean_optional_text(bank_data.get("bank_name"))
        bank_data["bank_addr"] = _clean_optional_text(bank_data.get("bank_addr"))

        account_no = _null_if_suspicious(_clean_optional_text(bank_data.get("bank_account_no")), suspicious_tokens)
        if isinstance(account_no, str):
            compact = re.sub(r"\s+", "", account_no).upper()
            bank_data["bank_account_no"] = account_no if BANK_ACCOUNT_PATTERN.fullmatch(compact) else None
        else:
            bank_data["bank_account_no"] = None

        iban = _null_if_suspicious(_clean_optional_text(bank_data.get("iban")), suspicious_tokens)
        if isinstance(iban, str):
            compact = re.sub(r"\s+", "", iban).upper()
            bank_data["iban"] = compact if IBAN_PATTERN.fullmatch(compact) else None
        else:
            bank_data["iban"] = None

        swift = _null_if_suspicious(_clean_optional_text(bank_data.get("swift_code")), suspicious_tokens)
        if isinstance(swift, str):
            compact = re.sub(r"\s+", "", swift).upper()
            bank_data["swift_code"] = compact if SWIFT_PATTERN.fullmatch(compact) else None
        else:
            bank_data["swift_code"] = None

        routing = _null_if_suspicious(_clean_optional_text(bank_data.get("bank_routing_no")), suspicious_tokens)
        if isinstance(routing, str):
            compact = re.sub(r"\D", "", routing)
            bank_data["bank_routing_no"] = compact if ROUTING_PATTERN.fullmatch(compact) else None
        else:
            bank_data["bank_routing_no"] = None

        data["bank_details"] = bank_data if any(v is not None for v in bank_data.values()) else None
    else:
        data["bank_details"] = None

    items = data.get("items")
    if isinstance(items, list):
        cleaned_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_data = dict(item)
            for key in ["line_no", "item_po_no", "part_no", "hsn_code"]:
                item_data[key] = _null_if_suspicious(_clean_optional_text(item_data.get(key)), suspicious_tokens)

            line_no = item_data.get("line_no")
            if isinstance(line_no, str):
                trimmed = line_no.strip()
                if " " in trimmed or len(trimmed) > 10:
                    item_data["line_no"] = None

            hsn_code = item_data.get("hsn_code")
            if isinstance(hsn_code, str):
                compact = re.sub(r"[.\s]", "", hsn_code)
                item_data["hsn_code"] = compact if HSN_PATTERN.fullmatch(compact) else None

            cleaned_items.append(item_data)
        data["items"] = cleaned_items

    return data

def _sanitize_po_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw_data)

    data["payment_terms"] = _clean_optional_text(data.get("payment_terms"))
    data["vendor_name"] = _clean_optional_text(data.get("vendor_name")) or _clean_optional_text(data.get("supplier_name"))
    data["currency"] = _clean_optional_text(data.get("currency")) or data.get("currency")
    data["po_number"] = _clean_optional_text(data.get("po_number")) or data.get("po_number")
    data["date"] = _clean_optional_text(data.get("date")) or data.get("date")
    for key in [
        "purchase_order_expiry_date",
        "delivery_by_date",
        "vendor_address",
        "supplier_code",
        "billing_name",
        "billing_address",
        "delivery_name",
        "delivery_address",
    ]:
        data[key] = _clean_optional_text(data.get(key))
    data["total_amount"] = _to_float_or_none(data.get("total_amount"))

    items = data.get("items")
    if isinstance(items, list):
        cleaned_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_data = dict(item)
            item_data["description"] = _clean_optional_text(item_data.get("description")) or item_data.get("description")
            item_data["product_code"] = _clean_optional_text(item_data.get("product_code"))
            item_data["line_number"] = _to_float_or_none(item_data.get("line_number"))
            if item_data["line_number"] is not None and item_data["line_number"] <= 0:
                item_data["line_number"] = None
            item_data["unit_price"] = _to_float_or_none(item_data.get("unit_price"))
            item_data["quantity"] = _to_float_or_none(item_data.get("quantity"))
            item_data["line_amount"] = _to_float_or_none(item_data.get("line_amount"))
            item_data["delivery_dates"] = _clean_optional_text(item_data.get("delivery_dates"))
            item_data["units_of_measure"] = _clean_optional_text(item_data.get("units_of_measure"))
            for key in ["net_amounts", "tax_amounts", "tax_rate"]:
                item_data[key] = _to_float_or_none(item_data.get(key))
                if item_data[key] is not None and item_data[key] < 0:
                    item_data[key] = None
            cleaned_items.append(item_data)
        data["items"] = cleaned_items

    return data

def _sanitize_grn_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw_data)

    for key in [
        "grn_number",
        "po_reference",
        "supplier_name",
        "merchant_address",
        "merchant_phone_number",
    ]:
        data[key] = _clean_optional_text(data.get(key))

    date_received = _clean_optional_text(data.get("date_received"))
    if isinstance(date_received, str) and date_received in {"1970-01-01", "1900-01-01", "0001-01-01", "0000-00-00"}:
        date_received = None
    data["date_received"] = date_received
    data["total_amount"] = _to_float_or_none(data.get("total_amount"))
    if data["total_amount"] is not None and data["total_amount"] < 0:
        data["total_amount"] = None
    data["tax_amount"] = _to_float_or_none(data.get("tax_amount"))
    if data["tax_amount"] is not None and data["tax_amount"] < 0:
        data["tax_amount"] = None

    items = data.get("items")
    if isinstance(items, list):
        cleaned_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_data = dict(item)
            item_data["item_description"] = _clean_optional_text(item_data.get("item_description"))
            item_data["item_part_no"] = _clean_optional_text(item_data.get("item_part_no"))

            for key in ["qty_ordered", "qty_received", "unit_price", "amount"]:
                item_data[key] = _to_float_or_none(item_data.get(key))
                if item_data[key] is not None and item_data[key] < 0:
                    item_data[key] = None

            # `0` in optional financial fields is often a hallucinated fallback in quantity-only GRNs.
            if _is_zero(item_data.get("unit_price")):
                item_data["unit_price"] = None

            cleaned_items.append(item_data)

        # Strong anti-hallucination pass for GRN financials:
        # if financial columns are missing, models often copy qty values into amount.
        amount_rows = 0
        mirrored_amount_rows = 0
        has_positive_unit_price = False
        has_positive_amount = False

        for item_data in cleaned_items:
            qty_ordered = item_data.get("qty_ordered")
            qty_received = item_data.get("qty_received")
            unit_price = item_data.get("unit_price")
            amount = item_data.get("amount")

            if amount is not None:
                amount_rows += 1
                mirrors_qty_ordered = _is_close(amount, qty_ordered)
                mirrors_qty_received = _is_close(amount, qty_received)
                if mirrors_qty_ordered or mirrors_qty_received:
                    mirrored_amount_rows += 1
                    # If amount mirrors quantity, it's likely not a true financial column.
                    if unit_price is None or _is_close(unit_price, amount):
                        item_data["amount"] = None
                    if unit_price is not None and _is_close(unit_price, amount):
                        item_data["unit_price"] = None
                    if mirrors_qty_ordered and qty_received is not None and not _is_close(qty_ordered, qty_received):
                        item_data["qty_ordered"] = None

            if item_data.get("unit_price") is not None and item_data["unit_price"] > 0:
                has_positive_unit_price = True
            if item_data.get("amount") is not None and item_data["amount"] > 0:
                has_positive_amount = True

        # If all financials are absent/zero, force both optional financial fields to null.
        if not has_positive_unit_price and not has_positive_amount:
            for item_data in cleaned_items:
                item_data["unit_price"] = None
                item_data["amount"] = None
        else:
            # If amounts mostly mirror qty and there is no positive price evidence, treat as hallucinated.
            mirror_ratio = (mirrored_amount_rows / amount_rows) if amount_rows else 0.0
            if mirror_ratio >= 0.67 and not has_positive_unit_price:
                for item_data in cleaned_items:
                    item_data["amount"] = None

            # If amount == unit_price across rows but multiplication checks fail,
            # treat these as repeated placeholders and drop financials.
            rows_with_financial = 0
            rows_with_equal_price_amount = 0
            rows_with_math_support = 0
            for item_data in cleaned_items:
                qty_ordered = item_data.get("qty_ordered")
                qty_received = item_data.get("qty_received")
                unit_price = item_data.get("unit_price")
                amount = item_data.get("amount")

                if unit_price is None and amount is None:
                    continue
                rows_with_financial += 1

                if unit_price is not None and amount is not None and _is_close(unit_price, amount):
                    rows_with_equal_price_amount += 1

                supported = False
                if unit_price is not None and amount is not None:
                    for qty in [qty_received, qty_ordered]:
                        if qty is None:
                            continue
                        expected = unit_price * qty
                        tol = max(0.05, abs(expected) * 0.02)
                        if abs(amount - expected) <= tol:
                            supported = True
                            break
                    if not supported and _is_close(amount, unit_price):
                        # qty==1 is one valid case where amount can equal unit_price.
                        if _is_close(qty_received, 1.0) or _is_close(qty_ordered, 1.0):
                            supported = True

                if supported:
                    rows_with_math_support += 1

            if rows_with_financial >= 2:
                equal_ratio = rows_with_equal_price_amount / rows_with_financial
                support_ratio = rows_with_math_support / rows_with_financial
                if equal_ratio >= 0.67 and support_ratio < 0.34:
                    for item_data in cleaned_items:
                        item_data["unit_price"] = None
                        item_data["amount"] = None
                        qty_ordered = item_data.get("qty_ordered")
                        qty_received = item_data.get("qty_received")
                        if (
                            qty_ordered is not None
                            and qty_received is not None
                            and not _is_integerish(qty_ordered)
                            and _is_integerish(qty_received)
                            and qty_received > 1
                            and not _is_close(qty_ordered, qty_received)
                        ):
                            item_data["qty_ordered"] = None

        data["items"] = cleaned_items

    return data

def resolve_refs(schema, defs=None):
    if defs is None: 
        defs = schema.get("$defs", {}) or schema.get("definitions", {})
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            name = ref.split("/")[-1]
            if name in defs: 
                return resolve_refs(defs[name], defs)
        new_schema = {}
        for k, v in schema.items():
            if k in ["$defs", "definitions"]: 
                continue
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
                    if opt.get("type") != "null": 
                        return _clean(opt)
            for key in ["default", "title", "$defs", "definitions"]:
                if key in s: 
                    del s[key]
            for key, value in s.items(): 
                s[key] = _clean(value)
        elif isinstance(s, list):
            for i, item in enumerate(s): 
                s[i] = _clean(item)
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
        except:
            config["response_schema"] = response_schema
    return config

# ==========================================
# 3. PYDANTIC SCHEMAS (From Original main.py)
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
    pan_no: Optional[str] = Field(None, description="PAN Number")
    msme_number: Optional[str] = Field(None, description="MSME/Udyam Aadhaar Number")
    billing_name: Optional[str] = Field(None, description="Billing Name")
    billing_addr: Optional[str] = Field(None, description="Billing Address")
    shipping_addr: Optional[str] = Field(None, description="Shipping Address")
    invoice_no: str = Field(..., description="Invoice Number")
    po_no: Optional[str] = Field(None, description="Purchase Order Number")
    vendor_gstin: Optional[str] = Field(None, description="Vendor GST Number")
    billing_gstin: Optional[str] = Field(None, description="Billing/Customer GST Number")
    vendor_vat_no: Optional[str] = Field(None, description="Vendor VAT Number")
    billing_vat_no: Optional[str] = Field(None, description="Billing VAT Number")
    date: str = Field(..., description="Document Date (YYYY-MM-DD)")
    due_date: Optional[str] = Field(None, description="Due Date (YYYY-MM-DD)")
    shipping_date: Optional[str] = Field(None, description="Shipping Date (YYYY-MM-DD)")
    payment_terms: Optional[str] = Field(None, description="Payment Terms")
    payment_addr: Optional[str] = Field(None, description="Payment Address")
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

# --- PO & GRN MODELS ---
class POItem(BaseModel):
    line_number: Optional[float] = None
    description: str
    product_code: Optional[str] = None
    unit_price: float
    quantity: float
    line_amount: float
    delivery_dates: Optional[str] = Field(None, description="Delivery Dates")
    units_of_measure: Optional[str] = Field(None, description="Units Of Measure")
    net_amounts: Optional[float] = Field(None, description="Net Amounts")
    tax_amounts: Optional[float] = Field(None, description="Tax Amounts")
    tax_rate: Optional[float] = Field(None, description="Tax Rate")

class PurchaseOrder(BaseModel):
    po_number: str
    date: str
    vendor_name: Optional[str] = None
    purchase_order_expiry_date: Optional[str] = Field(None, description="Purchase Order Expiry date")
    delivery_by_date: Optional[str] = Field(None, description="Delivery by date")
    vendor_address: Optional[str] = Field(None, description="Vendor Address")
    supplier_code: Optional[str] = Field(None, description="Supplier Code")
    billing_name: Optional[str] = Field(None, description="Billing Name")
    billing_address: Optional[str] = Field(None, description="Billing Address")
    delivery_name: Optional[str] = Field(None, description="Delivery Name")
    delivery_address: Optional[str] = Field(None, description="Delivery Address")
    currency: str
    total_amount: float
    payment_terms: Optional[str] = None
    items: List[POItem] = Field(default_factory=list)

class GRNItem(BaseModel):
    item_description: Optional[str] = None
    item_part_no: Optional[str] = None
    qty_ordered: Optional[float] = None
    qty_received: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None

class GoodsReceivedNote(BaseModel):
    grn_number: Optional[str] = None
    date_received: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    merchant_address: Optional[str] = Field(None, description="Merchant Address")
    merchant_phone_number: Optional[str] = Field(None, description="Merchant Phone Number")
    total_amount: Optional[float] = Field(None, description="Total Amount")
    tax_amount: Optional[float] = Field(None, description="Tax Amount")
    items: List[GRNItem] = Field(default_factory=list)

# ==========================================
# CLASSIFICATION SERVICE
# ==========================================
async def classify_document(gemini_file, total_pages: int) -> DocumentClassification:
    model = genai.GenerativeModel(CONFIG["CLASSIFIER_MODEL"])
    config = get_generation_config(response_schema=DocumentClassification)
    
    try:
        await get_rate_limiter().acquire(CONFIG["CLASSIFIER_MODEL"])
        response = await model.generate_content_async(
            [CLASSIFICATION_PROMPT, gemini_file],
            generation_config=config,
            request_options={"timeout": CONFIG["CLASSIFICATION_TIMEOUT"]}
        )
        result = DocumentClassification.model_validate_json(response.text)
        
        # If no classification found, default to full document as invoice
        if not result.invoices and not result.po and not result.grn:
            logger.warning("No classification found, defaulting to invoice")
            result.invoices = [[1, total_pages]]
        
        return result
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        # Fallback: treat as invoice
        return DocumentClassification(invoices=[[1, total_pages]])
    finally:
        release_rate_limit(CONFIG["CLASSIFIER_MODEL"])

# ==========================================
# EXTRACTION SERVICE
# ==========================================
async def extract_document_chunk(gemini_file, doc_type: str, page_range: List[int]) -> ExtractionResult:
    start_time = time.time()
    
    if doc_type == "po":
        prompt = PO_PROMPT + "\n" + GLOBAL_EXTRA_GUARDRAILS + "\n" + PO_EXTRA_GUARDRAILS
        schema = PurchaseOrder
    elif doc_type == "grn":
        prompt = GRN_PROMPT + "\n" + GLOBAL_EXTRA_GUARDRAILS + "\n" + GRN_EXTRA_GUARDRAILS
        schema = GoodsReceivedNote
    else:
        prompt = INVOICE_PROMPT + "\n" + GLOBAL_EXTRA_GUARDRAILS + "\n" + INVOICE_EXTRA_GUARDRAILS
        schema = Invoice

    num_pages = page_range[1] - page_range[0] + 1
    timeout = calculate_timeout_for_pages(num_pages)
    
    # Add page range instruction to prompt
    page_instruction = f"\n\nIMPORTANT: Extract data ONLY from pages {page_range[0]} to {page_range[1]} of this document. Ignore all other pages."
    full_prompt = prompt + page_instruction
    
    try:
        await get_rate_limiter().acquire(CONFIG["EXTRACTOR_MODEL"])
        
        model = genai.GenerativeModel(CONFIG["EXTRACTOR_MODEL"])
        config = get_generation_config(response_schema=schema)
        
        response = await model.generate_content_async(
            [full_prompt, gemini_file],
            generation_config=config,
            request_options={"timeout": timeout}
        )
        
        usage = None
        if response.usage_metadata:
            usage = {
                "prompt_token_count": response.usage_metadata.prompt_token_count,
                "candidates_token_count": response.usage_metadata.candidates_token_count,
            }
        
        data_obj = schema.model_validate_json(response.text)
        data_payload = data_obj.model_dump()
        if doc_type == "invoice":
            data_payload = _sanitize_invoice_data(data_payload)
            data_obj = Invoice.model_validate(data_payload)
        elif doc_type == "po":
            data_payload = _sanitize_po_data(data_payload)
            data_obj = PurchaseOrder.model_validate(data_payload)
        elif doc_type == "grn":
            data_payload = _sanitize_grn_data(data_payload)
            data_obj = GoodsReceivedNote.model_validate(data_payload)
        cost = calculate_cost(
            CONFIG["EXTRACTOR_MODEL"], 
            usage["prompt_token_count"] if usage else 0, 
            usage["candidates_token_count"] if usage else 0
        )
        
        return ExtractionResult(
            document_type=doc_type,
            page_range=page_range,
            data=data_obj.model_dump(),
            cost_usd=cost,
        )
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return ExtractionResult(
            document_type=doc_type,
            page_range=page_range,
            error=str(e),
        )
    finally:
        release_rate_limit(CONFIG["EXTRACTOR_MODEL"])

# ==========================================
# FASTAPI APP
# ==========================================
app = FastAPI(title="Invoice Extraction API (Split)", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    initialize_rate_limiter(
        model_limits={
            "gemini-2.5-flash": 1000, 
            "gemini-2.5-pro": 150
        },
        safety_margin=0.7,
        min_request_gap_ms=500,
        max_concurrent_per_model=2
    )

# ==========================================
# API 1: CLASSIFICATION
# ==========================================
@app.post("/api/v1/classify", response_model=ClassificationResponse)
async def classify_endpoint(file: UploadFile = File(...)):
    """
    Step 1: Upload PDF to Gemini, classify, return file reference
    """
    start = time.time()
    temp_path = None
    
    try:
        # Read file
        content = await file.read()
        total_pages = get_pdf_page_count(content)
        
        # Save temporarily (use current directory for Windows compatibility)
        temp_path = f"{uuid.uuid4()}.pdf"
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(content)
        
        # Upload to Gemini (stored for 48h)
        gemini_file = genai.upload_file(temp_path, mime_type="application/pdf")
        
        # Classify
        classification = await classify_document(gemini_file, total_pages)
        
        # Clean ranges
        def clean_ranges(ranges_list):
            cleaned = []
            for r in ranges_list:
                valid = [p for p in r if isinstance(p, int) and 1 <= p <= total_pages]
                if valid: 
                    cleaned.append([min(valid), max(valid)])
            return cleaned

        classification.invoices = clean_ranges(classification.invoices)
        classification.po = clean_ranges(classification.po)
        classification.grn = clean_ranges(classification.grn)
        
        # If all empty after cleaning, default to invoice
        if not classification.invoices and not classification.po and not classification.grn:
            logger.warning("All classifications empty after cleaning, defaulting to invoice")
            classification.invoices = [[1, total_pages]]
        
        return ClassificationResponse(
            status="success",
            gemini_file_name=gemini_file.name,  # e.g. "files/abc123"
            classification=classification,
            processing_time_seconds=time.time() - start
        )
        
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass

# ==========================================
# API 2: EXTRACTION
# ==========================================
@app.post("/api/v1/extract", response_model=ExtractionResponse)
async def extract_endpoint(request: ExtractionRequest):
    """
    Step 2: Use Gemini file reference to extract data
    """
    start = time.time()
    
    try:
        # Get file from Gemini using file name
        gemini_file = genai.get_file(request.gemini_file_name)
        
        # Use classification passed from frontend
        classification = request.classification
        
        # Prepare extraction tasks
        tasks = []
        for r in classification.invoices:
            if len(r) >= 2:  # Validate range has start and end
                tasks.append(extract_document_chunk(gemini_file, "invoice", r))
        for r in classification.po:
            if len(r) >= 2:
                tasks.append(extract_document_chunk(gemini_file, "po", r))
        for r in classification.grn:
            if len(r) >= 2:
                tasks.append(extract_document_chunk(gemini_file, "grn", r))
        
        # If no valid tasks, return empty results
        if not tasks:
            return ExtractionResponse(
                status="success",
                processing_time_seconds=time.time() - start,
                total_cost_usd=0.0,
                results=[]
            )
        
        # Extract in parallel
        results = await asyncio.gather(*tasks)
        
        total_cost = sum(r.cost_usd for r in results)
        
        return ExtractionResponse(
            status="success",
            processing_time_seconds=time.time() - start,
            total_cost_usd=round(total_cost, 6),
            results=results
        )
        
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Run with: uvicorn main:app --host 0.0.0.0 --port 5612 --reload
