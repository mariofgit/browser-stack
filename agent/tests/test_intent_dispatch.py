"""Pruebas sin FastAPI ni red: solo ``runtime.intent_dispatch``."""
from __future__ import annotations

import os

import pytest

from runtime.intent_dispatch import dispatch_intent


@pytest.fixture(autouse=True)
def clear_chart_period_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRICE_CHART_DEFAULT_PERIOD", raising=False)


@pytest.mark.parametrize(
    ("message", "tool", "ticker", "sources"),
    [
        ("dame el morning shot", "morning_shot", None, ["wsj", "nyt"]),
        ("morning shot solo wsj", "morning_shot", None, ["wsj"]),
        ("nyt morning shot", "morning_shot", None, ["nyt"]),
        ("resumen de noticias", "news_summary", None, None),
        ("noticias de reuters", "news_summary", None, None),
        ("por ahora necesito ver un grafico de voo", "price_chart", "VOO", None),
        ("muéstrame el gráfico de NVDA", "price_chart", "NVDA", None),
        ("grafico del ticker voo", "price_chart", "VOO", None),
        ("muestrame el chart de voo", "price_chart", "VOO", None),
        ("detalles de AAPL", "price_chart", "AAPL", None),
        ("cotización de AMXL.MX", "quote", "AMXL.MX", None),
        ("ticker NVDA", "quote", "NVDA", None),
        ("hola qué tal", "none", None, None),
    ],
)
def test_dispatch_intent(
    message: str,
    tool: str,
    ticker: str | None,
    sources: list[str] | None,
) -> None:
    got_tool, params = dispatch_intent(message)
    assert got_tool == tool
    if ticker is not None:
        assert params.get("ticker") == ticker
    if sources is not None:
        assert params.get("sources") == sources
    if tool == "news_summary":
        assert "source_ids" in params


def test_news_reuters_only_source_id() -> None:
    _tool, params = dispatch_intent("resumen de noticias de reuters")
    assert params.get("source_ids") == ["reuters"]


def test_chart_period_one_year(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRICE_CHART_DEFAULT_PERIOD", raising=False)
    _tool, params = dispatch_intent("gráfico de VOO último 1 año")
    assert params.get("period") == "1y"


def test_chart_period_default_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_CHART_DEFAULT_PERIOD", "6mo")
    _tool, params = dispatch_intent("chart de SPY")
    assert params.get("period") == "6mo"


def test_morning_overrides_news_keywords() -> None:
    """Si aparece morning shot, no debe clasificarse como news."""
    tool, _params = dispatch_intent("morning shot y también noticias")
    assert tool == "morning_shot"
