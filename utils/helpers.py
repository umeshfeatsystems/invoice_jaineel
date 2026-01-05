import logging
import os

# Pricing per 1M tokens (Estimated/Placeholder values)
# Input / Output
PRICING = {
    # Legacy / Fallback
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 3.50, "output": 10.50},
    
    # Your Active Models
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30}, # Assuming similar to 1.5 Flash
    "gemini-3-pro-preview": {"input": 3.50, "output": 10.50}, # Assuming similar to Pro tier
    
    # Default fallback
    "default": {"input": 3.50, "output": 10.50}
}

def calculate_cost(model_name, input_tokens, output_tokens):
    # Normalize model name to find pricing
    pricing = PRICING.get("default")
    
    # Try to find exact or partial match
    for key in PRICING:
        if key in model_name:
            pricing = PRICING[key]
            break
            
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    
    return round(input_cost + output_cost, 6)

def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger("Invoice_extraction")