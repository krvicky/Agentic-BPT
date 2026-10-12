"""Environment-only provider configuration; no SDK client or API calls."""
from dataclasses import dataclass, field
import os
from typing import Literal, Mapping

LLMTask = Literal["extraction", "classification", "evidence_audit", "complex_reasoning"]
_TASK_ENV: dict[str, str] = {
    "extraction": "OPENROUTER_EXTRACTION_MODEL",
    "classification": "OPENROUTER_CLASSIFICATION_MODEL",
    "evidence_audit": "OPENROUTER_EVIDENCE_AUDIT_MODEL",
    "complex_reasoning": "OPENROUTER_COMPLEX_REASONING_MODEL",
}

class ConfigurationError(ValueError):
    """Configuration error messages contain variable names, never credentials."""

@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(repr=False)
    flash_model: str
    pro_model: str
    base_url: str = "https://openrouter.ai/api/v1"
    extraction_model: str | None = None
    classification_model: str | None = None
    evidence_audit_model: str | None = None
    complex_reasoning_model: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConfig":
        values = os.environ if env is None else env
        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"Missing required environment variable: {name}")
            return value
        overrides = {task + "_model": values.get(name, "").strip() or None
                     for task, name in _TASK_ENV.items()}
        return cls(api_key=required("OPENROUTER_API_KEY"),
                   flash_model=required("OPENROUTER_FLASH_MODEL"),
                   pro_model=required("OPENROUTER_PRO_MODEL"),
                   base_url=values.get("OPENROUTER_BASE_URL", "").strip() or "https://openrouter.ai/api/v1",
                   **overrides)

    def model_for(self, task: LLMTask, *, override: str | None = None) -> str:
        if task not in _TASK_ENV:
            raise ConfigurationError("Unknown LLM task")
        if override is not None:
            if not isinstance(override, str) or not override.strip():
                raise ConfigurationError("Task model override must be a nonempty model ID")
            return override.strip()
        configured = getattr(self, task + "_model")
        if configured:
            return configured
        return self.flash_model if task in ("extraction", "classification") else self.pro_model
