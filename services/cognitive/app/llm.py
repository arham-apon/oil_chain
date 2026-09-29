"""Gemini via LangChain, wrapped in the ``gemini`` circuit breaker; every call is logged to ``ai_calls``.

The model name comes from ``GEMINI_MODEL`` (C22). At startup one probe call is made; if the model is unavailable the
first working fallback model is used and logged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fsp_shared.breaker import CircuitBreaker
from fsp_shared.config import Settings
from fsp_shared.exceptions import BreakerOpenError
from fsp_shared.logging import get_logger

from .metrics import GEMINI_CALLS, GEMINI_LATENCY

log = get_logger("cognitive.llm")
FALLBACK_MODELS = ("gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-flash-latest", "gemini-3.8-flash")
GROQ_FALLBACK_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")


class LLMUnavailable(Exception):
    pass


class GeminiLLM:
    def __init__(self, settings: Settings, breaker: CircuitBreaker, log_call=None) -> None:
        self.s = settings
        self.breaker = breaker
        self.log_call = log_call
        # LLM_PROVIDER=groq switches System 2 to Groq (OpenAI-compatible, LangChain ChatGroq); same breaker/fallbacks
        self.provider = settings.LLM_PROVIDER
        key = settings.GROQ_API_KEY if self.provider == "groq" else settings.GEMINI_API_KEY
        self.key = "invalid-chaos-key" if settings.CHAOS_BAD_AI_KEYS else key
        self.enabled = bool(self.key)
        self.model_name = settings.GROQ_MODEL if self.provider == "groq" else settings.GEMINI_MODEL
        self.timeout_s = settings.GEMINI_TIMEOUT_MS / 1000.0
        self._llm = None
        if self.enabled:
            self._llm = self._make(self.model_name)

    @property
    def label(self) -> str:
        """Source label stored with every LLM output (GEMINI | GROQ)."""
        return self.provider.upper()

    def _make(self, model: str, timeout: float | None = None):
        try:
            if self.provider == "groq":
                from langchain_groq import ChatGroq

                return ChatGroq(model=model, temperature=0.1, api_key=self.key,
                                timeout=timeout or self.timeout_s, max_retries=1)
            from langchain_google_genai import ChatGoogleGenerativeAI

            kwargs = {"thinking_budget": 0} if "2.5" in model else {}  # fast, no thinking
            return ChatGoogleGenerativeAI(model=model, temperature=0.1, api_key=self.key,
                                          timeout=timeout or self.timeout_s, max_retries=1, **kwargs)
        except Exception as exc:  # noqa: BLE001 - library missing/incompatible -> template mode
            log.warning("gemini_client_unavailable", error=str(exc)[:200])
            self.enabled = False
            return None

    async def probe(self) -> None:
        """C22: test the configured model at startup; fall back to an available Flash model if it is gone (404).
        Connectivity errors (DNS not ready, network down) are retried in the background with backoff."""
        if not self.enabled:
            return
        delay = 5.0
        while True:
            network_error = False
            fallbacks = GROQ_FALLBACK_MODELS if self.provider == "groq" else FALLBACK_MODELS
            for model in (self.model_name, *[m for m in fallbacks if m != self.model_name]):
                llm = self._make(model, timeout=15.0)
                if llm is None:
                    return
                try:
                    await asyncio.wait_for(llm.ainvoke("Reply with the single word OK."), 20.0)
                    if model != self.model_name:
                        log.warning("gemini_model_fallback", configured=self.model_name, using=model)
                    self.model_name, self._llm = model, self._make(model)
                    self.breaker.record_success()
                    log.info("gemini_ready", model=model)
                    return
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {str(exc)[:160]}"
                    log.warning("gemini_probe_failed", model=model, error=msg)
                    if "API key" in msg or "PERMISSION" in msg.upper() or "401" in msg or "403" in msg:
                        log.warning("gemini_key_rejected_using_templates")
                        return  # key problem: no model will work
                    if any(t in msg for t in ("ConnectError", "TimeoutError", "ConnectTimeout", "Name or service", "504", "503", "DEADLINE", "UNAVAILABLE")):
                        network_error = True
                        continue  # transient (network/overload): try the next model, retry the probe later if none works
            if not network_error:
                log.warning("gemini_unavailable_using_templates")
                return
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 2)

    def llm(self, tools: list | None = None, structured: type | None = None, timeout: float | None = None):
        if not self.enabled or self._llm is None:
            raise LLMUnavailable("GEMINI_API_KEY not configured")
        base = self._llm if timeout is None else self._make(self.model_name, timeout)
        if structured is not None:
            return base.with_structured_output(structured)
        if tools:
            return base.bind_tools(tools)
        return base

    async def call(self, runnable, payload: Any, purpose: str, *, timeout: float | None = None) -> Any:
        """Invoke ``runnable`` under the breaker; raises LLMUnavailable on any failure."""
        if not self.breaker.allow():
            self.breaker.record_fallback("breaker_open")
            raise LLMUnavailable("gemini breaker open")
        started = time.perf_counter()
        ok, err = False, None
        try:
            result = await asyncio.wait_for(runnable.ainvoke(payload), timeout or self.timeout_s)
            self.breaker.record_success()
            ok = True
            return result
        except BreakerOpenError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except Exception as exc:
            self.breaker.record_failure()
            self.breaker.record_fallback(type(exc).__name__)
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise LLMUnavailable(err) from exc
        finally:
            latency = time.perf_counter() - started
            GEMINI_LATENCY.observe(latency)
            GEMINI_CALLS.labels(str(ok).lower(), purpose).inc()
            if self.log_call:
                await self.log_call("gemini", purpose, int(latency * 1000), ok, err)
