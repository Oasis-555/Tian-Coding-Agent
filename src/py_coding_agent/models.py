from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    model: str
    display_name: str
    context_window_tokens: int
    max_output_tokens: int
    reasoning_effort: str | None = None


MODEL_PROFILES: dict[str, ModelProfile] = {
    "kimi-k2.6": ModelProfile(
        model="kimi-k2.6",
        display_name="Kimi K2.6",
        context_window_tokens=262_144,
        max_output_tokens=8_192,
    ),
    "kimi-k2.7-code": ModelProfile(
        model="kimi-k2.7-code",
        display_name="Kimi K2.7 Code",
        context_window_tokens=262_144,
        max_output_tokens=16_384,
    ),
    "kimi-k3": ModelProfile(
        model="kimi-k3",
        display_name="Kimi K3",
        context_window_tokens=1_048_576,
        max_output_tokens=16_384,
        reasoning_effort="high",
    ),
}


def model_profile(model: str) -> ModelProfile | None:
    return MODEL_PROFILES.get(model)


def supported_model_names() -> list[str]:
    return list(MODEL_PROFILES)

