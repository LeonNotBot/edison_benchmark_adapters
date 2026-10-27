"""Classify actual API failure events and official verifier evidence, offline."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_tb2.py"
spec = importlib.util.spec_from_file_location("diagnostic_adapter", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
from trial_diagnostics import diagnose_trial


def write_agent(directory, *events):
    (directory / "agent").mkdir(exist_ok=True)
    (directory / "agent/claude-code.txt").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events))


def failure(text, status):
    return {"type": "result", "is_error": True, "terminal_reason": "api_error",
            "api_error_status": status, "result": text}


def test_billing_429_does_not_trigger_rate_limit_fail_fast():
    billing = 'RateLimitError {"code": 429, "message": "insufficient_quota"}'
    limited = 'RateLimitError {"code": 429, "message": "Too many requests"}'
    assert not adapter.is_model_rate_limit_error(billing)
    assert adapter.is_model_rate_limit_error(limited)
    assert adapter.is_model_rate_limit_error(billing + "\n" + limited)


@pytest.mark.parametrize("text,status,code", [
    ("Failed to authenticate. API Error: 403 用户额度不足, 剩余额度: ＄-37.955678", 403, "model_quota_exhausted"),
    ("API Error: 402 Insufficient credits. Add more credits", 402, "model_quota_exhausted"),
    ('API Error: 429 {"error":{"code":"insufficient_quota"}}', 429, "model_quota_exhausted"),
    ("Your credit balance is too low to access the API", 400, "model_quota_exhausted"),
    ("billing_hard_limit_reached", 400, "model_quota_exhausted"),
    ("余额不足", 403, "model_quota_exhausted"),
    ("You exceeded your current quota, please check your plan and billing details", 429, "model_quota_exhausted"),
    ("Too many requests", 429, "model_rate_limited"),
    ("Rate limit exceeded", None, "model_rate_limited"),
    ("Permission denied", 403, "model_auth_error"),
    ("invalid_api_key", 401, "model_auth_error"),
    ("upstream failure", 503, "model_api_error"),
    ("Quota exceeded for tokens per minute", 429, "model_rate_limited"),
])
def test_provider_error_variants(tmp_path, text, status, code):
    write_agent(tmp_path, failure(text, status))
    diag = diagnose_trial(tmp_path, {"exception_info": {"exception_type": "UnknownApiError"}})
    assert diag["code"] == code
    assert diag["execution_error"] is True
    assert diag["source"] == "agent/claude-code.txt"
    assert diag["evidence"]


def test_do_not_match_tool_output_or_normal_model_text_or_recovered_errors(tmp_path):
    events = [
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "API Error: 402 insufficient credits"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "用户额度不足"}]}},
    ]
    write_agent(tmp_path, *events)
    assert diagnose_trial(tmp_path, {}, 0)["code"] == "verifier_not_passed"
    write_agent(tmp_path, {"is_api_error_message": True, "message": {"content": [{"type": "text", "text": "API Error: 402 insufficient credits"}]}},
                {"type": "result", "is_error": False, "result": "Recovered"})
    assert diagnose_trial(tmp_path, {}, 1)["code"] == "reward_positive"


def test_partial_log_api_event_and_redaction(tmp_path):
    write_agent(tmp_path, {"is_api_error_message": True, "message": {"content": [{"type": "text", "text": 'API Error: 403 用户额度不足 api_key="private-secret" Authorization: Bearer token-secret sk-secret123456'}]}})
    diag = diagnose_trial(tmp_path, {})
    assert diag["code"] == "model_quota_exhausted"
    for secret in ("private-secret", "token-secret", "sk-secret123456"):
        assert secret not in diag["evidence"]


@pytest.mark.parametrize("summary,code,passed,failed", [
    ("8 failed, 32 passed in 0.20s", "verifier_tests_failed", 32, 8),
    ("4 failed in 1.06s", "verifier_tests_failed", 0, 4),
    ("1 passed, 2 errors in 2.00s", "verifier_error", 1, 0),
])
def test_official_pytest_summary(tmp_path, summary, code, passed, failed):
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier/test-stdout.txt").write_text(f"==== test session starts ====\n==== {summary} ====\n")
    diag = diagnose_trial(tmp_path, {}, 0)
    assert diag["code"] == code
    assert diag["verifier_tests"]["passed"] == passed
    assert diag["verifier_tests"]["failed"] == failed
    assert diag["execution_error"] == (code == "verifier_error")


def test_ctrf_and_api_failure_priority(tmp_path):
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier/ctrf.json").write_text(json.dumps({"results": {"summary": {"tests": 40, "passed": 32, "failed": 8}}}))
    write_agent(tmp_path, failure("API Error: 403 用户额度不足", 403))
    diag = diagnose_trial(tmp_path, {}, 0)
    assert diag["code"] == "model_quota_exhausted"
    assert diag["verifier_tests"]["passed"] == 32
    assert diag["verifier_tests"]["source"] == "verifier/ctrf.json"


@pytest.mark.parametrize("ctrf", ["not json", '{"results":{"summary":[]}}', 'null'])
def test_missing_malformed_evidence_remains_unknown(tmp_path, ctrf):
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier/ctrf.json").write_text(ctrf)
    write_agent(tmp_path, {"type": "assistant", "message": {"content": "insufficient credits"}})
    diag = diagnose_trial(tmp_path, {"exception_info": {"exception_type": "UnknownApiError", "exception_message": "API Error"}}, 0)
    assert diag["code"] == "model_api_error"
    assert diag["verifier_tests"] is None


def test_normalization_and_progress_use_same_diagnosis_without_changing_reward(tmp_path):
    job = tmp_path / "job"
    trial_dir = job / "sglang__abc"
    trial_dir.mkdir(parents=True)
    trial = {"task_name": "sglang", "trial_name": "sglang__abc",
             "finished_at": "2026-10-26T10:19:00", "verifier_result": {"rewards": {"reward": 0}},
             "exception_info": {"exception_type": "UnknownApiError", "exception_message": "API Error"}}
    (trial_dir / "result.json").write_text(json.dumps(trial))
    (job / "result.json").write_text(json.dumps({"n_total_trials": 1, "stats": {"n_errored_trials": 1}}))
    write_agent(trial_dir, failure("Failed to authenticate. API Error: 403 用户额度不足", 403))
    sample = adapter.normalize_trial_result(trial_dir / "result.json")
    progress = adapter.harbor_progress(tmp_path, "job")
    assert sample["metrics"]["diagnosis"] == progress["recent_trials"][0]["diagnosis"]
    assert sample["metrics"]["diagnosis"]["code"] == "model_quota_exhausted"
    assert sample["score"] == 0
    assert sample["status"] == "error"
    assert sample["metrics"]["exception_type"] == "UnknownApiError"
