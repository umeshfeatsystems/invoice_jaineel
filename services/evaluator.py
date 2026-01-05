from typing import Dict, Any

def calculate_accuracy(predicted: Dict[str, Any], ground_truth: Dict[str, Any]) -> float:
    """
    Calculates simple accuracy score based on field matching.
    Flattens nested dictionaries for comparison.
    """
    def flatten(d, parent_key='', sep='_'):
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, dict):
                items.extend(flatten(v, new_key, sep=sep).items())
            elif isinstance(v, list):
                # For lists, we just compare length and maybe first item for simplicity in this demo
                # Or we can try to match items.
                # Let's just stringify for exact match or count items.
                # For this demo, let's keep it simple: compare string representation or length.
                items.append((new_key, str(v)))
            else:
                items.append((new_key, v))
        return dict(items)

    flat_pred = flatten(predicted)
    flat_gt = flatten(ground_truth)
    
    matches = 0
    total = 0
    
    for key, gt_val in flat_gt.items():
        total += 1
        pred_val = flat_pred.get(key)
        
        # Simple equality check, can be improved with fuzzy matching
        if str(pred_val).strip().lower() == str(gt_val).strip().lower():
            matches += 1
            
    if total == 0:
        return 0.0
        
    return (matches / total) * 100
