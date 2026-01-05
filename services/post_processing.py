from typing import Dict, Any

class CurrencyService:
    @staticmethod
    def get_cbic_rate(currency_code: str) -> float:
        # Placeholder for live scraping - returns None as requested
        return None

class PostProcessor:
    def __init__(self):
        self.currency_service = CurrencyService()

    def process_invoice(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Applies business logic to the extracted dictionary."""
        
        # 1. FCA -> FOB Logic
        toi = data.get("invoice_toi", "") or ""
        if toi and "FCA" in str(toi).upper():
            data["invoice_toi"] = "FOB"
            
        # 2. Exchange Rate
        currency = data.get("currency")
        if currency:
            data["invoice_exchange_rate"] = self.currency_service.get_cbic_rate(currency)
            
        # 3. Description Concatenation & Cleanup
        if "items" in data and isinstance(data["items"], list):
            for item in data["items"]:
                desc = item.get("item_description", "") or ""
                part = item.get("item_part_no", "") or ""
                
                # If part no exists and isn't already in desc, append it
                if part and part not in desc:
                    item["item_description"] = f"{desc} {part}".strip()
                    
        return data

post_processor = PostProcessor()