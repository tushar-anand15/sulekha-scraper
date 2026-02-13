"""
LLM Service for Sulekha Project Extraction.

Ported from EduCorrect - handles AI-powered extraction using Gemini models.

Uses OpenAI SDK with Gemini's OpenAI-compatible endpoint for reliability.

Features:
- Vision/multimodal processing for PDF tiles
- Structured outputs for extraction via Pydantic schemas
- Automatic JSON parsing with fallback strategies
- Retry with exponential backoff
- Usage tracking and cost calculation
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

from pydantic import BaseModel, ValidationError

# OpenAI SDK (used for Gemini via compatibility endpoint)
try:
    from openai import AsyncOpenAI, OpenAI
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    OPENAI_SDK_AVAILABLE = False
    AsyncOpenAI = None
    OpenAI = None

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
ImageInput = Union[bytes, str, pathlib.Path]


class StructuredOutputParseError(Exception):
    """Raised when structured output parsing fails."""
    
    def __init__(self, message: str, raw_content: str = "", error_type: str = "json"):
        super().__init__(message)
        self.raw_content = raw_content
        self.error_type = error_type


@dataclass
class ModelCapabilities:
    """Capabilities for each model."""
    vision: bool = False
    json_mode: bool = False
    max_tokens: int = 4096
    context_window: int = 8192


@dataclass
class ModelConfig:
    """Configuration for each model."""
    model_id: str
    capabilities: ModelCapabilities
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0


# Model Registry - Gemini models
# Prices from: https://ai.google.dev/gemini-api/docs/pricing (Jan 2025)
MODEL_REGISTRY: Dict[str, ModelConfig] = {
    # Gemini 2.5 Pro: Best for complex reasoning
    "gemini-2.5-pro": ModelConfig(
        model_id="gemini-2.5-pro",
        capabilities=ModelCapabilities(
            vision=True,
            json_mode=True,
            max_tokens=65536,
            context_window=1048576,
        ),
        cost_per_1k_input=0.00125,    # $1.25 per 1M tokens
        cost_per_1k_output=0.01000,   # $10.00 per 1M tokens
    ),
    # Gemini 2.5 Flash: Recommended - good balance
    "gemini-2.5-flash": ModelConfig(
        model_id="gemini-2.5-flash",
        capabilities=ModelCapabilities(
            vision=True,
            json_mode=True,
            max_tokens=65536,
            context_window=1048576,
        ),
        cost_per_1k_input=0.0003,     # $0.30 per 1M tokens
        cost_per_1k_output=0.0025,    # $2.50 per 1M tokens
    ),
    # Gemini 2.5 Flash-Lite: Most cost effective
    "gemini-2.5-flash-lite": ModelConfig(
        model_id="gemini-2.5-flash-lite",
        capabilities=ModelCapabilities(
            vision=True,
            json_mode=True,
            max_tokens=65536,
            context_window=1048576,
        ),
        cost_per_1k_input=0.0001,     # $0.10 per 1M tokens
        cost_per_1k_output=0.0004,    # $0.40 per 1M tokens
    ),
    # Gemini 2.0 Flash: Fast multimodal
    "gemini-2.0-flash": ModelConfig(
        model_id="gemini-2.0-flash",
        capabilities=ModelCapabilities(
            vision=True,
            json_mode=True,
            max_tokens=8192,
            context_window=1048576,
        ),
        cost_per_1k_input=0.0001,
        cost_per_1k_output=0.0004,
    ),
}


@dataclass
class UsageStats:
    """Track token usage and costs."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    requests: int = 0
    errors: int = 0
    start_time: datetime = field(default_factory=datetime.now)

    def add_usage(self, input_tokens: int, output_tokens: int, cost: float):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_cost += cost
        self.requests += 1

    def add_error(self):
        self.errors += 1

    def get_summary(self) -> Dict[str, Any]:
        duration = (datetime.now() - self.start_time).total_seconds()
        return {
            "total_requests": self.requests,
            "total_errors": self.errors,
            "total_input_tokens": self.input_tokens,
            "total_output_tokens": self.output_tokens,
            "total_cost_usd": round(self.total_cost, 6),
            "duration_seconds": round(duration, 2),
        }


class ImageProcessor:
    """Handle image input processing."""

    @staticmethod
    def detect_mime_type(data: bytes) -> str:
        """Detect MIME type from image data."""
        if data.startswith(b"\x89PNG"):
            return "image/png"
        elif data.startswith(b"\xff\xd8"):
            return "image/jpeg"
        elif data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"  # Default

    @staticmethod
    def process_image(image: ImageInput) -> Dict[str, Any]:
        """Convert image input to OpenAI-compatible format."""
        if isinstance(image, bytes):
            mime_type = ImageProcessor.detect_mime_type(image)
            b64_data = base64.b64encode(image).decode("utf-8")
            url = f"data:{mime_type};base64,{b64_data}"

        elif isinstance(image, pathlib.Path) or (
            isinstance(image, str) and os.path.isfile(image)
        ):
            path = pathlib.Path(image)
            if not path.exists():
                raise FileNotFoundError(f"Image file not found: {image}")

            with open(path, "rb") as f:
                data = f.read()

            mime_type = mimetypes.guess_type(str(path))[0] or ImageProcessor.detect_mime_type(data)
            b64_data = base64.b64encode(data).decode("utf-8")
            url = f"data:{mime_type};base64,{b64_data}"

        elif isinstance(image, str):
            # Assume it's a URL or data URI
            if image.startswith(("http://", "https://", "data:")):
                url = image
            else:
                raise ValueError(f"Invalid image string: {image}")
        else:
            raise TypeError(f"Unsupported image type: {type(image)}")

        return {"type": "image_url", "image_url": {"url": url}}

    @staticmethod
    def prepare_vision_input(images: List[ImageInput]) -> List[Dict[str, Any]]:
        """Process multiple images."""
        return [ImageProcessor.process_image(img) for img in images]


def parse_json_with_fallbacks(content: str) -> Dict[str, Any]:
    """
    Parse JSON with sophisticated fallback strategies.
    
    Handles common LLM output issues:
    - Markdown code blocks
    - Mixed content with explanations
    - Python literals (True/False/None)
    """
    if not content or not content.strip():
        raise ValueError("Empty content provided")
    
    def apply_json_fixes(text: str) -> str:
        if not text:
            return text
        
        # Remove comments
        text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
        
        # Fix trailing commas
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*]', ']', text)
        
        # Fix Python literals
        text = re.sub(r'\bTrue\b', 'true', text)
        text = re.sub(r'\bFalse\b', 'false', text)
        text = re.sub(r'\bNone\b', 'null', text)
        
        return text
    
    # Strategy 1: Direct JSON parsing
    try:
        content_stripped = content.strip()
        if content_stripped.startswith(("{", "[")):
            return json.loads(content_stripped)
    except json.JSONDecodeError:
        pass
    
    # Strategy 2: Extract from markdown code blocks
    try:
        markdown_patterns = [
            r'```json\s*\n?(.*?)\n?```',
            r'```\s*\n?(.*?)\n?```',
        ]
        
        for pattern in markdown_patterns:
            matches = re.findall(pattern, content, re.DOTALL | re.IGNORECASE)
            for match in matches:
                try:
                    cleaned = match.strip()
                    if cleaned:
                        return json.loads(cleaned)
                except json.JSONDecodeError:
                    try:
                        fixed = apply_json_fixes(cleaned)
                        return json.loads(fixed)
                    except:
                        continue
    except:
        pass
    
    # Strategy 3: Find first { or [ and extract balanced JSON
    try:
        start_idx = -1
        for i, char in enumerate(content):
            if char in '{[':
                start_idx = i
                break
        
        if start_idx >= 0:
            open_char = content[start_idx]
            close_char = '}' if open_char == '{' else ']'
            depth = 0
            in_string = False
            escape_next = False
            
            for i in range(start_idx, len(content)):
                char = content[i]
                
                if escape_next:
                    escape_next = False
                    continue
                
                if char == '\\':
                    escape_next = True
                    continue
                
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                
                if in_string:
                    continue
                
                if char == open_char:
                    depth += 1
                elif char == close_char:
                    depth -= 1
                    if depth == 0:
                        json_str = content[start_idx:i+1]
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            fixed = apply_json_fixes(json_str)
                            return json.loads(fixed)
    except:
        pass
    
    # If all strategies fail
    preview = content[:200] + "..." if len(content) > 200 else content
    raise ValueError(f"Could not parse JSON from content. Preview: {preview}")


class LLMService:
    """
    LLM Service for Sulekha Project Extraction.

    Uses OpenAI SDK with Gemini's OpenAI-compatible endpoint.
    
    Features:
    - Gemini vision support for processing PDF tiles
    - Structured outputs for extraction via Pydantic schemas
    - Automatic retry with exponential backoff
    - Usage tracking and cost calculation
    """

    # Gemini OpenAI-compatible endpoint
    GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        timeout: float = 300.0,
        backoff_multiplier: float = 1.0,
        enable_logging: bool = True,
    ):
        """
        Initialize the LLM service.
        
        Args:
            api_key: Gemini API key
            max_retries: Number of retries on failure
            timeout: Request timeout in seconds
            backoff_multiplier: Multiplier for exponential backoff
            enable_logging: Whether to log usage stats
        """
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._backoff_multiplier = backoff_multiplier
        self._enable_logging = enable_logging
        self._usage_stats: Dict[str, UsageStats] = {}
        self._async_client: Optional[AsyncOpenAI] = None
        self._sync_client: Optional[OpenAI] = None
        
    def _ensure_configured(self, async_client: bool = True):
        """Ensure OpenAI client is initialized for Gemini."""
        if async_client:
            if self._async_client is not None:
                return self._async_client
        else:
            if self._sync_client is not None:
                return self._sync_client
            
        if not OPENAI_SDK_AVAILABLE:
            raise RuntimeError("openai package is not installed. Install with: pip install openai")
        
        if not self._api_key:
            raise RuntimeError("API key not provided")
        
        # Create OpenAI client pointing to Gemini's compatibility endpoint
        if async_client:
            self._async_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self.GEMINI_BASE_URL,
                timeout=self._timeout,
            )
            return self._async_client
        else:
            self._sync_client = OpenAI(
                api_key=self._api_key,
                base_url=self.GEMINI_BASE_URL,
                timeout=self._timeout,
            )
            return self._sync_client

    @staticmethod
    def get_supported_models() -> List[str]:
        """Get list of supported models."""
        return list(MODEL_REGISTRY.keys())

    @staticmethod
    def get_model_capabilities(model_name: str) -> ModelCapabilities:
        """Get capabilities for a specific model."""
        if model_name not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model: {model_name}")
        return MODEL_REGISTRY[model_name].capabilities

    def calculate_cost(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Calculate cost for token usage."""
        config = MODEL_REGISTRY.get(model)
        if not config:
            return 0.0

        input_cost = (input_tokens / 1000) * config.cost_per_1k_input
        output_cost = (output_tokens / 1000) * config.cost_per_1k_output
        return input_cost + output_cost

    def _build_messages(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        images: Optional[List[ImageInput]] = None,
        structured_output_schema: Optional[Type[BaseModel]] = None,
    ) -> List[Dict[str, Any]]:
        """Build messages list for the API call."""
        messages = []
        
        # Add system prompt if provided
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # Build user message content
        actual_prompt = user_prompt
        
        # Add schema instruction for structured output (prompt engineering approach)
        if structured_output_schema:
            schema = structured_output_schema.model_json_schema()
            actual_prompt += f"\n\nReturn ONLY valid JSON matching this schema:\n```json\n{json.dumps(schema, indent=2)}\n```"
        
        # Handle images if present (multimodal)
        if images:
            content_parts: List[Dict[str, Any]] = []
            if actual_prompt:
                content_parts.append({"type": "text", "text": actual_prompt})
            content_parts.extend(ImageProcessor.prepare_vision_input(images))
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": actual_prompt})
        
        return messages

    async def chat(
        self,
        *,
        model: str,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        images: Optional[List[ImageInput]] = None,
        structured_output_schema: Optional[Type[BaseModel]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Union[str, BaseModel]:
        """
        Send a chat completion request using Gemini via OpenAI-compatible endpoint.

        Args:
            model: Model name to use (e.g., "gemini-2.5-flash")
            user_prompt: User message
            system_prompt: Optional system prompt
            images: Optional images to include (bytes or file paths)
            structured_output_schema: Pydantic model for structured output
            temperature: Sampling temperature (0.1 recommended for extraction)
            max_tokens: Maximum tokens to generate

        Returns:
            String response or Pydantic model (if structured)
        """
        client = self._ensure_configured(async_client=True)
        
        if model not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model: {model}. Supported: {list(MODEL_REGISTRY.keys())}")

        model_config = MODEL_REGISTRY[model]
        
        # Build messages
        messages = self._build_messages(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            images=images,
            structured_output_schema=structured_output_schema,
        )
        
        # Build request payload
        payload: Dict[str, Any] = {
            "model": model_config.model_id,
            "messages": messages,
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        
        # Initialize usage tracking
        if model not in self._usage_stats:
            self._usage_stats[model] = UsageStats()
        
        # Retry loop
        last_error = None
        for attempt in range(self._max_retries):
            try:
                start_time = time.time()
                
                # Make async request
                response = await client.chat.completions.create(**payload)
                
                latency_ms = int((time.time() - start_time) * 1000)
                
                # Check response
                if not response.choices:
                    raise RuntimeError("Gemini returned no choices - response was blocked or empty")
                
                message = response.choices[0].message
                content = message.content or ""
                
                # Track usage
                input_tokens = 0
                output_tokens = 0
                cost = 0.0
                if hasattr(response, 'usage') and response.usage:
                    input_tokens = response.usage.prompt_tokens or 0
                    output_tokens = response.usage.completion_tokens or 0
                    cost = self.calculate_cost(model, input_tokens, output_tokens)
                    self._usage_stats[model].add_usage(input_tokens, output_tokens, cost)
                    
                    if self._enable_logging:
                        logger.info(
                            f"[LLM] Model: {model}, In: {input_tokens}, Out: {output_tokens}, "
                            f"Cost: ${cost:.6f}, Latency: {latency_ms}ms"
                        )
                
                # Calculate input/output costs separately
                model_config = MODEL_REGISTRY.get(model)
                input_cost = (input_tokens / 1000) * model_config.cost_per_1k_input if model_config else 0.0
                output_cost = (output_tokens / 1000) * model_config.cost_per_1k_output if model_config else 0.0
                
                # Build usage info for this call
                usage_info = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "input_cost_usd": round(input_cost, 8),
                    "output_cost_usd": round(output_cost, 8),
                    "total_cost_usd": round(cost, 8),
                    "latency_ms": latency_ms,
                    "model": model,
                }
                
                # Handle structured output
                if structured_output_schema:
                    try:
                        data = parse_json_with_fallbacks(content)
                        parsed_result = structured_output_schema(**data)
                        # Attach usage info to the result
                        parsed_result._usage_info = usage_info
                        return parsed_result
                    except ValueError as parse_error:
                        raise StructuredOutputParseError(
                            str(parse_error),
                            raw_content=content,
                            error_type="json"
                        )
                    except ValidationError as val_error:
                        raise StructuredOutputParseError(
                            f"Pydantic validation failed: {str(val_error)}",
                            raw_content=content,
                            error_type="validation"
                        )
                
                return content
                
            except StructuredOutputParseError:
                # Don't retry parse errors on the last attempt
                if attempt >= self._max_retries - 1:
                    raise
                last_error = None  # Will retry
                await asyncio.sleep(self._backoff_multiplier * (2 ** attempt))
                
            except Exception as e:
                self._usage_stats[model].add_error()
                last_error = e
                
                if attempt < self._max_retries - 1:
                    wait_time = self._backoff_multiplier * (2 ** attempt)
                    logger.warning(f"[LLM] Attempt {attempt + 1} failed: {str(e)}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"[LLM] All {self._max_retries} attempts failed for model {model}")
                    raise
        
        if last_error:
            raise last_error
        raise RuntimeError("Unexpected error in LLM chat")

    def chat_sync(self, **kwargs) -> Union[str, BaseModel]:
        """Synchronous wrapper for chat method."""
        return asyncio.run(self.chat(**kwargs))

    def get_usage_stats(self, model: Optional[str] = None) -> Dict[str, Any]:
        """Get usage statistics."""
        if model:
            if model in self._usage_stats:
                return {model: self._usage_stats[model].get_summary()}
            return {}

        return {
            model: stats.get_summary() for model, stats in self._usage_stats.items()
        }

    def reset_usage_stats(self, model: Optional[str] = None):
        """Reset usage statistics."""
        if model:
            if model in self._usage_stats:
                self._usage_stats[model] = UsageStats()
        else:
            self._usage_stats.clear()


# Factory function for easy instantiation
def create_llm_service(
    api_key: str,
    **kwargs
) -> LLMService:
    """
    Create an LLM service instance.
    
    Args:
        api_key: Gemini API key (get from https://aistudio.google.com/apikey)
        **kwargs: Additional arguments for LLMService
    
    Returns:
        Configured LLMService instance
    """
    return LLMService(
        api_key=api_key,
        max_retries=kwargs.get('max_retries', 3),
        timeout=kwargs.get('timeout', 300.0),
        backoff_multiplier=kwargs.get('backoff_multiplier', 1.0),
        enable_logging=kwargs.get('enable_logging', True),
    )
