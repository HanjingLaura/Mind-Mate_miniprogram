"""模型网关：统一百炼、智谱和降级策略。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, AsyncGenerator, Callable

from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger(__name__)
_provider_failed_until: dict[str, float] = {}


class ModelProviderUnavailable(RuntimeError):
    """No configured provider can serve the request."""


@dataclass(frozen=True)
class ModelRoute:
    name: str
    api_key: str
    base_url: str
    text_model: str
    vision_model: str

    def model_for(self, has_image: bool) -> str:
        return self.vision_model if has_image else self.text_model


def _route(name: str) -> ModelRoute | None:
    if name == "bailian" and settings.DASHSCOPE_API_KEY:
        return ModelRoute(
            "bailian",
            settings.DASHSCOPE_API_KEY,
            settings.BAILIAN_BASE_URL,
            settings.BAILIAN_MODEL,
            settings.BAILIAN_VISION_MODEL,
        )
    if name == "zhipu" and settings.ZHIPU_API_KEY:
        return ModelRoute(
            "zhipu",
            settings.ZHIPU_API_KEY,
            settings.ZHIPU_BASE_URL,
            settings.ZHIPU_MODEL,
            settings.ZHIPU_VISION_MODEL,
        )
    return None


def get_model_routes(has_image: bool = False) -> list[ModelRoute]:
    """Return ordered, configured routes without exposing secrets."""
    preferred = settings.LLM_PROVIDER if settings.LLM_PROVIDER in {"auto", "bailian", "zhipu"} else "auto"
    names = ["bailian", "zhipu"] if preferred in {"auto", "bailian"} else ["zhipu", "bailian"]
    routes = [item for name in names if (item := _route(name)) is not None]
    if not routes:
        raise ModelProviderUnavailable("未配置可用模型服务，请在 backend/.env 填写 DASHSCOPE_API_KEY 或 ZHIPU_API_KEY")
    return routes


def describe_routes() -> dict[str, Any]:
    routes = get_model_routes()
    return {
        "preferred": settings.LLM_PROVIDER,
        "active": routes[0].name,
        "fallbacks": [item.name for item in routes[1:]],
        "text_models": {item.name: item.text_model for item in routes},
        "vision_models": {item.name: item.vision_model for item in routes},
    }


@lru_cache(maxsize=4)
def _client(provider: str, api_key: str, base_url: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=settings.MODEL_REQUEST_TIMEOUT_SECONDS, max_retries=0)


def _provider_is_cooling_down(name: str) -> bool:
    return _provider_failed_until.get(name, 0) > time.monotonic()


def _mark_provider_failure(name: str) -> None:
    _provider_failed_until[name] = time.monotonic() + settings.AGENT_PROVIDER_COOLDOWN_SECONDS


def _mark_provider_success(name: str) -> None:
    _provider_failed_until.pop(name, None)


async def stream_completion(
    messages: list[dict[str, Any]],
    *,
    has_image: bool = False,
    temperature: float = 0.9,
    max_tokens: int = 500,
    provider_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream from the preferred route and fall back before any token leaks."""
    last_error: Exception | None = None
    routes = get_model_routes(has_image)
    candidates = [route for route in routes if not _provider_is_cooling_down(route.name)] or routes[:1]
    for route in candidates:
        started = False
        try:
            response = await _client(route.name, route.api_key, route.base_url).chat.completions.create(
                model=route.model_for(has_image),
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if provider_callback:
                provider_callback(route.name)
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    started = True
                    yield chunk.choices[0].delta.content
            _mark_provider_success(route.name)
            return
        except Exception as exc:
            last_error = exc
            _mark_provider_failure(route.name)
            logger.warning("模型供应商 %s 调用失败%s", route.name, "，已尝试降级" if not started else "", exc_info=True)
            if started:
                raise
    raise ModelProviderUnavailable("模型服务暂时不可用") from last_error


async def complete(
    messages: list[dict[str, Any]],
    *,
    has_image: bool = False,
    temperature: float = 0.9,
    max_tokens: int = 500,
) -> tuple[str, str]:
    """Return text and the provider that produced it."""
    last_error: Exception | None = None
    routes = get_model_routes(has_image)
    candidates = [route for route in routes if not _provider_is_cooling_down(route.name)] or routes[:1]
    for route in candidates:
        try:
            response = await _client(route.name, route.api_key, route.base_url).chat.completions.create(
                model=route.model_for(has_image),
                messages=messages,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            _mark_provider_success(route.name)
            return response.choices[0].message.content or "", route.name
        except Exception as exc:
            last_error = exc
            _mark_provider_failure(route.name)
            logger.warning("模型供应商 %s 调用失败，准备降级", route.name, exc_info=True)
    raise ModelProviderUnavailable("模型服务暂时不可用") from last_error
