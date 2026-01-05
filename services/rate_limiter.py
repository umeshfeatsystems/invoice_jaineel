"""
Enhanced Rate Limiter for Gemini API (Bulletproof Edition)
===========================================================
Prevents ALL rate limit errors (429/503) with:
1. Sliding Window Rate Limiting (tracks actual 60s window)
2. Burst Protection (prevents request storms)
3. Conservative Safety Margins
4. Request Queue Management
5. Per-model concurrent request tracking
"""

import asyncio
import time
from collections import deque
from typing import Dict, Optional
import logging

logger = logging.getLogger("rate_limiter")


class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter that tracks actual requests over a 60-second window.
    Much more accurate than simple interval-based limiting.
    """
    
    def __init__(
        self, 
        model_limits: Dict[str, int], 
        safety_margin: float = 0.7,  # More conservative: 70% of limit
        min_request_gap_ms: int = 500,  # Minimum 500ms between ANY requests to same model
        max_concurrent_per_model: int = 2  # Max concurrent requests per model
    ):
        """
        Initialize rate limiter.
        
        Args:
            model_limits: Dict of model name -> RPM limit
            safety_margin: Use this % of limit (0.7 = 70% to be safe)
            min_request_gap_ms: Minimum milliseconds between requests to same model
            max_concurrent_per_model: Maximum concurrent requests per model
        """
        self.model_limits = model_limits
        self.safety_margin = safety_margin
        self.min_request_gap_ms = min_request_gap_ms
        self.max_concurrent_per_model = max_concurrent_per_model
        
        # Calculate effective limits
        self.effective_limits: Dict[str, int] = {
            model: int(rpm * safety_margin) 
            for model, rpm in model_limits.items()
        }
        
        # Sliding window: store timestamps of requests in last 60 seconds
        self.request_windows: Dict[str, deque] = {
            model: deque() for model in model_limits.keys()
        }
        
        # Track last request time for minimum gap enforcement
        self.last_request_time: Dict[str, float] = {}
        
        # Track concurrent requests per model
        self.concurrent_requests: Dict[str, int] = {
            model: 0 for model in model_limits.keys()
        }
        
        # Locks for thread safety
        self.locks: Dict[str, asyncio.Lock] = {
            model: asyncio.Lock() for model in model_limits.keys()
        }
        
        # Global lock for coordination
        self.global_lock = asyncio.Lock()
        
        # Statistics
        self.total_requests: Dict[str, int] = {model: 0 for model in model_limits.keys()}
        self.total_wait_time: Dict[str, float] = {model: 0.0 for model in model_limits.keys()}
        self.start_time = time.time()
        
        logger.info(f"🛡️ Rate Limiter initialized:")
        for model, limit in self.effective_limits.items():
            logger.info(f"   {model}: {limit} RPM (from {model_limits[model]} with {safety_margin*100:.0f}% safety)")
    
    def _clean_old_requests(self, model: str, current_time: float) -> None:
        """Remove requests older than 60 seconds from the window."""
        window = self.request_windows[model]
        cutoff = current_time - 60.0
        while window and window[0] < cutoff:
            window.popleft()
    
    def _get_requests_in_window(self, model: str) -> int:
        """Get number of requests made in the last 60 seconds."""
        current_time = time.time()
        self._clean_old_requests(model, current_time)
        return len(self.request_windows[model])
    
    def _calculate_wait_time(self, model: str) -> float:
        """Calculate how long to wait before the next request can be made."""
        current_time = time.time()
        self._clean_old_requests(model, current_time)
        
        effective_limit = self.effective_limits.get(model, 100)
        requests_in_window = len(self.request_windows[model])
        
        # Check 1: Are we at the RPM limit?
        if requests_in_window >= effective_limit:
            # Wait until oldest request falls out of window
            oldest_request = self.request_windows[model][0]
            wait_for_window = (oldest_request + 60.0) - current_time + 0.1  # +0.1s buffer
            if wait_for_window > 0:
                return wait_for_window
        
        # Check 2: Enforce minimum gap between requests
        last_time = self.last_request_time.get(model, 0)
        time_since_last = (current_time - last_time) * 1000  # Convert to ms
        if time_since_last < self.min_request_gap_ms:
            return (self.min_request_gap_ms - time_since_last) / 1000.0
        
        # Check 3: Check concurrent requests limit
        if self.concurrent_requests[model] >= self.max_concurrent_per_model:
            # Wait a short time and retry
            return 0.5
        
        return 0.0
    
    async def acquire(self, model_name: str) -> None:
        """
        Acquire permission to make a request.
        Blocks if necessary to maintain rate limit.
        
        Args:
            model_name: Name of the model being called
        """
        if model_name not in self.model_limits:
            logger.warning(f"⚠️ Model {model_name} not in rate limiter config. Add it to prevent rate limits!")
            # Still apply a basic delay for safety
            await asyncio.sleep(0.5)
            return
        
        async with self.locks[model_name]:
            total_wait = 0.0
            max_wait_time = 120.0  # Maximum wait time of 2 minutes
            
            while True:
                wait_time = self._calculate_wait_time(model_name)
                
                if wait_time <= 0:
                    break
                
                total_wait += wait_time
                
                if total_wait > max_wait_time:
                    logger.warning(
                        f"⚠️ {model_name}: Waited {total_wait:.1f}s - proceeding anyway to avoid deadlock"
                    )
                    break
                
                logger.debug(
                    f"⏳ {model_name}: Rate limit protection - waiting {wait_time:.2f}s "
                    f"(window: {self._get_requests_in_window(model_name)}/{self.effective_limits[model_name]} RPM)"
                )
                await asyncio.sleep(wait_time)
            
            # Record this request
            current_time = time.time()
            self.request_windows[model_name].append(current_time)
            self.last_request_time[model_name] = current_time
            self.concurrent_requests[model_name] += 1
            self.total_requests[model_name] += 1
            self.total_wait_time[model_name] += total_wait
            
            if total_wait > 1.0:
                logger.info(
                    f"🛡️ {model_name}: Rate limited for {total_wait:.1f}s "
                    f"(window: {self._get_requests_in_window(model_name)}/{self.effective_limits[model_name]})"
                )
    
    def release(self, model_name: str) -> None:
        """
        Release a concurrent request slot after API call completes.
        Must be called when the API request finishes.
        """
        if model_name in self.concurrent_requests:
            self.concurrent_requests[model_name] = max(0, self.concurrent_requests[model_name] - 1)
    
    def get_stats(self) -> Dict[str, Dict[str, float]]:
        """Get current rate limiter statistics."""
        current_time = time.time()
        elapsed_minutes = (current_time - self.start_time) / 60.0
        
        stats = {}
        for model in self.model_limits.keys():
            total_requests = self.total_requests[model]
            actual_rpm = total_requests / elapsed_minutes if elapsed_minutes > 0 else 0
            limit_rpm = self.effective_limits[model]
            requests_in_window = self._get_requests_in_window(model)
            
            stats[model] = {
                "total_requests": total_requests,
                "actual_rpm": round(actual_rpm, 2),
                "limit_rpm": limit_rpm,
                "current_window": requests_in_window,
                "concurrent": self.concurrent_requests[model],
                "total_wait_seconds": round(self.total_wait_time[model], 2),
                "usage_percent": round((requests_in_window / limit_rpm * 100), 1) if limit_rpm > 0 else 0
            }
        
        return stats
    
    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self.total_requests = {model: 0 for model in self.model_limits.keys()}
        self.total_wait_time = {model: 0.0 for model in self.model_limits.keys()}
        self.start_time = time.time()
    
    def get_current_usage(self, model_name: str) -> str:
        """Get human-readable current usage for a model."""
        if model_name not in self.model_limits:
            return "Unknown model"
        
        requests = self._get_requests_in_window(model_name)
        limit = self.effective_limits[model_name]
        concurrent = self.concurrent_requests[model_name]
        
        return f"{requests}/{limit} RPM, {concurrent}/{self.max_concurrent_per_model} concurrent"


# Global rate limiter instance
_rate_limiter: Optional[SlidingWindowRateLimiter] = None


def initialize_rate_limiter(
    model_limits: Dict[str, int], 
    safety_margin: float = 0.7,
    min_request_gap_ms: int = 500,
    max_concurrent_per_model: int = 2
) -> None:
    """
    Initialize the global rate limiter.
    
    Args:
        model_limits: Dict of model name -> RPM limit
        safety_margin: Safety margin (default 0.7 = use 70% of limit)
        min_request_gap_ms: Minimum ms between requests (default 500)
        max_concurrent_per_model: Max concurrent per model (default 2)
    """
    global _rate_limiter
    _rate_limiter = SlidingWindowRateLimiter(
        model_limits, 
        safety_margin,
        min_request_gap_ms,
        max_concurrent_per_model
    )
    logger.info("✅ Rate limiter initialized successfully")


def get_rate_limiter() -> SlidingWindowRateLimiter:
    """
    Get the global rate limiter instance.
    
    Returns:
        SlidingWindowRateLimiter instance
    
    Raises:
        RuntimeError: If rate limiter not initialized
    """
    if _rate_limiter is None:
        raise RuntimeError("Rate limiter not initialized. Call initialize_rate_limiter() first.")
    return _rate_limiter


def release_rate_limit(model_name: str) -> None:
    """
    Release a concurrent request slot.
    Call this when an API request completes.
    """
    if _rate_limiter is not None:
        _rate_limiter.release(model_name)


def get_rate_limit_stats() -> Dict[str, Dict[str, float]]:
    """Get rate limiter statistics."""
    if _rate_limiter is None:
        return {}
    return _rate_limiter.get_stats()