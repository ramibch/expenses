import json
import logging
import re

from django.conf import settings
from openai import BadRequestError, OpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from utils.timing import timed

logger = logging.getLogger(__name__)

# Nebius Token Factory exposes an OpenAI-compatible API.
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# Ask the API to constrain the reply to a single JSON object. Models that reject
# this (some vision models do) are retried without it.
JSON_RESPONSE_FORMAT = {"type": "json_object"}

_CODE_FENCE_START = re.compile(r"\A```[a-zA-Z0-9_+-]*\s*")
_CODE_FENCE_END = re.compile(r"```\s*\Z")


def get_ai_client() -> OpenAI:
    return OpenAI(
        base_url=BASE_URL,
        api_key=settings.NEBIUS_API_KEY,
    )


def get_ai_models() -> list[str]:
    """Return the ids of the models available to the configured API key."""
    client = get_ai_client()
    return [model.id for model in client.models.list()]


def extract_json(reply: str):
    """Parse ``reply`` as JSON, tolerating markdown fences and stray prose.

    Models often wrap the object in a ```json fence or add a sentence around it
    even when asked not to. A stray fence used to lose the user's expense, so
    the reply is repaired here instead of failing the request outright.
    """
    text = (reply or "").strip()
    unfenced = _CODE_FENCE_END.sub("", _CODE_FENCE_START.sub("", text)).strip()

    candidates = [text]
    if unfenced != text:
        candidates.append(unfenced)
    start, end = unfenced.find("{"), unfenced.rfind("}")
    if 0 <= start < end:
        candidates.append(unfenced[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("reply is not valid JSON", text, 0)


def _build_message_content(prompt: str, image_url: str | None):
    """Return the user message content, as a multimodal list when an image is set."""
    if not image_url:
        return prompt

    return [
        ChatCompletionContentPartTextParam(type="text", text=prompt),
        ChatCompletionContentPartImageParam(
            type="image_url", image_url={"url": image_url}
        ),
    ]


def _create_completion(
    client: OpenAI,
    model_id: str,
    messages: list[ChatCompletionMessageParam],
    temperature: float,
    json_mode: bool,
):
    """Create a chat completion, retrying without ``response_format`` if rejected.

    Not every model accepts JSON mode; rather than fail the user's request we
    fall back to prompt-only JSON when the API rejects ``response_format``.
    """
    kwargs = {"model": model_id, "messages": messages, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = JSON_RESPONSE_FORMAT

    try:
        return client.chat.completions.create(**kwargs)
    except BadRequestError:
        if not json_mode:
            raise
        logger.info("Model %s rejected response_format; retrying without it", model_id)
        kwargs.pop("response_format")
        return client.chat.completions.create(**kwargs)


def get_response_to_prompt(
    prompt: str,
    model_id: str,
    image_url: str | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    return_json: bool = True,
    fallback_model_id: str | None = None,
    temperature: float = 0,
):
    """Send ``prompt`` to ``model_id`` and return the assistant's reply.

    When ``image_url`` is set the prompt is sent as a multimodal message so
    vision models can see the image; otherwise a plain text message is sent.

    With ``return_json=True`` the reply is parsed with :func:`extract_json` and
    returned as a Python object; if that fails and ``fallback_model_id`` is set,
    the request is retried once with the fallback (typically a stronger model)
    before the error is raised. Otherwise the raw string content is returned.
    """
    client = get_ai_client()

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _build_message_content(prompt, image_url)},
    ]

    model_ids = [model_id]
    if fallback_model_id:
        model_ids.append(fallback_model_id)

    error: json.JSONDecodeError | None = None
    for attempt_model_id in model_ids:
        with timed("ai_call"):
            response = _create_completion(
                client,
                attempt_model_id,
                messages,
                temperature=temperature,
                json_mode=return_json,
            )

        reply = response.choices[0].message.content or ""
        if not return_json:
            return reply

        try:
            return extract_json(reply)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Model %s returned a non-JSON reply: %r",
                attempt_model_id,
                reply[:200],
            )
            error = exc

    raise error
