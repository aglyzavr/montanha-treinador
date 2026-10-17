"""Local model calls via Ollama, plus small output parsers."""
import json
import re

import requests


# Ollama silently drops the START of a prompt that doesn't fit the context
# window -- and the start is the system prompt: the output contract, the
# no-arithmetic rule, the principles. Nothing errors; the model just answers
# without its rules. Ollama's default window is small (a few thousand tokens)
# and this project's prompts are not (principles alone are ~3.5k tokens), so
# the window is set explicitly from config and every run checks it wasn't hit.
CHARS_PER_TOKEN = 3.0          # deliberately pessimistic for mixed EN/PT text
CONTEXT_HEADROOM = 0.75        # the prompt may use at most 75% of the window;
                               # the rest is for thinking + the answer


class ContextOverflow(RuntimeError):
    """The prompt does not fit model.num_ctx. Raised rather than sending advice
    the model produced without having read its own rules."""


def estimate_tokens(*texts: str) -> int:
    return int(sum(len(t or "") for t in texts) / CHARS_PER_TOKEN)


def check_context(num_ctx: int, system: str, user: str):
    """Before the call: refuse a prompt that plainly won't fit."""
    est = estimate_tokens(system, user)
    if num_ctx and est > num_ctx * CONTEXT_HEADROOM:
        raise ContextOverflow(
            f"prompt is ~{est} tokens but model.num_ctx is {num_ctx}; "
            f"raise num_ctx in config.yaml (or shorten context_days)")
    return est


def check_response(num_ctx: int, data: dict):
    """After the call: Ollama reports what it actually read and why it stopped."""
    read = data.get("prompt_eval_count") or 0
    if num_ctx and read >= num_ctx - 16:
        raise ContextOverflow(
            f"model read {read} prompt tokens = the whole {num_ctx}-token window; "
            f"the start of the prompt was cut off. Raise model.num_ctx.")
    if data.get("done_reason") == "length":
        raise ContextOverflow(
            f"answer was cut off at the context limit ({num_ctx}); raise model.num_ctx")
    return read


def chat(cfg: dict, system: str, user: str) -> str:
    m = cfg["model"]
    num_ctx = int(m.get("num_ctx") or 0)
    est = check_context(num_ctx, system, user)
    options = {"temperature": m.get("temperature", 0.4)}
    if num_ctx:
        options["num_ctx"] = num_ctx
    payload = {
        "model": m["name"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": bool(m.get("think", False)),  # thinking/reasoning mode
        "options": options,
    }
    # On the Mac the model could stay resident. On a 32 GB gaming PC it holds
    # ~22 GB of RAM, so by default it is unloaded the moment the answer is in.
    if m.get("keep_alive") is not None:
        payload["keep_alive"] = m["keep_alive"]
    resp = requests.post(
        m["host"].rstrip("/") + "/api/chat",
        json=payload,
        timeout=m.get("timeout_seconds", 900),
    )
    resp.raise_for_status()
    data = resp.json()
    read = check_response(num_ctx, data)
    secs = (data.get("total_duration") or 0) / 1e9
    print(f"Model: read {read} prompt tokens (estimated {est}, window {num_ctx or 'default'}), "
          f"wrote {data.get('eval_count') or 0}, {secs:.0f}s")
    msg = data.get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if not content:
        # Some thinking models put everything under "thinking" if content is empty.
        content = (msg.get("thinking") or "").strip()
    return content


def parse_header(text: str) -> dict:
    """Parse the 'KEY: value' lines before the first '---' separator."""
    head = text.split("---", 1)[0]
    out = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().upper()] = v.strip()
    return out


def human_part(text: str) -> str:
    """The message meant for the athlete: everything after the first '---',
    but before an '---UPDATED_PLAN---' block if present."""
    body = text.split("---", 1)[1] if "---" in text else text
    body = body.split("---UPDATED_PLAN---", 1)[0]
    return body.strip()


def extract_json_block(text: str):
    """Pull the first ```json ...``` fenced block and parse it. Returns dict or None."""
    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except Exception:
        return None
