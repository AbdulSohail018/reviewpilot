"""Model factory."""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from reviewpilot.config import ReviewConfig


def get_model(cfg: ReviewConfig) -> BaseChatModel:
    if cfg.provider == "mock":
        from reviewpilot.mock_llm import MockReviewer

        return MockReviewer()
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=cfg.resolved_model(), temperature=0, max_tokens=4096)
    if cfg.provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=cfg.resolved_model(), temperature=0)
    raise ValueError(f"unknown provider {cfg.provider}")
