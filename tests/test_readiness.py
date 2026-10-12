import importlib.util
import sys
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/verify_batch1.py"
spec = importlib.util.spec_from_file_location("batch1_readiness", path)
readiness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = readiness
spec.loader.exec_module(readiness)


def test_http_success_does_not_hide_business_auth_failure():
    result = readiness.summarize_envelope({"code": 2001, "data": None})
    assert result["status"] == "unauthorized"


def test_empty_and_malformed_success_do_not_pass_readiness():
    assert readiness.summarize_envelope({"code": 0, "data": {"item": []}})["status"] == "empty"
    assert readiness.summarize_envelope({"code": 0, "data": None})["status"] == "failed"
    assert readiness.summarize_envelope({"code": "0", "data": {"item": [{}]}})["status"] == "failed"
    assert readiness.summarize_envelope({"code": 0, "data": {"item": [None]}})["status"] == "failed"
    assert readiness.summarize_envelope({"code": 0, "data": {"item": [{}]}})["status"] == "failed"


def test_metadata_report_excludes_market_values():
    result = readiness.summarize_envelope({"code": 0, "data": {"timestamp": 1, "item": [
        {"thscode": "600519.SH", "last_price": 1234.56}]}})
    assert result["status"] == "passed" and result["row_count"] == 1
    assert "1234.56" not in str(result)


def test_secret_redaction_covers_keys_and_nested_values():
    result = readiness.redact({"fake-secret": ["prefix-fake-secret"]}, ["fake-secret"])
    assert result == {"[REDACTED]": ["prefix-[REDACTED]"]}


def test_invalid_tool_or_dependency_rejected():
    with pytest.raises(Exception):
        readiness.validate_plan('{"goal":"x","tasks":[{"id":"1","tool":"place_order"}]}')
    with pytest.raises(ValueError):
        readiness.validate_plan('{"goal":"x","tasks":[{"id":"1","tool":"get_daily_bars","depends_on":["2"]}]}')


def test_credentials_and_remote_cleartext_urls_rejected():
    assert not readiness.valid_url("https://user:secret@example.com/v1")
    assert not readiness.valid_url("http://example.com/v1")
    assert readiness.valid_url("http://127.0.0.1:11434/v1")
