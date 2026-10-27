"""Evidence-based presentation of trial outcomes; never changes official rewards.

Only API error events/final results are considered, not arbitrary model text or
tool output. Verifier counts come from official verifier files, not agent tests.
"""
import json
import re
from functools import lru_cache
from pathlib import Path


def tail_text(path: Path, limit=512_000):
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            if size > limit:
                f.readline()  # Discard partial JSON line.
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def safe_evidence(value):
    text = str(value)
    text = re.sub(r"\b(?:sk|key)-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|auth[_-]?token|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[REDACTED]", text)
    return text[:700]


def api_failure(trial_dir):
    """Prefer the last final result, so recovered transient errors don't win."""
    last_result = None
    last_api = None
    for line in tail_text(trial_dir / "agent/claude-code.txt").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            last_result = event
        elif event.get("is_api_error_message") is True:
            message = event.get("message") or {}
            if not isinstance(message, dict):
                continue
            blocks = message.get("content") or []
            text = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text") if isinstance(blocks, list) else ""
            last_api = (text, event.get("api_error_status"))
    if last_result is not None:
        if not last_result.get("is_error") and last_result.get("terminal_reason") != "api_error":
            return None
        text = str(last_result.get("result") or "")
        status = last_result.get("api_error_status")
        if status or last_result.get("terminal_reason") == "api_error" or "API Error" in text:
            return text or (last_api or ("", None))[0], status
        return None
    return last_api


def classify_api(text, status=None):
    """Explicit billing evidence beats a wrapper's 403/authentication label."""
    if status is None:
        match = re.search(r"(?i)(?:API Error|HTTP(?: status)?|status(?:_code)?)\s*[:=]?\s*([45]\d\d)\b", text)
        status = int(match.group(1)) if match else None
    try:
        status = int(status) if status else None
    except (TypeError, ValueError):
        status = None
    billing = re.search(
        r"(?i)insufficient[_\s-]*(?:credits?|balance|quota|funds)|"
        r"credit balance (?:is )?too low|(?:credits?|balance|quota) (?:has been |is )?(?:exhausted|depleted)|"
        r"billing[_\s-]hard[_\s-]limit[_\s-]reached|exceeded your current quota|"
        r"requires more credits|(?:余额|额度|配额|积分|点数).{0,10}(?:不足|耗尽|用完)|欠费", text)
    if billing or status == 402:
        return "model_quota_exhausted", "模型余额／额度不足", status
    if status == 429 or re.search(r"(?i)rate[_\s-]?limit|too many requests|请求过于频繁|限流", text):
        return "model_rate_limited", "模型接口限流", status
    if status in (401, 403) or re.search(r"(?i)authentication_failed|invalid[_\s-]api[_\s-]key|unauthorized", text):
        return "model_auth_error", "模型认证／访问权限异常", status
    return "model_api_error", "模型 API 调用异常（原因未识别）", status


def verifier_counts(trial_dir):
    # CTRF summary is authoritative when available and well formed.
    try:
        document = json.loads(tail_text(trial_dir / "verifier/ctrf.json", 2_000_000))
        summary = document["results"]["summary"]
        if not isinstance(summary, dict):
            raise ValueError("Invalid CTRF summary")
        keys = ("tests", "passed", "failed", "skipped", "pending", "other")
        if all(isinstance(summary.get(k, 0), int) and not isinstance(summary.get(k, 0), bool) and summary.get(k, 0) >= 0 for k in keys):
            counts = {k: summary.get(k, 0) for k in keys}
            if counts["tests"] > 0 and sum(counts[k] for k in keys[1:]) == counts["tests"]:
                counts["source"] = "verifier/ctrf.json"
            else:
                counts = None
        else:
            counts = None
    except (ValueError, TypeError, KeyError):
        counts = None
    # Pytest errors/collection failures may not be represented in CTRF counts.
    text = tail_text(trial_dir / "verifier/test-stdout.txt", 64_000)
    summaries = re.findall(r"(?m)^=+\s+(.+?)\s+=+\s*$", text)
    last = next((line for line in reversed(summaries) if re.search(r"\d+ (?:passed|failed|errors?|skipped)", line)), "")
    numbers = {k.rstrip("s") if k in ("error", "errors") else k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", last)}
    if counts is not None:
        counts["errors"] = numbers.get("error", 0)
        return counts
    if numbers:
        return {"passed": numbers.get("passed", 0), "failed": numbers.get("failed", 0),
                "errors": numbers.get("error", 0), "skipped": numbers.get("skipped", 0),
                "tests": sum(v for k, v in numbers.items() if k != "deselected"),
                "source": "verifier/test-stdout.txt"}
    return None


def diagnose_trial(trial_dir, trial, reward=None):
    # Heartbeats revisit completed trials. Invalidate when any evidence changes.
    stamps = []
    for relative in ("agent/claude-code.txt", "verifier/ctrf.json", "verifier/test-stdout.txt"):
        try:
            stat = (trial_dir / relative).stat()
            stamps.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append(None)
    return _diagnose_cached(str(trial_dir), json.dumps(trial.get("exception_info") or {}, sort_keys=True),
                            reward if isinstance(reward, (int, float)) else None, tuple(stamps))


@lru_cache(maxsize=512)
def _diagnose_cached(directory, exception_json, reward, stamps):
    trial_dir = Path(directory)
    exception = json.loads(exception_json)
    exception_type = str(exception.get("exception_type") or "")
    message = str(exception.get("exception_message") or "")
    counts = verifier_counts(trial_dir)
    diagnostic = {"version": 1, "original_exception_type": exception_type or None,
                  "verifier_tests": counts, "http_status": None, "execution_error": False}
    failure = api_failure(trial_dir)
    api_source = "agent/claude-code.txt" if failure is not None else "result.json"
    # Other agents may provide the API error only through Harbor's exception.
    if failure is None and ("api" in exception_type.lower() or "authentication" in exception_type.lower()):
        failure = (message, None)
    if exception_type == "ResourcePreflightError":
        code, label = "resource_unavailable", "资源不满足，已跳过"
        source, evidence, execution_error = "result.json", message, True
    elif failure is not None:
        text, status = failure
        code, label, diagnostic["http_status"] = classify_api(text, status)
        source = api_source
        evidence, execution_error = text, True
    elif exception:
        code, label = "execution_error", "执行异常"
        source, evidence, execution_error = "result.json", message, True
    elif counts and counts.get("errors", 0):
        code, label = "verifier_error", "判分测试执行异常"
        source, evidence, execution_error = counts["source"], f"判分测试报告 {counts['errors']} 项错误；需查看判分日志定位原因。", True
    elif counts and counts["failed"] > 0:
        code = "verifier_tests_failed"
        label = "判分测试部分未通过" if counts["passed"] else "判分测试未通过"
        source, evidence, execution_error = counts["source"], f"通过 {counts['passed']}，失败 {counts['failed']}，跳过 {counts['skipped']}。具体原因见官方判分日志。", False
    elif isinstance(reward, (int, float)) and reward <= 0:
        code, label = "verifier_not_passed", "官方判分未通过"
        source, evidence, execution_error = "result.json", "官方 reward 为 0；未获取到可识别的测试统计，不能据此推断具体原因。", False
    elif isinstance(reward, (int, float)) and reward > 0:
        code, label = "reward_positive", "官方判分已得分"
        source, evidence, execution_error = "result.json", f"官方 reward：{reward}", False
    else:
        code, label = "unknown", "结果待确认"
        source, evidence, execution_error = "result.json", "缺少可识别的评分或异常信息。", False
    diagnostic.update(code=code, label=label, source=source, evidence=safe_evidence(evidence), execution_error=execution_error)
    return diagnostic
