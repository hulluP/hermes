"""HAI Continuation Proxy

Sits between Hermes and the HAI desktop proxy (port 6655).
Transparently handles the HAI proxy's 4096-token output cap by:
  - Detecting finish_reason='length' on tool calls
  - Sending continuation requests to collect the full tool-call arguments
  - Stitching pieces together and returning a complete response to Hermes

Supports both streaming (SSE) and non-streaming modes.
When stream=True, the final response is converted to SSE format so the
OpenAI Python SDK (used by Hermes) can consume it normally.

Run:
    python3 proxy.py   (or: bash /Users/D048098/.hermes/debug.sh proxy.py)
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hai-proxy")

HAI_UPSTREAM = "http://localhost:6655/litellm/v1"
LISTEN_PORT = 7655
MAX_CONTINUATION_ROUNDS = 8  # at 4096 tokens each → up to 32k tokens total


# ── Upstream communication ─────────────────────────────────────────────────────

def _forward_to_hai(path: str, body: dict, auth_header: str) -> dict:
    """Forward request to HAI, always as non-streaming to get full JSON."""
    forwarded = {**body, "stream": False}
    data = json.dumps(forwarded).encode()
    req = urllib.request.Request(
        HAI_UPSTREAM + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw)


# ── Truncation detection ───────────────────────────────────────────────────────

def _is_tool_call_truncated(response: dict) -> bool:
    choice = response.get("choices", [{}])[0]
    return (
        choice.get("finish_reason") == "length"
        and bool(choice.get("message", {}).get("tool_calls"))
    )


def _is_text_truncated(response: dict) -> bool:
    choice = response.get("choices", [{}])[0]
    return (
        choice.get("finish_reason") == "length"
        and not choice.get("message", {}).get("tool_calls")
    )


def _get_partial_tool_args(response: dict) -> tuple[str, str, str]:
    """Return (tool_call_id, tool_name, partial_args) from a truncated tool response."""
    choice = response.get("choices", [{}])[0]
    tool_calls = choice.get("message", {}).get("tool_calls", [])
    tc = tool_calls[0] if tool_calls else {}
    return (
        tc.get("id", ""),
        tc.get("function", {}).get("name", ""),
        tc.get("function", {}).get("arguments", ""),
    )


# ── SSE conversion ─────────────────────────────────────────────────────────────

def json_to_sse(response: dict) -> bytes:
    """
    Convert a full chat.completion JSON object to SSE (Server-Sent Events) bytes.
    Produces the format the OpenAI Python SDK expects when stream=True.
    """
    resp_id = response.get("id", f"chatcmpl-{int(time.time())}")
    created = response.get("created", int(time.time()))
    model = response.get("model", "unknown")
    system_fp = response.get("system_fingerprint", "")

    choice = response.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    tool_calls = message.get("tool_calls") or []
    content = message.get("content") or ""

    lines: list[str] = []

    def emit(delta: dict, fr: str | None = None) -> None:
        chunk = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
        }
        if system_fp:
            chunk["system_fingerprint"] = system_fp
        lines.append(f"data: {json.dumps(chunk)}\n\n")

    # Role delta
    emit({"role": "assistant", "content": ""})

    if tool_calls:
        # Tool call: emit name in first delta, then arguments in subsequent deltas
        for i, tc in enumerate(tool_calls):
            tc_id = tc.get("id", f"call_{i}")
            fn = tc.get("function", {})
            name = fn.get("name", "")
            arguments = fn.get("arguments", "")
            tc_type = tc.get("type", "function")

            # First chunk: id, type, name, empty arguments
            emit({
                "tool_calls": [{
                    "index": i,
                    "id": tc_id,
                    "type": tc_type,
                    "function": {"name": name, "arguments": ""},
                }]
            })

            # Arguments in chunks (max 512 chars each to avoid large single SSE events)
            chunk_size = 512
            for start in range(0, len(arguments), chunk_size):
                piece = arguments[start:start + chunk_size]
                emit({"tool_calls": [{"index": i, "function": {"arguments": piece}}]})

        # Finish chunk
        emit({}, finish_reason)

    else:
        # Text content: emit in chunks
        chunk_size = 512
        if content:
            for start in range(0, len(content), chunk_size):
                emit({"content": content[start:start + chunk_size]})
        # Finish chunk
        emit({}, finish_reason)

    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


# ── Continuation logic ─────────────────────────────────────────────────────────

def _try_reconstruct_from_partial(partial_args: str, continuation_text: str) -> str | None:
    """
    When partial_args only has 'path' but not 'content' yet, reconstruct
    a valid tool call by pairing the path with the continuation text as content.
    """
    path_match = re.search(r'"path"\s*:\s*"([^"]+)"', partial_args)
    if not path_match:
        return None

    file_path = path_match.group(1)
    content = continuation_text.strip()
    # Strip code fences if model wrapped content in ```
    content = re.sub(r'^```[^\n]*\n', '', content).rstrip('`').strip()
    return json.dumps({"path": file_path, "content": content})


def _continue_tool_call(
    original_body: dict,
    first_response: dict,
    auth_header: str,
) -> dict:
    """
    Continue a truncated tool call. Two strategies based on what was truncated:
    1. No content field yet → ask for plain-text content, reconstruct JSON ourselves.
    2. Partial content in JSON → ask model to continue the JSON fragment.
    """
    tc_id, tool_name, accumulated_args = _get_partial_tool_args(first_response)
    total_completion_tokens = first_response.get("usage", {}).get("completion_tokens", 0)
    has_content_field = '"content"' in accumulated_args

    log.info(
        "Tool call truncated: tool=%s partial_len=%d has_content=%s",
        tool_name, len(accumulated_args), has_content_field,
    )

    base_messages = list(original_body.get("messages", []))
    accumulated_continuation = ""

    for round_num in range(1, MAX_CONTINUATION_ROUNDS + 1):
        if not has_content_field:
            path_match = re.search(r'"path"\s*:\s*"([^"]+)"', accumulated_args)
            file_path = path_match.group(1) if path_match else "unknown"
            prompt = (
                f"[System: Your write_file call for `{file_path}` was truncated before "
                f"any file content was written. Output ONLY the complete file content "
                f"as plain text now — no JSON, no tool calls, no explanation. "
                f"Start the content directly.]"
            )
        else:
            tail = accumulated_args[-300:] if len(accumulated_args) > 300 else accumulated_args
            prompt = (
                f"[System: Your write_file JSON arguments were truncated. "
                f"The fragment ends with: ...{tail}\n"
                f"Output ONLY the remaining characters to complete the JSON. "
                f"No tool calls, no explanation. Raw JSON fragment only.]"
            )

        cont_body = {
            **{k: v for k, v in original_body.items() if k not in ("tools", "tool_choice")},
            "messages": base_messages + [{"role": "user", "content": prompt}],
        }

        try:
            cont_response = _forward_to_hai("/chat/completions", cont_body, auth_header)
        except Exception as e:
            log.warning("Continuation round %d failed: %s", round_num, e)
            break

        cont_choice = cont_response.get("choices", [{}])[0]
        cont_content = cont_choice.get("message", {}).get("content", "") or ""
        cont_finish = cont_choice.get("finish_reason", "stop")
        total_completion_tokens += cont_response.get("usage", {}).get("completion_tokens", 0)

        log.info("Continuation round %d: finish=%s +%d chars", round_num, cont_finish, len(cont_content))
        accumulated_continuation += cont_content

        if not has_content_field:
            reconstructed = _try_reconstruct_from_partial(accumulated_args, accumulated_continuation)
            if reconstructed:
                try:
                    json.loads(reconstructed)
                    log.info("Reconstructed complete tool call JSON (%d chars content)", len(accumulated_continuation))
                    accumulated_args = reconstructed
                    break
                except json.JSONDecodeError:
                    pass
            if cont_finish != "length":
                reconstructed = _try_reconstruct_from_partial(accumulated_args, accumulated_continuation)
                if reconstructed:
                    accumulated_args = reconstructed
                break
        else:
            accumulated_args += cont_content
            try:
                json.loads(accumulated_args)
                log.info("Tool call JSON complete after %d rounds", round_num)
                break
            except json.JSONDecodeError:
                pass
            if cont_finish != "length":
                break

        base_messages = base_messages + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": cont_content},
        ]

    stitched = json.loads(json.dumps(first_response))
    stitched["choices"][0]["finish_reason"] = "tool_calls"
    stitched["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = accumulated_args
    if "usage" in stitched:
        stitched["usage"]["completion_tokens"] = total_completion_tokens

    try:
        json.loads(accumulated_args)
        log.info("Final result: valid JSON, %d chars", len(accumulated_args))
    except json.JSONDecodeError as e:
        log.warning("Final result: invalid JSON (%s) — returning best effort", e)

    return stitched


def _continue_text(
    original_body: dict,
    first_response: dict,
    auth_header: str,
) -> dict:
    """Continue a truncated plain-text response."""
    choice = first_response["choices"][0]
    accumulated = choice.get("message", {}).get("content", "") or ""
    total_tokens = first_response.get("usage", {}).get("completion_tokens", 0)
    messages = list(original_body.get("messages", []))
    # Don't include the truncated message — just add a continuation prompt
    for round_num in range(1, MAX_CONTINUATION_ROUNDS + 1):
        messages_with_cont = messages + [
            {"role": "assistant", "content": accumulated},
            {"role": "user", "content":
                "[System: Your previous response was truncated by the output length limit. "
                "Continue exactly where you left off. Do not restart or repeat prior text.]"},
        ]
        cont_body = {**original_body, "messages": messages_with_cont}
        try:
            cont = _forward_to_hai("/chat/completions", cont_body, auth_header)
        except Exception as e:
            log.warning("Text continuation round %d failed: %s", round_num, e)
            break
        cont_choice = cont["choices"][0]
        cont_text = cont_choice.get("message", {}).get("content", "") or ""
        cont_finish = cont_choice.get("finish_reason", "stop")
        total_tokens += cont.get("usage", {}).get("completion_tokens", 0)
        accumulated += cont_text
        log.info("Text continuation %d: finish=%s +%d chars", round_num, cont_finish, len(cont_text))
        if cont_finish != "length":
            break

    stitched = json.loads(json.dumps(first_response))
    stitched["choices"][0]["finish_reason"] = "stop"
    stitched["choices"][0]["message"]["content"] = accumulated
    if "usage" in stitched:
        stitched["usage"]["completion_tokens"] = total_tokens
    return stitched


# ── Main request handler ───────────────────────────────────────────────────────

def handle_chat_completions(body: dict, auth_header: str) -> dict:
    response = _forward_to_hai("/chat/completions", body, auth_header)
    if _is_tool_call_truncated(response):
        log.warning("Tool call truncated — starting continuation loop")
        response = _continue_tool_call(body, response, auth_header)
    elif _is_text_truncated(response):
        log.info("Text response truncated — continuing")
        response = _continue_text(body, response, auth_header)
    return response


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, sse_bytes: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(sse_bytes)))
        self.end_headers()
        self.wfile.write(sse_bytes)

    def _passthrough_get(self) -> None:
        auth = self.headers.get("Authorization", "")
        path = self.path
        # Strip our prefix to get the upstream path
        for prefix in ("/litellm/v1", "/v1"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        try:
            req = urllib.request.Request(
                HAI_UPSTREAM + path,
                headers={"Authorization": auth},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
                ct = r.headers.get("Content-Type", "application/json")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._send_json(e.code, {"error": str(e)})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def do_GET(self) -> None:
        self._passthrough_get()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        # Route: only intercept chat completions
        path = self.path
        is_chat = "/chat/completions" in path

        if not is_chat:
            # Pass through other POST endpoints unchanged
            try:
                req = urllib.request.Request(
                    HAI_UPSTREAM + path.replace("/litellm/v1", "").replace("/v1", ""),
                    data=raw,
                    headers={"Content-Type": "application/json", "Authorization": auth},
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json(502, {"error": str(e)})
            return

        wants_stream = body.get("stream", False)

        try:
            result = handle_chat_completions(body, auth)
            if wants_stream:
                self._send_sse(json_to_sse(result))
            else:
                self._send_json(200, result)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            log.error("Upstream HTTP %d: %s", e.code, err_body[:400])
            try:
                self._send_json(e.code, json.loads(err_body))
            except Exception:
                self._send_json(e.code, {"error": err_body[:400]})
        except Exception as e:
            log.exception("Proxy error")
            self._send_json(500, {"error": str(e)})


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    log.info("HAI continuation proxy listening on http://127.0.0.1:%d", LISTEN_PORT)
    log.info("Forwarding to: %s", HAI_UPSTREAM)
    log.info("Max continuation rounds: %d (up to %d tokens per response)",
             MAX_CONTINUATION_ROUNDS, MAX_CONTINUATION_ROUNDS * 4096)
    server.serve_forever()
