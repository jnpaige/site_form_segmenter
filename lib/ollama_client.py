"""Ollama REST client with text and vision support, thread-safe token stats."""
import ipaddress
import json
import re
import threading
import time
from urllib.parse import urlparse

import httpx

_lock = threading.Lock()
_stats: dict = {"prompt_tokens": 0, "completion_tokens": 0, "elapsed_s": 0.0}


def assert_local_inference(base_url: str) -> None:
    """Raise unless base_url points at an inference host on this machine.

    To help meet broad data security requirements across domains, the
    ability to pass content to an outside model, or to send it to an outside
    server, is walled off here. base_url is the single config value that
    decides where document content goes — text in text mode, rendered page
    images in vision mode — so it is checked on every request rather than
    trusted.

    Raises rather than warns, and has no override flag: a warning in a long
    batch run scrolls past unread, and an override that exists gets used. If
    a non-local inference host is ever approved, add the flag in that
    commit, so the decision shows up in a diff rather than sitting here
    pre-granted.

    Called from _post rather than at config load, so every entry point that
    reaches Ollama through this module is covered — segmenter.py,
    segment_reports_pass0.py, segment_glo_township_range_llm.py — without
    each one having to remember. Kept in sync with site_coder's copy; the
    two clients are intentionally parallel.
    """
    host = urlparse(base_url).hostname
    if host is None:
        raise ValueError(f"base_url is not a parseable URL: {base_url!r}")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass  # a hostname, not an IP literal — cannot be confirmed local here
    raise ValueError(
        f"Refusing to send content to a non-local inference host: {base_url!r}\n"
        "Outbound model calls are walled off by design. Point base_url at an "
        "Ollama instance running on this machine (http://localhost:11434)."
    )


def call_ollama(
    system_prompt: str,
    user_content: str,
    model: str,
    base_url: str,
    temperature: float = 0.05,
    timeout: float = 1800,
    label: str = "",
    num_ctx: int | None = None,
) -> str | None:
    """Call Ollama chat endpoint; return raw text response or None on error."""
    options = {"temperature": temperature}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "stream": False,
        "options": options,
    }
    return _post(payload, base_url, timeout, label)


def call_ollama_vision(
    system_prompt: str,
    user_content: str,
    images: list[str],
    model: str,
    base_url: str,
    temperature: float = 0.05,
    timeout: float = 1800,
    label: str = "",
    num_ctx: int | None = None,
) -> str | None:
    """Call Ollama chat endpoint with embedded images; return raw text or None.

    images: list of base64-encoded strings (no data-URI prefix).
    Images are attached to the user message; the model sees them in order.
    """
    options = {"temperature": temperature}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content, "images": images},
        ],
        "stream": False,
        "options": options,
    }
    return _post(payload, base_url, timeout, label)


def _post(payload: dict, base_url: str, timeout: float, label: str) -> str | None:
    assert_local_inference(base_url)
    t0 = time.monotonic()
    try:
        resp = httpx.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.TimeoutException:
        print(f"  [ERROR] {label}: timed out")
        return None
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        print(f"  [ERROR] {label}: {e}" + (f"\n          {body}" if body else ""))
        return None

    elapsed = time.monotonic() - t0
    raw     = resp.content
    lines   = [l for l in raw.splitlines() if l.strip()]

    text: str = ""
    prompt_tokens = eval_tokens = eval_dur = 0
    n_chunks = 0
    for line in lines:
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        text += chunk.get("message", {}).get("content", "")
        n_chunks += 1
        if chunk.get("done"):
            prompt_tokens = chunk.get("prompt_eval_count", 0)
            eval_tokens   = chunk.get("eval_count", 0)
            eval_dur      = chunk.get("eval_duration", 0)
            done_reason   = chunk.get("done_reason", "unknown")
            if label:
                print(f"\n    [debug] {label}: chunks={n_chunks}  prompt_tok={prompt_tokens}  gen_tok={eval_tokens}  done_reason={done_reason}  text_len={len(text)}", flush=True)
            break

    with _lock:
        _stats["prompt_tokens"]     += prompt_tokens
        _stats["completion_tokens"] += eval_tokens
        _stats["elapsed_s"]         += elapsed

    return text


def extract_json(
    system_prompt: str,
    user_content: str,
    model: str,
    base_url: str,
    temperature: float = 0.05,
    timeout: float = 1800,
    label: str = "",
    num_ctx: int | None = None,
) -> dict | list | None:
    text = call_ollama(system_prompt, user_content, model, base_url,
                       temperature, timeout, label, num_ctx)
    return _parse_json(text, label) if text is not None else None


def extract_json_vision(
    system_prompt: str,
    user_content: str,
    images: list[str],
    model: str,
    base_url: str,
    temperature: float = 0.05,
    timeout: float = 1800,
    label: str = "",
    num_ctx: int | None = None,
) -> dict | list | None:
    text = call_ollama_vision(system_prompt, user_content, images, model,
                              base_url, temperature, timeout, label, num_ctx)
    return _parse_json(text, label) if text is not None else None


def _parse_json(text: str, label: str = "") -> dict | list | None:
    text = text.strip()
    text = re.sub(r'^```[a-z]*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$',    '', text, flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'[\[{].*[\]}]', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    preview = text[:600].replace("\n", " ") if text else "<empty>"
    print(f"  [warn] JSON parse failed for {label}  |  response preview: {preview!r}")
    return None


def get_stats() -> dict:
    with _lock:
        return dict(_stats)


def reset_stats() -> None:
    with _lock:
        _stats["prompt_tokens"]     = 0
        _stats["completion_tokens"] = 0
        _stats["elapsed_s"]         = 0.0
