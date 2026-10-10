"""LLM helpers for TravelAgntcy. They never raise: on any failure they return the fallback."""
import asyncio
import logging
import os

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("travel.llm")

TIMEOUT_SECONDS = float(os.getenv("TRAVEL_LLM_TIMEOUT", "20"))
_warned = False


def llm_disabled() -> bool:
    """True when we should skip the LLM and use backup text instead."""
    if os.getenv("TRAVEL_DISABLE_LLM", "").strip().lower() in ("1", "true", "yes"):
        return True
    from config.config import LLM_MODEL
    return not LLM_MODEL


def _warn_once(problem: Exception) -> None:
    """Log the first failure so a broken key doesn't hide behind fallback text."""
    global _warned
    if not _warned:
        logger.warning("LLM call failed, using fallback text: %r", problem)
        _warned = True


async def llm_text(prompt: str, fallback: str, *, system: str | None = None) -> str:
    """Ask the LLM for text. Returns `fallback` if anything goes wrong."""
    if llm_disabled():
        return fallback
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    try:
        from common.llm import get_llm
        reply = await asyncio.wait_for(
            get_llm(streaming=False).ainvoke(messages), TIMEOUT_SECONDS
        )
        text = reply.content if isinstance(reply.content, str) else ""
        return text.strip() or fallback
    except Exception as problem:
        _warn_once(problem)
        return fallback


async def llm_structured(schema, messages, fallback=None):
    """Ask the LLM to fill in `schema` (a Pydantic model). Returns `fallback` if anything goes wrong."""
    if llm_disabled():
        return fallback
    try:
        from common.llm import get_llm
        structured = get_llm(streaming=False).with_structured_output(schema)
        result = await asyncio.wait_for(structured.ainvoke(messages), TIMEOUT_SECONDS)
        return result if result is not None else fallback
    except Exception as problem:
        _warn_once(problem)
        return fallback