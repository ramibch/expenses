import json

from django.conf import settings
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

# Nebius Token Factory exposes an OpenAI-compatible API.
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def get_ai_client() -> OpenAI:
    return OpenAI(
        base_url=BASE_URL,
        api_key=settings.NEBIUS_API_KEY,
    )


def get_ai_models() -> list[str]:
    """Return the ids of the models available to the configured API key."""
    client = get_ai_client()
    return [model.id for model in client.models.list()]


def get_response_to_prompt(
    prompt: str,
    model_id: str,
    image_url: str | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    return_json: bool = True,
):
    """Send ``prompt`` to ``model_id`` and return the assistant's reply.

    When ``image_url`` is set the prompt is sent as a multimodal message so
    vision models (e.g. ``Qwen/Qwen2-VL-72B-Instruct``) can see the image;
    otherwise a plain text message is sent.

    With ``return_json=True`` the reply is parsed with :func:`json.loads` and
    returned as a Python object; otherwise the raw string content is returned.
    """
    client = get_ai_client()

    if image_url:
        content: str | list[
            ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam
        ] = [
            ChatCompletionContentPartTextParam(type="text", text=prompt),
            ChatCompletionContentPartImageParam(
                type="image_url", image_url={"url": image_url}
            ),
        ]
    else:
        content = prompt

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    response = client.chat.completions.create(
        model=model_id,
        messages=messages,
    )

    reply = response.choices[0].message.content or ""
    if return_json:
        return json.loads(reply)

    return reply
