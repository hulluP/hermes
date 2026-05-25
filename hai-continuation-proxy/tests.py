"""
Test suite for hai-continuation-proxy.

Run with:
    bash /Users/D048098/.hermes/debug.sh tests.py

All tests hit the live proxy on port 7655 and require:
  - HAI proxy running on port 6655 (Hyperspace AI app)
  - Continuation proxy running on port 7655 (proxy.py)
"""

import json
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, "/Users/D048098/SAPDevelop/2026LangChainHome/hai-continuation-proxy")

TOKEN = "e9840074-f6df-4f07-a2c7-93ee5c515a05"
PROXY_URL = "http://localhost:7655/litellm/v1"
HAI_URL = "http://localhost:6655/litellm/v1"
MODEL = "anthropic--claude-4.7-opus"

WRITE_FILE_TOOL = [{
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}]


# ── Helpers ────────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0

def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓  {name}")

def fail(name, reason):
    global FAIL
    FAIL += 1
    print(f"  ✗  {name}: {reason}")

def post(url, body, timeout=300):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url + "/chat/completions", data=data, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def parse_sse(raw: bytes) -> list[dict]:
    """Parse SSE stream bytes into a list of data objects."""
    chunks = []
    for line in raw.decode().splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunks.append(json.loads(payload))
            except Exception:
                pass
    return chunks

def get_sse(url, body, timeout=300):
    """POST and return (raw_bytes, parsed_chunks)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url + "/chat/completions", data=data, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        content_type = r.headers.get("Content-Type", "")
        raw = r.read()
    return content_type, raw, parse_sse(raw)


# ── Unit tests (no network) ────────────────────────────────────────────────────

def test_unit_truncation_detection():
    from proxy import _is_tool_call_truncated, _is_text_truncated

    truncated_tool = {"choices": [{"finish_reason": "length", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path":"/t'}}]
    }}], "usage": {"completion_tokens": 4096}}

    normal_tool = {"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"id": "c1", "function": {"name": "write_file", "arguments": '{}'}}]
    }}]}

    truncated_text = {"choices": [{"finish_reason": "length",
                                    "message": {"role": "assistant", "content": "partial"}}], "usage": {}}

    assert _is_tool_call_truncated(truncated_tool), "should detect truncated tool call"
    assert not _is_tool_call_truncated(normal_tool), "should NOT flag normal tool call"
    assert not _is_tool_call_truncated(truncated_text), "should NOT flag text truncation"
    assert _is_text_truncated(truncated_text), "should detect truncated text"
    assert not _is_text_truncated(truncated_tool), "should NOT flag tool truncation as text"
    ok("unit: truncation detection")

def test_unit_json_reconstruction():
    from proxy import _try_reconstruct_from_partial

    partial = '{"path": "/tmp/report.md"'
    content = "# Report\n\nThis is the content."
    result = _try_reconstruct_from_partial(partial, content)
    assert result is not None, "reconstruction should succeed"
    parsed = json.loads(result)
    assert parsed["path"] == "/tmp/report.md"
    assert "Report" in parsed["content"]
    ok("unit: JSON reconstruction from partial path-only")

def test_unit_sse_conversion():
    """Test that json_to_sse produces valid SSE with correct chunks."""
    from proxy import json_to_sse

    response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello world"},
            "finish_reason": "stop",
        }],
        "usage": {"completion_tokens": 2, "prompt_tokens": 10, "total_tokens": 12},
    }

    raw = json_to_sse(response)
    chunks = parse_sse(raw)
    assert len(chunks) >= 2, f"expected at least 2 chunks, got {len(chunks)}"

    # Find the finish chunk
    finish_reasons = [
        c["choices"][0].get("finish_reason")
        for c in chunks
        if c.get("choices")
    ]
    assert "stop" in finish_reasons, f"no stop finish_reason in chunks: {finish_reasons}"

    # Reconstruct content
    content = ""
    for c in chunks:
        delta = c.get("choices", [{}])[0].get("delta", {})
        content += delta.get("content", "") or ""
    assert content == "Hello world", f"content mismatch: {content!r}"
    ok("unit: SSE conversion (text response)")

def test_unit_sse_tool_call():
    """Test that json_to_sse handles tool call responses."""
    from proxy import json_to_sse

    response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": MODEL,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path":"/tmp/x","content":"hello"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"completion_tokens": 10},
    }

    raw = json_to_sse(response)
    chunks = parse_sse(raw)

    # Reconstruct arguments from delta chunks
    args = ""
    for c in chunks:
        tc_list = c.get("choices", [{}])[0].get("delta", {}).get("tool_calls", [])
        for tc in tc_list:
            args += tc.get("function", {}).get("arguments", "") or ""

    assert args == '{"path":"/tmp/x","content":"hello"}', f"args mismatch: {args!r}"
    ok("unit: SSE conversion (tool call response)")


# ── Integration tests (require live proxies) ──────────────────────────────────

def check_proxies():
    for label, url in [("HAI :6655", HAI_URL), ("Proxy :7655", PROXY_URL)]:
        try:
            req = urllib.request.Request(
                url.replace("/litellm/v1", "") + "/",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            try:
                req2 = urllib.request.Request(
                    url + "/models",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                urllib.request.urlopen(req2, timeout=5)
            except Exception as e:
                print(f"\n  ⚠  {label} not reachable: {e}")
                print("     Start it first, then re-run tests.\n")
                return False
    return True


def test_integration_non_streaming():
    """stream=False: proxy returns plain JSON."""
    resp = post(PROXY_URL, {
        "model": MODEL, "max_tokens": 512, "stream": False,
        "messages": [{"role": "user", "content": "Say exactly: PROXY_OK"}],
    })
    content = resp["choices"][0]["message"].get("content", "")
    assert "PROXY_OK" in content, f"unexpected content: {content!r}"
    ok("integration: non-streaming passthrough")


def test_integration_streaming_simple():
    """stream=True: proxy converts JSON to SSE, Hermes-style parsing works."""
    content_type, raw, chunks = get_sse(PROXY_URL, {
        "model": MODEL, "max_tokens": 512, "stream": True,
        "messages": [{"role": "user", "content": "Say exactly: STREAM_OK"}],
    })
    assert "event-stream" in content_type or len(chunks) > 0, \
        f"expected SSE response, got content_type={content_type!r}"
    content = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in chunks if c.get("choices")
    )
    finish = next(
        (c["choices"][0].get("finish_reason") for c in chunks
         if c.get("choices") and c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "stop", f"expected finish_reason=stop, got {finish!r}"
    assert "STREAM_OK" in content, f"unexpected content: {content!r}"
    ok("integration: streaming simple response")


def test_integration_streaming_small_tool():
    """stream=True + small tool call (fits in 4096 tokens): clean tool_calls finish."""
    content_type, raw, chunks = get_sse(PROXY_URL, {
        "model": MODEL, "max_tokens": 8192, "stream": True,
        "tools": WRITE_FILE_TOOL,
        "messages": [{"role": "user", "content":
            "Use write_file to save the text 'hello world' to /tmp/small_test.md"}],
    })
    args = ""
    tool_name = ""
    for c in chunks:
        tc_list = c.get("choices", [{}])[0].get("delta", {}).get("tool_calls", [])
        for tc in tc_list:
            tool_name = tool_name or tc.get("function", {}).get("name", "")
            args += tc.get("function", {}).get("arguments", "") or ""

    finish = next(
        (c["choices"][0].get("finish_reason") for c in chunks
         if c.get("choices") and c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "tool_calls", f"expected finish_reason=tool_calls, got {finish!r}"
    parsed = json.loads(args)
    assert "hello world" in parsed.get("content", "").lower() or \
           "hello" in parsed.get("content", "").lower(), \
        f"unexpected content: {parsed.get('content','')!r}"
    ok("integration: streaming small tool call")


def test_integration_streaming_large_tool():
    """
    stream=True + large write_file (triggers HAI 4096-token cap + continuation).
    Validates the core fix: continuation proxy must return valid JSON args > 5000 chars.
    """
    content_type, raw, chunks = get_sse(PROXY_URL, {
        "model": MODEL, "max_tokens": 8192, "stream": True,
        "tools": WRITE_FILE_TOOL,
        "messages": [{"role": "user", "content":
            "Write a detailed 2000-word product requirements document covering: "
            "executive summary, 5 user personas, 10 features with acceptance criteria, "
            "and success metrics. Use write_file to save it to /tmp/prd_test.md."}],
    }, timeout=300)

    args = ""
    for c in chunks:
        tc_list = c.get("choices", [{}])[0].get("delta", {}).get("tool_calls", [])
        for tc in tc_list:
            args += tc.get("function", {}).get("arguments", "") or ""

    finish = next(
        (c["choices"][0].get("finish_reason") for c in chunks
         if c.get("choices") and c["choices"][0].get("finish_reason")),
        None,
    )

    assert finish == "tool_calls", f"expected finish_reason=tool_calls, got {finish!r}"
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as e:
        fail("integration: streaming large tool call (continuation)", f"invalid JSON: {e}\ntail: {args[-200:]!r}")
        return
    content_len = len(parsed.get("content", ""))
    assert content_len > 5000, f"expected >5000 chars content, got {content_len}"
    ok(f"integration: streaming large tool call (continuation) [{content_len} chars]")


def test_integration_openai_sdk():
    """Use the actual openai SDK in streaming mode — exactly how Hermes calls it."""
    try:
        from openai import OpenAI
    except ImportError:
        fail("integration: openai SDK streaming", "openai package not installed in venv")
        return

    client = OpenAI(base_url=PROXY_URL, api_key=TOKEN)
    stream = client.chat.completions.create(
        model=MODEL,
        max_tokens=8192,
        stream=True,
        tools=WRITE_FILE_TOOL,
        messages=[{"role": "user", "content":
            "Write a 1500-word technical spec document. "
            "Use write_file to save it to /tmp/spec_sdk_test.md."}],
    )

    tool_name = ""
    args = ""
    finish_reason = None
    for chunk in stream:
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = choice.delta
        if delta.tool_calls:
            for tc in delta.tool_calls:
                if tc.function.name:
                    tool_name = tc.function.name
                if tc.function.arguments:
                    args += tc.function.arguments

    assert finish_reason == "tool_calls", f"expected tool_calls, got {finish_reason!r}"
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as e:
        fail("integration: openai SDK streaming", f"invalid JSON: {e}")
        return
    content_len = len(parsed.get("content", ""))
    assert content_len > 3000, f"content too short: {content_len}"
    ok(f"integration: openai SDK streaming (tool call, {content_len} chars)")


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_all():
    print("\n" + "═" * 60)
    print(" HAI Continuation Proxy — Test Suite")
    print("═" * 60)

    print("\n── Unit tests (no network) ──")
    test_unit_truncation_detection()
    test_unit_json_reconstruction()
    test_unit_sse_conversion()
    test_unit_sse_tool_call()

    print("\n── Integration tests (live proxies) ──")
    if not check_proxies():
        print("  Skipping integration tests — proxies not running")
    else:
        test_integration_non_streaming()
        test_integration_streaming_simple()
        test_integration_streaming_small_tool()
        test_integration_streaming_large_tool()
        test_integration_openai_sdk()

    print("\n" + "─" * 60)
    total = PASS + FAIL
    print(f" Results: {PASS}/{total} passed", "✓" if FAIL == 0 else f"  ({FAIL} FAILED)")
    print("─" * 60 + "\n")
    return FAIL == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
