"""Web AI interface detection and request normalization.

Normalizes request bodies from web-based AI chat interfaces (claude.ai, chatgpt.com)
into formats the trace viewer can parse -- standard Anthropic/OpenAI message arrays.
"""

from __future__ import annotations

import re

# Hostnames whose traffic we want to trace in browser mode
WEB_AI_HOSTS: frozenset[str] = frozenset(
    {
        "claude.ai",
        "chatgpt.com",
    }
)

# Gemini (gemini.google.com) is intentionally excluded: it uses Batchexecute,
# a complex binary multiplexing format that isn't worth parsing at this stage.


def is_web_ai_host(hostname: str) -> bool:
    """Return True if hostname is a known web AI interface host."""
    return hostname.rstrip(".") in WEB_AI_HOSTS


def is_web_ai_ai_path(hostname: str, path: str) -> bool:
    """Return True if this host+path is a web AI model API call (not a UI asset)."""
    clean = path.split("?", 1)[0]
    if "claude.ai" in hostname:
        return "/chat_conversations/" in clean and clean.endswith("/completion")
    if "chatgpt.com" in hostname:
        return clean.startswith("/backend-api/conversation")
    return False


# ---------------------------------------------------------------------------
# claude.ai request normalization
# ---------------------------------------------------------------------------

_HA_TURN_RE = re.compile(r"\n\n(Human|Assistant):\s?")


def _parse_human_assistant_prompt(prompt: str) -> list[dict]:
    """Parse a legacy claude.ai Human/Assistant prompt string into messages."""
    # re.split with groups includes the captured group in the result
    # e.g. "\n\nHuman: hi\n\nAssistant: hello" ->
    #      ['', 'Human', ' hi', 'Assistant', ' hello']
    parts = _HA_TURN_RE.split(prompt)
    messages: list[dict] = []
    # parts[0] is leading text before first turn marker (typically empty)
    i = 1
    while i + 1 < len(parts):
        role_raw = parts[i].strip()
        content = parts[i + 1].strip()
        if role_raw and content:
            role = "user" if role_raw.lower() == "human" else "assistant"
            messages.append({"role": role, "content": content})
        i += 2
    return messages


def normalize_claude_ai_request(req_body: object) -> object:
    """Normalize a claude.ai request body toward standard Anthropic format.

    Keeps all original keys; only adds/replaces "messages" when absent or empty.
    """
    if not isinstance(req_body, dict):
        return req_body
    # Already has a non-empty messages array -- good as-is
    if isinstance(req_body.get("messages"), list) and req_body["messages"]:
        return req_body
    # Legacy format: prompt is "\n\nHuman: …\n\nAssistant:" string
    prompt = req_body.get("prompt", "")
    if isinstance(prompt, str) and ("\n\nHuman:" in prompt or "\n\nAssistant:" in prompt):
        messages = _parse_human_assistant_prompt(prompt)
        if messages:
            return {**req_body, "messages": messages}
    return req_body


# ---------------------------------------------------------------------------
# chatgpt.com request normalization
# ---------------------------------------------------------------------------


def _normalize_chatgpt_message(msg: object) -> dict | None:
    """Return a standard {role, content} dict from a chatgpt.com message object.

    chatgpt.com differs from the OpenAI API in two ways:
    - role lives under "author": {"role": "..."} instead of top-level "role"
    - content is {"content_type": "text", "parts": ["..."]} instead of a plain string
    """
    if not isinstance(msg, dict):
        return None
    author = msg.get("author")
    role: str | None = msg.get("role") or (author.get("role") if isinstance(author, dict) else None)
    if not isinstance(role, str) or not role:
        return None

    content_obj = msg.get("content", "")
    if isinstance(content_obj, dict):
        ct = content_obj.get("content_type", "")
        parts = content_obj.get("parts") or []
        if ct == "text":
            content: object = "\n".join(str(p) for p in parts if p)
        elif ct in ("multimodal_text", "real_time_user_audio_video_asset_pointer"):
            # Keep only plain-text parts; mark non-text ones
            texts = [str(p) for p in parts if isinstance(p, str) and p]
            content = "\n".join(texts) if texts else "[non-text content]"
        else:
            content = str(content_obj)
    else:
        content = content_obj

    return {"role": role, "content": content}


def normalize_chatgpt_request(req_body: object) -> object:
    """Normalize a chatgpt.com request body to standard OpenAI format.

    Returns a shallow copy with "messages" replaced by normalized dicts.
    All other keys (model, conversation_id, action, …) are preserved.
    """
    if not isinstance(req_body, dict):
        return req_body
    messages = req_body.get("messages")
    if not isinstance(messages, list):
        return req_body
    normalized: list[dict] = []
    for msg in messages:
        result = _normalize_chatgpt_message(msg)
        if result:
            normalized.append(result)
    if not normalized:
        return req_body
    return {**req_body, "messages": normalized}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def normalize_web_ai_request(hostname: str, path: str, req_body: object) -> object:
    """Normalize a web AI interface request body for trace storage.

    Returns a modified copy with messages normalized to the standard format
    understood by the trace viewer.  Original keys not related to message
    content are preserved.
    """
    if "claude.ai" in hostname:
        return normalize_claude_ai_request(req_body)
    if "chatgpt.com" in hostname:
        return normalize_chatgpt_request(req_body)
    return req_body
