"""Build provider-neutral description requests.

This module owns the "what do we send to the model" part of description
generation. Providers should only translate the request into their wire format.
"""

from __future__ import annotations

from dataclasses import dataclass

from ResourceProcessor.description.description_generator import DescriptionInput
from ResourceProcessor.description.prompt_config import (
    get_description_user_prompt,
    get_user_prompt,
)


@dataclass(frozen=True)
class DescriptionRequest:
    context: str
    user_prompt: str
    llm_input_type: str
    llm_input_paths: list[str]


def build_description_request(
    input_data: DescriptionInput,
    *,
    include_classification: bool = False,
) -> DescriptionRequest:
    """Return a normalized request body for current single calls or batch jobs."""
    context = input_data.to_prompt_context()
    prompt_kwargs = {"description_prompt_env": input_data.description_prompt_env}
    user_prompt = (
        get_user_prompt(context, **prompt_kwargs)
        if include_classification
        else get_description_user_prompt(context, **prompt_kwargs)
    )
    return DescriptionRequest(
        context=context,
        user_prompt=user_prompt,
        llm_input_type=input_data.resolved_llm_input_type,
        llm_input_paths=(
            input_data.resolved_llm_input_paths
            if input_data.attach_llm_media
            else []
        ),
    )
