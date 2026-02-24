"""
Standardized JSON-based Prompt Configuration System
====================================================
Scope: Commercial Invoices, Purchase Orders (PO), and Goods Received Notes (GRN).
Generic / Universal Heuristics only. Vendor-specific logic removed.
"""

from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum
import copy


class FieldType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    CURRENCY = "currency"
    LIST = "list"


@dataclass
class FieldConfig:
    name: str
    display_name: str
    field_type: FieldType
    description: str
    extraction_guidelines: List[str]
    aliases: List[str] = None
    required: bool = False
    default: Any = None
    validation_rules: List[str] = None

    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []
        if self.validation_rules is None:
            self.validation_rules = []


# =============================================================================
# 1. COMMERCIAL INVOICE FIELDS
# =============================================================================

INVOICE_FIELDS: Dict[str, FieldConfig] = {
    "invoice_number": FieldConfig(
        name="invoice_number",
        display_name="Invoice Number",
        field_type=FieldType.STRING,
        description="Unique invoice identifier",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Invoice No', 'Invoice #', 'Invoice Number', 'Tax Invoice No', 'Bill No', 'Document No'",
            "**LOCATION**: Usually top-right corner of first page, near date",
            "**PRIORITY 1**: Extract value directly after the anchor label",
            "**PRIORITY 2**: If no label, find prominent alphanumeric string near top-right",
            "**FORMAT**: Preserve exact format including prefixes (SIN-, KM_, 0100, etc.)",
            "**NEGATIVE RULE**: Ignore dates, phone numbers, fax numbers, bank account numbers"
        ],
        required=True
    ),

    "invoice_date": FieldConfig(
        name="invoice_date",
        display_name="Invoice Date",
        field_type=FieldType.DATE,
        description="Date the invoice was issued",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Invoice Date', 'Date', 'Inv. Date', 'Document Date', 'Issue Date'",
            "**LOCATION**: Usually near invoice number, top section of document",
            "**INPUT FORMATS**: DD/MM/YYYY, MM/DD/YYYY, DD-MMM-YYYY, YYYY-MM-DD",
            "**OUTPUT FORMAT**: Always convert to ISO format YYYY-MM-DD",
            "**PRIORITY**: Prefer date labeled 'Invoice Date' over 'Date' or 'Order Date'",
            "**NEGATIVE RULE**: Ignore 'PO Date', 'Order Date', 'Delivery Date', 'Due Date'"
        ],
        required=True
    ),

    "seller_name": FieldConfig(
        name="seller_name",
        display_name="Seller Name",
        field_type=FieldType.STRING,
        description="Name of the seller/supplier company",
        extraction_guidelines=[
            "**PRIMARY**: Company with LOGO at top of invoice (usually top-left)",
            "**ANCHOR LABELS**: 'From', 'Seller', 'Supplier', 'Vendor', 'Exporter', 'Shipper'",
            "**SECONDARY**: Company name in letterhead or header",
            "**FORMAT**: Extract full legal company name including suffixes (Pte Ltd, Inc, GmbH)",
            "**NEGATIVE RULE**: Not the buyer, not the bank, not the freight forwarder",
            "**NEGATIVE RULE**: Ignore 'Bill To', 'Ship To', 'Consignee' - those are buyers"
        ],
        required=True
    ),

    "seller_address": FieldConfig(
        name="seller_address",
        display_name="Seller Address",
        field_type=FieldType.STRING,
        description="Complete full address of the seller/supplier",
        extraction_guidelines=[
            "**LOCATION**: Directly below or beside Seller Name / Company Logo",
            "**CRITICAL**: Extract the COMPLETE FULL address - do not truncate",
            "**MUST INCLUDE ALL OF THESE (if present)**:",
            "  - Building/Floor number",
            "  - Street name and number",
            "  - Area/District/Suburb",
            "  - City/Town",
            "  - State/Province/Region",
            "  - Postal/ZIP Code",
            "  - Country",
            "**CONCATENATION**: Join multi-line addresses with comma-space",
            "**NEGATIVE RULE**: Do NOT include bank details, tax IDs, or registration numbers"
        ]
    ),

    "buyer_name": FieldConfig(
        name="buyer_name",
        display_name="Buyer Name",
        field_type=FieldType.STRING,
        description="Name of the buyer/importer company",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Bill To', 'Sold To', 'Buyer', 'Consignee', 'Importer', 'Customer'",
            "**PRIORITY 1**: Value after 'Bill To' or 'Sold To' label",
            "**PRIORITY 2**: If 'Ship To' is different from 'Bill To', use 'Bill To'",
            "**FORMAT**: Extract full legal company name",
            "**NEGATIVE RULE**: NOT the seller, NOT the bank, NOT the freight agent",
            "**NEGATIVE RULE**: Ignore 'Notify Party' - that's different from buyer"
        ],
        required=True
    ),

    "buyer_address": FieldConfig(
        name="buyer_address",
        display_name="Buyer Address",
        field_type=FieldType.STRING,
        description="Complete full address of the buyer/importer",
        extraction_guidelines=[
            "**LOCATION**: Directly below or beside Buyer Name / 'Bill To' section",
            "**CRITICAL**: Extract the COMPLETE FULL address - do not truncate",
            "**CONCATENATION**: Join multi-line addresses with comma-space",
            "**PRIORITY**: Use 'Bill To' address over 'Ship To' address if different",
            "**NEGATIVE RULE**: Do NOT extract seller address or bank address"
        ]
    ),

    "invoice_currency": FieldConfig(
        name="invoice_currency",
        display_name="Currency",
        field_type=FieldType.CURRENCY,
        description="3-letter ISO currency code",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Currency', 'Curr', 'Amount in', 'All values in'",
            "**LOCATION**: Near Total Amount, or in header section",
            "**FORMAT**: Extract 3-letter ISO code only (USD, EUR, SGD, INR, JPY, GBP, CHF)",
            "**SYMBOL MAPPING**: $ → USD (unless context says SGD/AUD), € → EUR, £ → GBP, ¥ → JPY/CNY",
            "**PRIORITY**: Explicit text 'US Dollars' > Symbol $ alone",
            "**NEGATIVE RULE**: Do NOT guess if no clear indicator exists"
        ]
    ),

    "total_amount": FieldConfig(
        name="total_amount",
        display_name="Total Amount",
        field_type=FieldType.NUMBER,
        description="Grand total invoice value",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Total', 'Grand Total', 'Net Total', 'Invoice Total', 'Amount Due', 'Total Amount'",
            "**LOCATION**: Bottom of invoice, after line items",
            "**SELECTOR RULE**: If multiple totals exist, pick the LARGEST value (usually includes tax/freight)",
            "**PRIORITY ORDER**: 'Grand Total' > 'Total' > 'Subtotal'",
            "**FORMAT**: Remove currency symbols ($ € £), remove thousand separators (,), keep decimal point",
            "**NEGATIVE RULE**: Ignore 'Subtotal', 'Items Total' if 'Grand Total' exists"
        ],
        required=True
    ),

    "net_amount": FieldConfig(
        name="net_amount",
        display_name="Net Amount",
        field_type=FieldType.NUMBER,
        description="Total amount before tax",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Sub Total', 'Taxable Value', 'Net Total', 'Total before Tax'"
        ]
    ),
    
    "tax_amount": FieldConfig(
        name="tax",
        display_name="Tax Amount",
        field_type=FieldType.NUMBER,
        description="Total tax amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Tax Total', 'VAT', 'GST Total', 'IGST+CGST+SGST', 'Total Tax'"
        ]
    ),
    
    "discount": FieldConfig(
        name="discount",
        display_name="Discount Amount",
        field_type=FieldType.NUMBER,
        description="Total discount amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Discount', 'Less', 'Rebate'",
            "**NOTE**: Return positive number"
        ]
    ),
    
    "shipping_charges": FieldConfig(
        name="shipping_charges",
        display_name="Shipping Charges",
        field_type=FieldType.NUMBER,
        description="Total shipping/freight charges",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Freight', 'Shipping', 'Transportation', 'Delivery Charge'"
        ]
    ),
    
    "payment_terms": FieldConfig(
        name="payment_terms",
        display_name="Payment Terms",
        field_type=FieldType.STRING,
        description="Payment conditions",
        extraction_guidelines=[
            "**VALUES**: 'Net 30', 'Immediate', 'Due on Receipt'",
            "**ANCHOR LABELS**: 'Payment Terms', 'Terms'"
        ]
    ),
    
    "due_date": FieldConfig(
        name="due_date",
        display_name="Due Date",
        field_type=FieldType.DATE,
        description="Payment due date",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Due Date', 'Pay by', 'Payment Due'"
        ]
    ),

    # --- INDIA GST FIELDS START ---
    
    "vendor_gstin": FieldConfig(
        name="vendor_gstin",
        display_name="Vendor GST Number",
        field_type=FieldType.STRING,
        description="GST Identification Number of the Supplier",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'GSTIN', 'GST No.', 'Vendor GST', 'GST#', 'GST Registration No'",
            "**FORMAT**: 15 alphanumeric characters (e.g., 27AAAAA0000A1Z5)",
            "**LOCATION**: Header or under Seller details",
            "**VALIDATION**: Starts with 2-digit state code"
        ]
    ),

    "billing_gstin": FieldConfig(
        name="billing_gstin",
        display_name="Billing GST Number",
        field_type=FieldType.STRING,
        description="GST Identification Number of the Buyer",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Customer GST', 'Buyer GSTIN', 'GST No.', 'GSTIN'",
            "**LOCATION**: 'Bill To' section or under Buyer details",
            "**FORMAT**: 15 alphanumeric characters"
        ]
    ),
    
    "sgst_percentage": FieldConfig(
        name="sgst_percentage",
        display_name="SGST Rate",
        field_type=FieldType.NUMBER,
        description="State Goods and Service Tax Rate (%)",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'SGST @', 'SGST Rate', 'SGST %'",
            "**FORMAT**: Number only (e.g. 9 for 9%)"
        ]
    ),
    
    "cgst_percentage": FieldConfig(
        name="cgst_percentage",
        display_name="CGST Rate",
        field_type=FieldType.NUMBER,
        description="Central Goods and Service Tax Rate (%)",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'CGST @', 'CGST Rate', 'CGST %'",
            "**FORMAT**: Number only"
        ]
    ),
    
    "igst_percentage": FieldConfig(
        name="igst_percentage",
        display_name="IGST Rate",
        field_type=FieldType.NUMBER,
        description="Integrated Goods and Service Tax Rate (%)",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'IGST @', 'IGST Rate', 'IGST %'",
            "**FORMAT**: Number only"
        ]
    ),

    "sgst_total": FieldConfig(
        name="sgst_total",
        display_name="SGST Amount",
        field_type=FieldType.NUMBER,
        description="Total SGST Amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'SGST Amount', 'SGST Amt', 'SGST'",
            "**LOCATION**: Bottom tax summary or footer",
            "**NEGATIVE RULE**: Not the rate"
        ]
    ),

    "cgst_total": FieldConfig(
        name="cgst_total",
        display_name="CGST Amount",
        field_type=FieldType.NUMBER,
        description="Total CGST Amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'CGST Amount', 'CGST Amt', 'CGST'",
            "**LOCATION**: Bottom tax summary or footer"
        ]
    ),
    
    "igst_total": FieldConfig(
        name="igst_total",
        display_name="IGST Amount",
        field_type=FieldType.NUMBER,
        description="Total IGST Amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'IGST Amount', 'IGST Amt', 'IGST'",
            "**LOCATION**: Bottom tax summary"
        ]
    ),
    
    # --- INDIA GST FIELDS END ---

    "invoice_toi": FieldConfig(
        name="invoice_toi",
        display_name="Terms of Delivery (TOI)",
        field_type=FieldType.STRING,
        description="Terms of delivery / Incoterms governing shipment",
        extraction_guidelines=[
            "**ALLOWED LABELS (EXPLICIT REQUIRED)**:",
            "  'Terms of Delivery', 'Delivery Terms', 'Incoterms', 'Shipment Terms'",
            "**EXTRACTION RULE (VERBATIM)**:",
            "  Extract the FULL value EXACTLY as written after the label.",
            "  DO NOT normalize, translate, or convert values.",
            "**SUPPORTED VALUES (OPEN TEXT)**:",
            "  EXW, FCA, DAP, DDP, CIF, FOB, Delivery at Place",
            "**HARD NEGATIVE RULES**:",
            "  - NOT payment terms",
            "  - NOT freight charges",
            "  - NOT shipping mode (Air/Sea/Road)",
            "**ANTI-HALLUCINATION FAIL SAFE**:",
            "  If no allowed label is present, RETURN null."
        ]
    ),

    "invoice_exchange_rate": FieldConfig(
        name="invoice_exchange_rate",
        display_name="Exchange Rate",
        field_type=FieldType.NUMBER,
        description="Currency exchange rate applied to invoice",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Exchange Rate', 'Ex. Rate', 'Conversion Rate', 'Rate', 'FX Rate'",
            "**LOCATION**: Usually near currency or total amount section",
            "**FORMAT**: Numeric value with up to 6 decimal places",
            "**CONTEXT**: Look for pattern like '1 USD = 83.45 INR' → Extract 83.45",
            "**NEGATIVE RULE**: Do NOT confuse with unit price or discount rate",
            "**DEFAULT**: Return null if no exchange rate mentioned (single currency invoice)"
        ]
    ),

    "invoice_po_date": FieldConfig(
        name="invoice_po_date",
        display_name="PO Date",
        field_type=FieldType.DATE,
        description="Purchase Order date referenced in invoice",
        extraction_guidelines=[
            "**ALLOWED LABELS ONLY (MANDATORY)**:",
            "  'PO Date', 'Purchase Order Date', 'Order Date'.",
            "**HARD DEPENDENCY RULE (ABSOLUTE)**:",
            "  If NO PO Number / Order Number exists in the document,",
            "  invoice_po_date MUST be null.",
            "**STRICT PROHIBITION (ABSOLUTE)**:",
            "  A date labeled ONLY as 'Date' or 'Date:' is ALWAYS Invoice Date.",
            "  It MUST NEVER be treated as PO Date.",
            "**ABSOLUTE SHIPMENT PHRASE BAN (CRITICAL)**:",
            "  NEVER extract dates near or under: 'Leaving on', 'ETD', 'ETA', 'Dispatch Date'.",
            "**FAIL SAFE (NON-NEGOTIABLE)**:",
            "  If PO Date is NOT explicitly labeled AND linked to a PO Number → RETURN null."
        ]
    ),
}


INVOICE_ITEM_FIELDS: Dict[str, FieldConfig] = {
    "item_no": FieldConfig(
        name="item_no",
        display_name="Item/Serial Number",
        field_type=FieldType.STRING,
        description="Line item sequence number",
        extraction_guidelines=[
            "**HARDCODED RULE**: IGNORE the specific values in the column (e.g. '000027').",
            "**HARDCODED RULE**: IGNORE the row position (do not count 1, 2, 3...).",
            "**ACTION**: For EVERY single line item row, output the value '1'.",
            "**RESULT**: If there are 5 items, the output should be '1', '1', '1', '1', '1'.",
        ]
    ),

    "item_description": FieldConfig(
        name="item_description",
        display_name="Description",
        field_type=FieldType.STRING,
        description="Product/service description",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Description', 'Particulars', 'Item Description', 'Product', 'Goods', 'Material'",
            "**GRID ALIGNMENT**: Extract text aligned under Description column",
            "**MULTI-LINE RULE**: If a row has description text but NO Quantity/Price, append to PREVIOUS item",
            "**CONCATENATION**: Join wrapped lines with single space",
            "**NEGATIVE RULE**: Do NOT create phantom items from text overflow",
            "**NEGATIVE RULE**: Do NOT include standalone page numbers or headers"
        ]
    ),

    "item_part_no": FieldConfig(
        name="item_part_no",
        display_name="Part Number",
        field_type=FieldType.STRING,
        description="Product part number, SKU, or article number",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Part No', 'Part Number', 'Article', 'Article No', 'SKU', 'Model', 'Item Code', 'Material No'",
            "**PRIMARY**: Extract from a dedicated Part Number column ONLY",
            "**FORMAT PRIORITY**: Prefer PURE NUMERIC values when present in a Part Number column",
            "**NEGATIVE FORMAT**: Do NOT extract mixed alphanumeric codes if a numeric-only value exists in the same row",
            "**FALLBACK**: Use alphanumeric codes (e.g. 'AB-1234') ONLY if no numeric part number exists",
            "**NEGATIVE RULE**: Do NOT extract Pack numbers as part numbers",
            "**NEGATIVE RULE**: Do NOT extract long descriptive product codes beneath the Part Number"
        ]
    ),

    "item_po_no": FieldConfig(
        name="item_po_no",
        display_name="Item PO Number",
        field_type=FieldType.STRING,
        description="Purchase Order reference for line item",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'PO No', 'Order No', 'Your Reference', 'Customer PO', 'Your Order', 'PO Number'",
            "**PRIORITY 1**: Value in dedicated PO column for line item",
            "**PRIORITY 2**: If no line-level PO, use header-level PO for all items",
            "**NEGATIVE RULE**: NOT a person's name, NOT a date"
        ]
    ),

    "item_date": FieldConfig(
        name="item_date",
        display_name="Item Date",
        field_type=FieldType.DATE,
        description="Date specific to line item (delivery/ship date)",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Item Date', 'Date of Service', 'Service Date'",
            "**LOCATION**: In line items table, date column",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**DEFAULT**: Return null if no item-specific date exists",
            "**NEGATIVE RULE**: NOT the invoice date, NOT the PO date.",
            "**CRITICAL NEGATIVE RULE**: IGNORE columns labeled 'Delivery Date', 'Shipping Date', or 'Del Date'. These are NOT the Item Date."
        ]
    ),

    "hsn_code": FieldConfig(
        name="hsn_code",
        display_name="HSN / HS Code",
        field_type=FieldType.STRING,
        description="Harmonized System Code for customs classification",
        extraction_guidelines=[
            "**EXPLICIT LABEL REQUIRED (MANDATORY)**:",
            "  Extract HSN ONLY if explicitly labeled as one of: 'HSN', 'HSN Code', 'HS Code', 'Tariff Code', 'Commodity Code'.",
            "**STRICT LOCATION RULE**:",
            "  The code MUST appear in a column or row explicitly labeled for HSN/HS Code.",
            "**FORMAT RULE**:",
            "  Must be 6–8 digit numeric code (dots allowed).",
            "**ROW ISOLATION RULE (CRITICAL)**:",
            "  If the same row contains a Part Number, DO NOT extract any numeric value from that row as HSN.",
            "**HARD NEGATIVE RULES (ABSOLUTE)**:",
            "  - NOT a Part Number",
            "  - NOT an Article Number",
            "  - NOT a Quantity",
            "  - NOT a Price",
            "**ANTI-HALLUCINATION FAIL SAFE**: If HSN is NOT explicitly labeled, RETURN null."
        ]
    ),

    "item_quantity": FieldConfig(
        name="item_quantity",
        display_name="Quantity",
        field_type=FieldType.NUMBER,
        description="Number of units ordered",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Qty', 'Quantity', 'Ordered', 'Shipped', 'Units', 'Pcs'",
            "**FORMAT**: Numeric value, remove thousand separators",
            "**DECIMALS**: Allow decimals for fractional quantities (kg, meters)",
            "**VALIDATION**: Must be positive number, zero only if explicitly stated",
            "**NEGATIVE RULE**: NOT the unit price, NOT the amount"
        ]
    ),

    "item_uom": FieldConfig(
        name="item_uom",
        display_name="Unit of Measure",
        field_type=FieldType.STRING,
        description="Unit of measurement for quantity",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'UOM', 'Unit', 'U/M', 'Measure', 'UM'",
            "**COMMON VALUES**: PCS, EA, NOS, KG, KGS, LBS, MTR, M, CM, MM, LTR, SET, BOX, CTN, PAL",
            "**STANDARDIZATION**: 'Pieces' → 'PCS', 'Each' → 'EA', 'Numbers' → 'NOS', 'Kilograms' → 'KG'",
            "**LOCATION**: Column next to Quantity, or appended to quantity (e.g., '100 PCS')",
            "**FORMAT**: Uppercase abbreviation",
            "**DEFAULT**: Return null if not specified"
        ]
    ),

    "item_unit_price": FieldConfig(
        name="item_unit_price",
        display_name="Unit Price",
        field_type=FieldType.NUMBER,
        description="Price per single unit",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Unit Price', 'Price', 'Rate', 'Unit Rate', 'Price/Unit', 'Each'",
            "**FORMAT**: Numeric, remove currency symbols and thousand separators",
            "**DECIMALS**: Preserve decimal precision (up to 4-6 decimal places common)",
            "**CURRENCY**: Use invoice-level currency for all unit prices",
            "**VALIDATION**: If unit_price × quantity ≈ amount, extraction is likely correct",
            "**NEGATIVE RULE**: NOT the total amount, NOT the extended price"
        ]
    ),

    "item_amount": FieldConfig(
        name="item_amount",
        display_name="Line Amount",
        field_type=FieldType.NUMBER,
        description="Total amount for line item (qty × unit price)",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Amount', 'Total', 'Extended', 'Line Total', 'Net Amount', 'Value'",
            "**LOCATION**: Usually rightmost column in line items table",
            "**FORMAT**: Numeric, remove currency symbols and thousand separators",
            "**VALIDATION**: Should approximately equal quantity × unit_price",
            "**NEGATIVE RULE**: NOT the invoice grand total, NOT a subtotal row"
        ]
    ),

    "item_origin_country": FieldConfig(
        name="item_origin_country",
        display_name="Country of Origin",
        field_type=FieldType.STRING,
        description="Country where the product was manufactured",
        extraction_guidelines=[
            "**ABSOLUTE PRE-CONDITION (MANDATORY)**:",
            "  You MUST find an explicit textual label that clearly indicates origin: 'Country of Origin', 'COO', 'Made in', 'Origin:'.",
            "**STRICT LABEL BINDING RULE (CRITICAL)**:",
            "  Extract a country ONLY if it appears IMMEDIATELY AFTER one of the allowed labels.",
            "  Ignore country names appearing elsewhere in the document.",
            "**COUNTRY NAME NORMALIZATION RULE (CRITICAL)**:",
            "  ALWAYS return the clean canonical country name ONLY (e.g., 'Made in Germany' → 'Germany').",
            "**ISO CODE NORMALIZATION RULE (CRITICAL)**:",
            "  Convert 2-letter ISO codes to full names (e.g., DE → Germany, CN → China).",
            "**ANTI-HALLUCINATION FAIL SAFE (NON-NEGOTIABLE)**:",
            "  If NO allowed label is found, RETURN null."
        ]
    ),

    "item_mfg_name": FieldConfig(
        name="item_mfg_name",
        display_name="Manufacturer Name",
        field_type=FieldType.STRING,
        description="Name of the product manufacturer",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Manufacturer', 'Mfg', 'Maker', 'Brand', 'Producer'",
            "**PRIORITY 1**: Dedicated manufacturer column in line items",
            "**PRIORITY 2**: Manufacturer mentioned in item description (e.g. 'c/o ManufacturerName')",
            "**PRIORITY 3**: Specific 'Manufacturer' block in document body",
            "**FALLBACK (Use Last)**: If NOT found in items or body, check Top-Left Header (Seller Name/Logo). Assume Seller is Manufacturer only if no other manufacturer is listed.",
            "**FORMAT**: Full company name"
        ]
    ),

    "item_mfg_addr": FieldConfig(
        name="item_mfg_addr",
        display_name="Manufacturer Address",
        field_type=FieldType.STRING,
        description="Complete full address of the product manufacturer",
        extraction_guidelines=[
            "**LOCATION**: Usually with manufacturer name, or in separate manufacturer details section",
            "**CRITICAL**: Extract the COMPLETE FULL address if available",
            "**MUST INCLUDE ALL**: Street, City, State, Zip, AND COUNTRY",
            "**NEGATIVE RULE**: NOT the buyer address"
        ]
    ),

    "item_mfg_country": FieldConfig(
        name="item_mfg_country",
        display_name="Manufacturer Country",
        field_type=FieldType.STRING,
        description="Country of the manufacturer",
        extraction_guidelines=[
            "**PRIMARY SOURCE (MANDATORY)**:",
            "  Extract country STRICTLY from the already extracted 'item_mfg_addr'.",
            "**DETERMINISTIC PARSING RULE (CRITICAL)**:",
            "  If a full country name appears anywhere in 'item_mfg_addr', you MUST extract it exactly as written.",
            "**ISO CODE NORMALIZATION RULE (CRITICAL)**:",
            "  If the address contains a 2-letter ISO country code, convert it to the full country name.",
            "**FAIL SAFE**: If NO country name or ISO code exists in 'item_mfg_addr', return null."
        ]
    ),
}

# =============================================================================
# 2. PURCHASE ORDER (PO) FIELDS
# =============================================================================

PO_FIELDS: Dict[str, FieldConfig] = {
    "po_number": FieldConfig(
        name="po_number",
        display_name="PO Number",
        field_type=FieldType.STRING,
        description="Unique identifier for the purchase order",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'PO Number', 'Purchase Order No', 'Order No', 'P.O. #'",
            "**LOCATION**: Usually in header section, top-right area",
            "**FORMAT**: Preserve exact format including prefixes",
            "**NEGATIVE RULE**: NOT the invoice number, NOT the quote number"
        ]
    ),

    "po_date": FieldConfig(
        name="po_date",
        display_name="PO Date",
        field_type=FieldType.DATE,
        description="Date the PO was issued",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'PO Date', 'Order Date', 'Date', 'Issue Date'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**LOCATION**: Near PO number in header",
            "**NEGATIVE RULE**: NOT the required delivery date"
        ]
    ),

    "purchase_order_expiry_date": FieldConfig(
        name="purchase_order_expiry_date",
        display_name="Purchase Order Expiry date",
        field_type=FieldType.DATE,
        description="PO validity/expiry date",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Purchase Order Expiry date', 'PO Expiry Date', 'Expiry Date'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**NEGATIVE RULE**: NOT PO issue date"
        ]
    ),

    "delivery_by_date": FieldConfig(
        name="delivery_by_date",
        display_name="Delivery by date",
        field_type=FieldType.DATE,
        description="Requested or promised delivery by date",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Delivery by date', 'Deliver By', 'Required By', 'Need By Date'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**NEGATIVE RULE**: NOT PO issue date"
        ]
    ),

    "vendor_name": FieldConfig(
        name="vendor_name",
        display_name="Vendor Name",
        field_type=FieldType.STRING,
        description="Name of the seller/vendor",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Vendor', 'Supplier', 'To', 'Ship From'",
            "**LOCATION**: Usually in 'To' section or vendor block",
            "**FORMAT**: Full legal company name",
            "**NEGATIVE RULE**: NOT the buyer company (that's usually in header/logo)"
        ]
    ),

    "vendor_address": FieldConfig(
        name="vendor_address",
        display_name="Vendor Address",
        field_type=FieldType.STRING,
        description="Address of the vendor",
        extraction_guidelines=[
            "**LOCATION**: Below vendor name",
            "**CRITICAL**: Extract complete full address",
            "**CONCATENATION**: Join multi-line addresses with comma-space"
        ]
    ),

    "supplier_code": FieldConfig(
        name="supplier_code",
        display_name="Supplier Code",
        field_type=FieldType.STRING,
        description="Supplier or vendor code identifier",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Supplier Code', 'Vendor Code', 'Supplier ID'",
            "**FORMAT**: Preserve exact alphanumeric format"
        ]
    ),

    "buyer_name": FieldConfig(
        name="buyer_name",
        display_name="Buyer Name",
        field_type=FieldType.STRING,
        description="Name of the entity issuing the PO",
        required=True,
        extraction_guidelines=[
            "**PRIMARY**: Company with LOGO in header (buyer's company)",
            "**ANCHOR LABELS**: 'From', 'Buyer', 'Bill To'",
            "**ROLE REVERSAL**: Unlike invoices, header company is the BUYER",
            "**FORMAT**: Full legal company name"
        ]
    ),

    "buyer_address": FieldConfig(
        name="buyer_address",
        display_name="Buyer Address",
        field_type=FieldType.STRING,
        description="Billing address of the buyer",
        extraction_guidelines=[
            "**LOCATION**: In header or 'Bill To' section",
            "**CRITICAL**: Extract complete full address",
            "**CONCATENATION**: Join multi-line addresses with comma-space"
        ]
    ),

    "ship_to_name": FieldConfig(
        name="ship_to_name",
        display_name="Ship To Name",
        field_type=FieldType.STRING,
        description="Name of the receiving location",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Ship To', 'Deliver To', 'Delivery Location'",
            "**FALLBACK**: If not specified, may be same as buyer_name",
            "**FORMAT**: Company or facility name"
        ]
    ),

    "billing_name": FieldConfig(
        name="billing_name",
        display_name="Billing Name",
        field_type=FieldType.STRING,
        description="Billing party name",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Billing Name', 'Bill To', 'Billing Party'",
            "**FORMAT**: Full name as printed"
        ]
    ),

    "billing_address": FieldConfig(
        name="billing_address",
        display_name="Billing Address",
        field_type=FieldType.STRING,
        description="Billing address details",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Billing Address', 'Bill To Address'",
            "**CRITICAL**: Extract full multi-line billing address"
        ]
    ),

    "delivery_name": FieldConfig(
        name="delivery_name",
        display_name="Delivery Name",
        field_type=FieldType.STRING,
        description="Delivery party or destination name",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Delivery Name', 'Deliver To', 'Ship To Name'",
            "**FORMAT**: Full name as printed"
        ]
    ),

    "delivery_address": FieldConfig(
        name="delivery_address",
        display_name="Delivery Address",
        field_type=FieldType.STRING,
        description="Delivery destination address",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Delivery Address', 'Ship To Address', 'Deliver To Address'",
            "**CRITICAL**: Extract full multi-line delivery address"
        ]
    ),

    "ship_to_address": FieldConfig(
        name="ship_to_address",
        display_name="Ship To Address",
        field_type=FieldType.STRING,
        description="Physical delivery address",
        extraction_guidelines=[
            "**LOCATION**: 'Ship To' section",
            "**CRITICAL**: Extract complete full address including dock/bay if present",
            "**CONCATENATION**: Join multi-line addresses with comma-space"
        ]
    ),

    "payment_terms": FieldConfig(
        name="payment_terms",
        display_name="Payment Terms",
        field_type=FieldType.STRING,
        description="Agreed payment terms",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Payment Terms', 'Terms', 'Payment'",
            "**COMMON VALUES**: 'Net 30', 'Net 60', 'Due on Receipt', 'COD'",
            "**LOCATION**: Usually in header or terms section"
        ]
    ),

    "shipping_method": FieldConfig(
        name="shipping_method",
        display_name="Shipping Method",
        field_type=FieldType.STRING,
        description="Mode of transport",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Ship Via', 'Shipping Method', 'Delivery Method'",
            "**COMMON VALUES**: 'FedEx', 'UPS', 'Air Freight', 'Sea Freight', 'Ground'",
            "**LOCATION**: Near shipping address or in terms section"
        ]
    ),

    "currency": FieldConfig(
        name="currency",
        display_name="Currency",
        field_type=FieldType.CURRENCY,
        description="Currency code",
        extraction_guidelines=[
            "**FORMAT**: 3-letter ISO code (USD, EUR, GBP, etc.)",
            "**LOCATION**: Near total amount or in header",
            "**SYMBOL MAPPING**: $ → USD, € → EUR, £ → GBP"
        ]
    ),

    "total_amount": FieldConfig(
        name="total_amount",
        display_name="Total Amount",
        field_type=FieldType.NUMBER,
        description="Grand total amount of the PO",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Total', 'Grand Total', 'Order Total'",
            "**LOCATION**: Bottom of document after line items",
            "**FORMAT**: Remove currency symbols and thousand separators",
            "**VALIDATION**: Should equal sum of line item totals"
        ]
    )
}

PO_ITEM_FIELDS: Dict[str, FieldConfig] = {
    "item_description": FieldConfig(
        name="item_description",
        display_name="Description",
        field_type=FieldType.STRING,
        description="Item name or description",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Description', 'Item', 'Product', 'Material'",
            "**FORMAT**: Full product description",
            "**MULTI-LINE**: Concatenate wrapped text with space"
        ]
    ),

    "item_part_no": FieldConfig(
        name="item_part_no",
        display_name="Part Number",
        field_type=FieldType.STRING,
        description="SKU or Part Number",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Part No', 'SKU', 'Item Code', 'Product Code'",
            "**FORMAT**: Preserve exact format",
            "**LOCATION**: Dedicated column in line items"
        ]
    ),

    "item_quantity": FieldConfig(
        name="item_quantity",
        display_name="Quantity",
        field_type=FieldType.NUMBER,
        description="Number of units ordered",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Qty', 'Quantity', 'Ordered'",
            "**FORMAT**: Numeric value, remove thousand separators",
            "**VALIDATION**: Must be positive number"
        ]
    ),

    "item_uom": FieldConfig(
        name="item_uom",
        display_name="Unit of Measure",
        field_type=FieldType.STRING,
        description="Unit of measurement",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'UOM', 'Unit', 'U/M'",
            "**COMMON VALUES**: 'EA', 'PCS', 'BOX', 'KG', 'SET'",
            "**FORMAT**: Uppercase abbreviation"
        ]
    ),

    "item_unit_price": FieldConfig(
        name="item_unit_price",
        display_name="Unit Price",
        field_type=FieldType.NUMBER,
        description="Price per unit",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Unit Price', 'Price', 'Rate'",
            "**FORMAT**: Numeric, remove currency symbols",
            "**VALIDATION**: quantity × unit_price ≈ total_price"
        ]
    ),

    "item_total_price": FieldConfig(
        name="item_total_price",
        display_name="Total Price",
        field_type=FieldType.NUMBER,
        description="Total price for this line item",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Total', 'Amount', 'Extended Price'",
            "**FORMAT**: Numeric, remove currency symbols",
            "**VALIDATION**: Should equal quantity × unit_price"
        ]
    ),

    "item_date_required": FieldConfig(
        name="item_date_required",
        display_name="Date Required",
        field_type=FieldType.DATE,
        description="Specific delivery date for this item",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Required Date', 'Delivery Date', 'Need By'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**DEFAULT**: Return null if not item-specific"
        ]
    ),

    "delivery_dates": FieldConfig(
        name="delivery_dates",
        display_name="Delivery Dates",
        field_type=FieldType.DATE,
        description="Line item delivery date",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Delivery Date', 'Delivery Dates', 'Deliver By'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**DEFAULT**: Return null when not present at line-item level"
        ]
    ),

    "units_of_measure": FieldConfig(
        name="units_of_measure",
        display_name="Units Of Measure",
        field_type=FieldType.STRING,
        description="Line item unit of measure",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Units Of Measure', 'UOM', 'Unit'",
            "**FORMAT**: Preserve printed unit abbreviation"
        ]
    ),

    "net_amounts": FieldConfig(
        name="net_amounts",
        display_name="Net Amounts",
        field_type=FieldType.NUMBER,
        description="Line item net amount before tax",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Net Amount', 'Net Amounts', 'Net'",
            "**FORMAT**: Numeric value only"
        ]
    ),

    "tax_amounts": FieldConfig(
        name="tax_amounts",
        display_name="Tax Amounts",
        field_type=FieldType.NUMBER,
        description="Line item tax amount",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Tax Amount', 'Tax Amounts', 'GST Amount', 'VAT Amount'",
            "**FORMAT**: Numeric value only"
        ]
    ),

    "tax_rate": FieldConfig(
        name="tax_rate",
        display_name="Tax Rate",
        field_type=FieldType.NUMBER,
        description="Line item tax rate percentage",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Tax Rate', 'GST %', 'VAT %', 'Tax %'",
            "**FORMAT**: Numeric percentage value"
        ]
    )
}

# =============================================================================
# 3. GOODS RECEIVED NOTE (GRN) FIELDS
# =============================================================================

GRN_FIELDS: Dict[str, FieldConfig] = {
    "grn_number": FieldConfig(
        name="grn_number",
        display_name="GRN Number",
        field_type=FieldType.STRING,
        description="Unique identifier for the GRN",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'GRN No', 'Receipt No', 'Delivery Note No', 'Docket No'",
            "**LOCATION**: Usually in header section",
            "**FORMAT**: Preserve exact format"
        ]
    ),

    "date_received": FieldConfig(
        name="date_received",
        display_name="Date Received",
        field_type=FieldType.DATE,
        description="Date the goods arrived",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Date Received', 'Received On', 'Delivery Date'",
            "**FORMAT**: Convert to ISO format YYYY-MM-DD",
            "**LOCATION**: Header section near GRN number"
        ]
    ),

    "po_reference": FieldConfig(
        name="po_reference",
        display_name="PO Reference",
        field_type=FieldType.STRING,
        description="Associated Purchase Order number",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'PO No', 'Order No', 'PO Reference', 'Your Ref'",
            "**CRITICAL**: This links GRN to originating PO",
            "**LOCATION**: Header or reference section"
        ]
    ),

    "supplier_name": FieldConfig(
        name="supplier_name",
        display_name="Supplier Name",
        field_type=FieldType.STRING,
        description="Name of the supplier",
        required=True,
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Supplier', 'Vendor', 'From', 'Delivered By'",
            "**FORMAT**: Full company name"
        ]
    ),

    "merchant_address": FieldConfig(
        name="merchant_address",
        display_name="Merchant Address",
        field_type=FieldType.STRING,
        description="Merchant address details",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Merchant Address', 'Supplier Address', 'Vendor Address'",
            "**CRITICAL**: Extract full multi-line address"
        ]
    ),

    "merchant_phone_number": FieldConfig(
        name="merchant_phone_number",
        display_name="Merchant Phone Number",
        field_type=FieldType.STRING,
        description="Merchant contact phone number",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Merchant Phone Number', 'Phone', 'Contact No', 'Tel'",
            "**FORMAT**: Preserve digits and leading + if present"
        ]
    ),

    "warehouse_location": FieldConfig(
        name="warehouse_location",
        display_name="Warehouse Location",
        field_type=FieldType.STRING,
        description="Receiving warehouse or dock",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Warehouse', 'Location', 'Received At', 'Dock'",
            "**FORMAT**: Facility name or code"
        ]
    ),

    "carrier_name": FieldConfig(
        name="carrier_name",
        display_name="Carrier Name",
        field_type=FieldType.STRING,
        description="Invoice provider/Carrier name",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Carrier', 'Transporter', 'Delivered Via'",
            "**COMMON VALUES**: 'FedEx', 'UPS', 'DHL', company truck names"
        ]
    ),

    "vehicle_reg": FieldConfig(
        name="vehicle_reg",
        display_name="Vehicle Registration",
        field_type=FieldType.STRING,
        description="Truck or Vehicle Registration number",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Vehicle Reg', 'Truck No', 'Vehicle Number', 'License Plate'",
            "**FORMAT**: Alphanumeric registration"
        ]
    ),

    "waybill_no": FieldConfig(
        name="waybill_no",
        display_name="Waybill Number",
        field_type=FieldType.STRING,
        description="Delivery Note or Waybill number",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Waybill', 'AWB', 'Delivery Note', 'Consignment No'",
            "**FORMAT**: Alphanumeric tracking number"
        ]
    ),

    "receiver_name": FieldConfig(
        name="receiver_name",
        display_name="Receiver Name",
        field_type=FieldType.STRING,
        description="Name of person receiving goods",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Received By', 'Receiver', 'Accepted By'",
            "**FORMAT**: Person's name (may include signature)"
        ]
    ),

    "total_packages": FieldConfig(
        name="total_packages",
        display_name="Total Packages",
        field_type=FieldType.NUMBER,
        description="Total count of packages received",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Total Packages', 'No of Packages', 'Package Count'",
            "**FORMAT**: Integer value",
            "**VALIDATION**: Must be positive number"
        ]
    ),

    "total_amount": FieldConfig(
        name="total_amount",
        display_name="Total Amount",
        field_type=FieldType.NUMBER,
        description="Document total amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Total Amount', 'Grand Total', 'Total'",
            "**FORMAT**: Numeric value only"
        ]
    ),

    "tax_amount": FieldConfig(
        name="tax_amount",
        display_name="Tax Amount",
        field_type=FieldType.NUMBER,
        description="Document tax amount",
        extraction_guidelines=[
            "**ANCHOR LABELS**: 'Tax Amount', 'Total Tax', 'GST Amount', 'VAT Amount'",
            "**FORMAT**: Numeric value only"
        ]
    )
}

GRN_ITEM_FIELDS: Dict[str, FieldConfig] = {
    "item_description": FieldConfig(
        name="item_description",
        display_name="Description",
        field_type=FieldType.STRING,
        description="Item description",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Description', 'Item', 'Product'",
            "**FORMAT**: Full product description"
        ]
    ),

    "item_part_no": FieldConfig(
        name="item_part_no",
        display_name="Part Number",
        field_type=FieldType.STRING,
        description="SKU or Part Number",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Part No', 'SKU', 'Item Code'",
            "**FORMAT**: Preserve exact format"
        ]
    ),

    "qty_ordered": FieldConfig(
        name="qty_ordered",
        display_name="Quantity Ordered",
        field_type=FieldType.NUMBER,
        description="Quantity expected/ordered",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Qty Ordered', 'Ordered', 'Expected', 'Advised'",
            "**FORMAT**: Numeric value",
            "**VALIDATION**: Must be positive"
        ]
    ),

    "qty_received": FieldConfig(
        name="qty_received",
        display_name="Quantity Received",
        field_type=FieldType.NUMBER,
        description="Actual quantity received",
        required=True,
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Qty Received', 'Received', 'Delivered', 'Actual'",
            "**FORMAT**: Numeric value",
            "**CRITICAL**: This is the PRIMARY quantity field for GRN"
        ]
    ),

    "qty_rejected": FieldConfig(
        name="qty_rejected",
        display_name="Quantity Rejected",
        field_type=FieldType.NUMBER,
        description="Quantity rejected/damaged",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Rejected', 'Damaged', 'Shortage', 'Defective'",
            "**FORMAT**: Numeric value",
            "**DEFAULT**: Return 0 if not specified"
        ]
    ),

    "qty_accepted": FieldConfig(
        name="qty_accepted",
        display_name="Quantity Accepted",
        field_type=FieldType.NUMBER,
        description="Quantity accepted into stock",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Accepted', 'OK', 'Passed', 'Into Stock'",
            "**FORMAT**: Numeric value",
            "**CALCULATION**: Usually qty_received - qty_rejected"
        ]
    ),

    "remarks": FieldConfig(
        name="remarks",
        display_name="Remarks",
        field_type=FieldType.STRING,
        description="Notes on damage or discrepancies",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Remarks', 'Notes', 'Comments', 'Inspection Notes'",
            "**FORMAT**: Free text",
            "**INCLUDE**: Quality issues, damage reports, packaging condition"
        ]
    ),

    "batch_no": FieldConfig(
        name="batch_no",
        display_name="Batch Number",
        field_type=FieldType.STRING,
        description="Batch or Lot number",
        extraction_guidelines=[
            "**COLUMN HEADERS**: 'Batch', 'Lot No', 'Serial No'",
            "**FORMAT**: Alphanumeric",
            "**CRITICAL**: Important for traceability"
        ]
    )
}


# =============================================================================
# PROMPT GENERATOR FUNCTIONS
# =============================================================================

def generate_field_prompt_section(fields: Dict[str, FieldConfig], section_name: str = "FIELDS", exclude_list: List[str] = None) -> str:
    if exclude_list is None: exclude_list = []
    
    lines = [f"### {section_name} TO EXTRACT:"]
    for _, config in fields.items():
        if config.name in exclude_list:
            continue
            
        req = " [REQUIRED]" if config.required else ""
        lines.append(f"\n- **{config.name}** ({config.display_name}){req}")
        lines.append(f"  - Description: {config.description}")
        if config.extraction_guidelines:
            lines.append("  - Guidelines:")
            for g in config.extraction_guidelines:
                lines.append(f"    • {g}")
    return "\n".join(lines)


def generate_extraction_prompt(doc_type, fields, item_fields, custom_instructions=None, exclude_fields=None):
    if exclude_fields is None: exclude_fields = []
    
    parts = [f"You are a forensic Data Extractor. Extract data from this {doc_type}.\n"]
    
    if custom_instructions:
        parts.append("### CRITICAL BUSINESS RULES:")
        for rule in custom_instructions:
            parts.append(f"- {rule}")
            
    parts.append("\n")
    parts.append(generate_field_prompt_section(fields, "DOCUMENT FIELDS", exclude_fields))
    
    if item_fields:
        parts.append("\n")
        parts.append(generate_field_prompt_section(item_fields, "LINE ITEM FIELDS", exclude_fields))
        parts.append("\n### LINE ITEMS INSTRUCTIONS:")
        parts.append("- Extract ALL line items from the table")
        parts.append("- **item_no**: HARDCODED RULE: ALWAYS set to '1'")

    parts.append("\n### OUTPUT:")
    parts.append("Return valid JSON strictly matching the provided schema. Use null for missing fields.")
    return "\n".join(parts)


# =============================================================================
# FINAL PROMPTS
# =============================================================================

CLASSIFICATION_PROMPT = """
You are an Expert Document Classification Router. Analyze this multi-page PDF and return a JSON mapping of page ranges to document type buckets.

IMPORTANT: Return ONLY valid JSON. No explanations, no prose, no markdown code blocks.

================================================================================
SECTION 1: DOCUMENT SPLITTING RULES
================================================================================

A NEW DOCUMENT RANGE starts when ANY of these occur:
1. The PRIMARY REFERENCE NUMBER changes (Invoice #, PO #, GRN #)
2. The HEADER ENTITY (Logo/Company Name) changes
3. Pagination resets (e.g., "Page 1 of X" appears again)

================================================================================
SECTION 2: EXCLUSION FILTER
================================================================================

IMMEDIATELY SKIP pages labeled as:
- Quote / Quotation
- Proforma Invoice
- Statement of Account
- Payment Remittance Advice
- Credit Note (unless explicitly treated as negative invoice)
- Certificate of Origin (standalone, not attached to invoice)

================================================================================
SECTION 3: DOCUMENT TYPE CLASSIFICATION
================================================================================

┌─────────────────┬──────────────────────────────┬──────────────────────────────────────────┐
│ BUCKET          │ PRIMARY KEYWORDS             │ STRUCTURAL MARKERS                       │
├─────────────────┼──────────────────────────────┼──────────────────────────────────────────┤
│ po              │ "Purchase Order"             │ • Generated by the BUYER                 │
│                 │ "PO Number"                  │ • Contains "Bill To" AND "Ship To"       │
│                 │ "Local Purchase Order" (LPO) │ • Signature is "Authorized Buyer"        │
│                 │ "Order Confirmation"         │ • Lists Payment Terms (Net 30, etc.)     │
│                 │                              │ • Header company is BUYER (has logo)     │
├─────────────────┼──────────────────────────────┼──────────────────────────────────────────┤
│ grn             │ "Goods Received Note"        │ • Focus on QUANTITIES (Ordered vs Recvd) │
│                 │ "Delivery Note" / "Docket"   │ • Often LACKS unit prices/totals         │
│                 │ "Packing List" / "Slip"      │ • Contains Warehouse/Invoice data      │
│                 │ "Receiving Report"           │   (e.g., "Vehicle Reg", "Bin Location")  │
│                 │ "Material Receipt"           │ • Checkboxes for inspection/QC           │
├─────────────────┼──────────────────────────────┼──────────────────────────────────────────┤
│ invoices        │ "Tax Invoice"                │ • Generated by the SELLER                │
│                 │ "Commercial Invoice"         │ • "Pay to" instructions / Bank Details   │
│                 │ "Bill" / "Bill of Supply"    │ • Total Amount Due is prominent          │
│                 │                              │ • Header company is SELLER (has logo)    │
└─────────────────┴──────────────────────────────┴──────────────────────────────────────────┘

MATCHING REQUIREMENT: Document must exhibit 2+ structural traits of the category.

================================================================================
SECTION 4: CONFLICT RESOLUTION LOGIC
================================================================================

### THE "PRICE" TEST (Invoice vs. GRN/Packing List)
Many GRNs and Packing Lists look like invoices but have no financial value.
RULE:
IF document lists items/quantities but NO prices/totals  → CLASSIFY AS GRN
IF document lists items with prices, tax, and total due  → CLASSIFY AS INVOICE

### THE "DIRECTION" TEST (Invoice vs. PO)
Both Invoices and POs have prices and totals.
RULE:
IF the Sender is demanding payment ("Pay to...")         → CLASSIFY AS INVOICE
IF the Sender is requesting goods ("Ship to...")         → CLASSIFY AS PO
IF Header logo is BUYER + "Bill To" is present           → CLASSIFY AS PO
IF Header logo is SELLER + Bank details present          → CLASSIFY AS INVOICE

================================================================================
SECTION 5: OUTPUT SPECIFICATION (Strict JSON Only)
================================================================================

Return ONLY this JSON structure. No other text.

{
  "po": [[start, end], ...],
  "grn": [[start, end], ...],
  "invoices": [[start, end], ...]
}

RULES:
- Pages are 1-indexed (first page = 1)
- [5, 10] means pages 5 through 10 inclusive
- NO overlapping ranges across categories
- Each page appears in exactly ONE category or is skipped (if excluded)
"""

INVOICE_PROMPT = generate_extraction_prompt(
    "Commercial Invoice",
    INVOICE_FIELDS,
    INVOICE_ITEM_FIELDS,
    [
        "**UNIVERSAL HEURISTIC**: Use visual alignment logic for tables.",
        "**ANCHOR LOGIC**: Logo usually denotes Seller. 'To' usually denotes Buyer.",
        "**MATH CHECK**: Total Amount should equal sum of Line Items (approx).",
        "**DATES**: Standardize all dates to YYYY-MM-DD.",
        "**INDIA GST RULE**: Look specifically for GSTINs (15 chars starting with state code e.g., 27...). If SGST/CGST/IGST are split, extract them individually into their specific fields."
    ],
    exclude_fields=["invoice_toi", "invoice_po_date"] # Excluded from main pass if doing 2-pass extraction
)

PO_PROMPT = generate_extraction_prompt(
    "Purchase Order",
    PO_FIELDS,
    PO_ITEM_FIELDS,
    [
        "**ROLE REVERSAL**: Unlike invoices, the entity in the HEADER/LOGO is the BUYER. The entity in 'To' or 'Vendor' block is the SELLER.",
        "**ADDRESS LOGIC**: Distinctly extract 'Bill To' vs 'Ship To'. If 'Ship To' is missing, fallback to 'Buyer Address' but prioritize specific shipping instructions.",
        "**DATES**: 'Date' is the Order Date. 'Delivery Date' or 'Required Date' is distinct—extract it at the line item level if specific to items.",
        "**FINANCIALS**: Extract Unit Prices and Totals if present. If tax is listed, ensure Total Amount includes it.",
        "**META**: Look for 'Payment Terms' (e.g., Net 30) and 'Shipping Method' (e.g., Air/Sea)."
    ]
)

GRN_PROMPT = generate_extraction_prompt(
    "Goods Received Note (GRN)",
    GRN_FIELDS,
    GRN_ITEM_FIELDS,
    [
        "**QUANTITY PRIORITY**: The most critical data is QUANTITY. Prices are often missing or irrelevant.",
        "**COLUMN MAPPING**: Carefully distinguish between: 'Qty Ordered', 'Qty Received' (Primary), and 'Qty Rejected'.",
        "**PO LINKING**: Aggressively search for 'PO Number', 'Order Ref', or 'Your Ref' to link this GRN to a Purchase Order.",
        "**QC DATA**: Extract text from 'Remarks' or 'Notes' columns regarding damage, wet cartons, or quality issues.",
        "**Invoice**: Extract 'Vehicle Reg' (Truck Number) and 'Carrier' if listed in the header."
    ]
) 
