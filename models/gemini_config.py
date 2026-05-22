import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

# Create a globally accessible client instance.
# It will use API_KEY or default to the environment variable if None.
client = genai.Client(api_key=API_KEY) if API_KEY else genai.Client()

def resolve_refs(schema, defs=None):
    """Recursively resolve $ref in schema using definitions."""
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
            if k in ["$defs", "definitions"]: continue
            new_schema[k] = resolve_refs(v, defs)
        return new_schema
    elif isinstance(schema, list):
        return [resolve_refs(item, defs) for item in schema]
    return schema

def clean_schema(schema):
    """
    Aggressively cleans Pydantic JSON schema for Gemini compatibility.
    Removes 'default', 'title', and handles 'anyOf' (Optional fields).
    """                             
    schema = resolve_refs(schema)
    
    def _clean(s):
        if isinstance(s, dict):
            # FIX: Handle Optional fields (anyOf: [type, null])
            if "anyOf" in s:
                for opt in s["anyOf"]:
                    if opt.get("type") != "null":
                        return _clean(opt) # Flatten 'anyOf' to the actual type
            
            # FIX: Remove forbidden keys
            for key in ["default", "title", "$defs", "definitions"]:
                if key in s:
                    del s[key]
            
            # Recurse
            for key, value in s.items():
                s[key] = _clean(value)
        
        elif isinstance(s, list):
            for i, item in enumerate(s):
                s[i] = _clean(item)
        return s

    return _clean(schema)

def get_generation_config(response_schema=None, enable_low_latency_thinking: bool = False):
    """
    Generates the model configuration.
    
    Args:
        response_schema: The Pydantic model for JSON output.
        enable_low_latency_thinking: If True, configures Gemini 3.0 for minimal reasoning latency.
    """
    config_kwargs = {
        "response_mime_type": "application/json",
        "temperature": 0.0,
    }
    
    # --- GEMINI 3.0 PRO SPECIFIC CONFIGURATION ---
    if enable_low_latency_thinking:
        config_kwargs["thinking_config"] = {
            "include_thoughts": False,  # Suppress thought tokens in output to prevent JSON parsing errors
            "thinking_level": "LOW"     # "LOW" minimizes latency for high-throughput tasks
        }
    
    if response_schema:
        try:
            if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
                raw_schema = response_schema.model_json_schema()
                config_kwargs["response_schema"] = clean_schema(raw_schema)
            else:
                config_kwargs["response_schema"] = response_schema
        except Exception as e:
            print(f"Schema cleaning failed: {e}")
            config_kwargs["response_schema"] = response_schema
            
    return types.GenerateContentConfig(**config_kwargs)

def get_http_options(timeout_seconds: int) -> types.HttpOptions:
    timeout_seconds = max(int(timeout_seconds), 10)
    return types.HttpOptions(timeout=timeout_seconds * 1000)

# Verified Model Configuration
CONFIG = {
    "CLASSIFIER_MODEL": "gemini-2.5-flash",
    "EXTRACTOR_MODEL": "gemini-3-pro-preview"
}
