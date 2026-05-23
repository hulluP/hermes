#!/usr/bin/env python3
"""
Debug script for analyzing Hermes agent truncation issues.
Examines session JSONs to find proxy output token limits.

Usage:
  python3 debug_truncation.py [profile_name]
  python3 debug_truncation.py product_manager
  python3 debug_truncation.py flutter_dev
"""

import json
import sys
import os
import glob

HERMES_DIR = os.path.expanduser("~/.hermes/profiles")


def estimate_tokens(text) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    n = len(text) if isinstance(text, str) else int(text)
    return max(1, n // 4)


def analyze_session(session_path: str):
    with open(session_path) as f:
        data = json.load(f)

    session_id = data.get("session_id", os.path.basename(session_path))
    model = data.get("model", "?")
    messages = data.get("messages", [])

    print(f"\n{'='*70}")
    print(f"Session: {session_id}")
    print(f"Model:   {model}")
    print(f"Messages: {len(messages)}")
    print(f"{'='*70}")

    truncations = []
    tool_truncations = []
    max_safe_assistant_len = 0

    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_input = json.dumps(block.get("input", {}))
                        if len(tool_input) > 3000:
                            print(f"  [tool_use] msg[{i}] tool={block.get('name','?')} input_chars={len(tool_input)}")
            continue

        if not isinstance(content, str):
            continue

        # Detect continuation prompt (Hermes's truncation recovery)
        if "truncated by the output length limit" in content and role == "user":
            prev_idx = i - 1
            if prev_idx >= 0:
                prev = messages[prev_idx]
                prev_content = prev.get("content", "")
                if isinstance(prev_content, str):
                    truncations.append({
                        "truncated_at_msg": prev_idx,
                        "chars": len(prev_content),
                        "est_tokens": estimate_tokens(prev_content),
                    })
            continue

        # Detect Repaired tool call (from tool result messages containing the error string)
        if "Response truncated (finish_reason='length')" in content and role == "tool":
            tool_truncations.append({"msg_idx": i, "chars": len(content)})

        # Track max safe (non-truncated) assistant message
        if role == "assistant" and isinstance(content, str):
            is_followed_by_continue = False
            if i + 1 < len(messages):
                next_content = messages[i + 1].get("content", "")
                if isinstance(next_content, str) and "truncated by the output length limit" in next_content:
                    is_followed_by_continue = True
            if not is_followed_by_continue and len(content) > max_safe_assistant_len:
                max_safe_assistant_len = len(content)

    print(f"\n── Text Response Truncations ({len(truncations)}) ──")
    for t in truncations:
        print(f"  msg[{t['truncated_at_msg']}] chars={t['chars']:6d}  est_tokens={t['est_tokens']:5d}")

    print(f"\n── Tool Call Truncations in Results ({len(tool_truncations)}) ──")
    for t in tool_truncations:
        print(f"  msg[{t['msg_idx']}] chars={t['chars']}")

    print(f"\n── Token Limit Estimate ──")
    if truncations:
        min_truncated = min(t["chars"] for t in truncations)
        max_safe = max_safe_assistant_len
        print(f"  Largest NON-truncated assistant msg: {max_safe} chars (~{estimate_tokens(max_safe)} tokens)")
        print(f"  Smallest truncated response: {min_truncated} chars (~{estimate_tokens(min_truncated)} tokens)")
        print(f"  → Proxy output cap is between ~{estimate_tokens(max_safe)} and ~{estimate_tokens(min_truncated)} tokens")
        print(f"  → Recommended model.max_tokens: {estimate_tokens(max_safe) - 200}")
    else:
        print(f"  No text truncations found.")
        print(f"  Largest assistant msg: {max_safe_assistant_len} chars (~{estimate_tokens(max_safe_assistant_len)} tokens)")

    return truncations, tool_truncations


def main():
    profile = sys.argv[1] if len(sys.argv) > 1 else None

    if profile:
        sessions_dir = os.path.join(HERMES_DIR, profile, "sessions")
        session_files = sorted(glob.glob(os.path.join(sessions_dir, "*.json")))
        print(f"Profile: {profile}  ({len(session_files)} sessions)")
    else:
        session_files = sorted(glob.glob(os.path.join(HERMES_DIR, "*", "sessions", "*.json")))
        print(f"All profiles ({len(session_files)} sessions)")

    if not session_files:
        print("No session files found.")
        return

    all_truncations = []
    for sf in session_files[-5:]:  # Analyze latest 5 sessions
        tc, ttc = analyze_session(sf)
        all_truncations.extend(tc)

    if all_truncations:
        print(f"\n{'='*70}")
        print("OVERALL PROXY LIMIT ESTIMATE")
        chars_at_truncation = [t["chars"] for t in all_truncations]
        print(f"  Truncations detected at: {sorted(chars_at_truncation)}")
        min_t = min(chars_at_truncation)
        max_t = max(chars_at_truncation)
        print(f"  Range: {min_t}–{max_t} chars  (~{estimate_tokens(min_t)}–{estimate_tokens(max_t)} tokens)")
        print(f"\n  Suggested config (conservative):")
        print(f"  model:")
        print(f"    max_tokens: {estimate_tokens(min_t) - 300}")
        print()


if __name__ == "__main__":
    main()
