"""Preflight transport recovery without model or Docker calls."""
import importlib.util
import io
from pathlib import Path
import ssl
import urllib.error

import pytest

spec = importlib.util.spec_from_file_location(
    "tb_preflight", Path(__file__).resolve().parents[1] / "scripts" / "run_tb2.py"
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.mark.parametrize("wrapped", [False, True])
def test_tls_eof_recovers_once_with_remaining_timeout(monkeypatch, capsys, wrapped):
    eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
    error = urllib.error.URLError(eof) if wrapped else eof
    calls = []
    clock = [0.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def open_request(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] += 2
            raise error
        return io.BytesIO(b'{"choices": []}')

    monkeypatch.setattr(adapter.urllib.request, "urlopen", open_request)
    assert adapter.post_json("https://example.invalid", {"authorization": "secret"}, {}, 10) == {"choices": []}
    assert calls == [10, 7]
    log = capsys.readouterr().err
    assert "retrying once" in log
    assert "secret" not in log


@pytest.mark.parametrize("error,timeout,expected_calls", [
    (urllib.error.URLError(ssl.SSLEOFError(8, "EOF")), 10, 2),
    (urllib.error.URLError(ssl.SSLEOFError(8, "EOF")), 0.5, 1),
    (urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate invalid")), 10, 1),
    (urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, None), 10, 1),
    (urllib.error.HTTPError("https://example.invalid", 429, "Rate limited", {}, None), 10, 1),
])
def test_persistent_failure_and_nonretryable_errors(monkeypatch, error, timeout, expected_calls):
    calls = []
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(adapter.urllib.request, "urlopen", fail)
    with pytest.raises(type(error)):
        adapter.post_json("https://example.invalid", {}, {}, timeout)
    assert len(calls) == expected_calls
