"""config.yaml is the source for non-secret settings."""

from app.config import resolve_settings


def test_openai_preset_fills_blank_model_and_url():
    resolved = resolve_settings({"ai": {"provider": "openai"}})
    assert resolved["ai_provider"] == "openai"
    assert resolved["ai_model"] == "gpt-4.1-mini"
    assert resolved["ai_base_url"] == "https://api.openai.com/v1"
    assert resolved["checker_model"] == "gpt-4.1-mini"


def test_explicit_model_overrides_the_preset():
    resolved = resolve_settings(
        {
            "ai": {
                "provider": "openai",
                "model": "gpt-4.1",
                "base_url": "https://example.invalid/v1",
            }
        }
    )
    assert resolved["ai_model"] == "gpt-4.1"
    assert resolved["ai_base_url"] == "https://example.invalid/v1"


def test_negative_experience_max_is_open_ended():
    resolved = resolve_settings({"screening": {"experience_max_years": -1}})
    assert resolved["experience_max_years"] is None


def test_scrape_lists_and_scheduler_come_from_the_mapping():
    resolved = resolve_settings(
        {
            "scrape": {"sites": ["indeed"], "search_terms": ["ML Engineer"], "proxies": []},
            "scheduler": {"interval_minutes": 30, "timezone": "UTC"},
            "tracing": {"otel_endpoint": "http://localhost:4318"},
        }
    )
    assert resolved["sites"] == ["indeed"]
    assert resolved["search_terms"] == ["ML Engineer"]
    assert resolved["scheduler"]["interval_minutes"] == 30
    assert resolved["scheduler"]["timezone"] == "UTC"
    assert resolved["otel_endpoint"] == "http://localhost:4318"
    assert resolved["rescore_after_hours"] == 72
