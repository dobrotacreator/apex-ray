from typing import Literal

LLMProviderErrorCategory = Literal[
    "auth",
    "malformed",
    "provider",
    "quota",
    "rate_limit",
    "refusal",
    "timeout",
    "truncated",
]


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: LLMProviderErrorCategory | None = None,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


def classify_llm_provider_error(error: Exception | str) -> str:
    if isinstance(error, LLMProviderError) and error.category is not None:
        return {
            "auth": "failed_auth",
            "malformed": "failed_malformed",
            "provider": "failed_provider",
            "quota": "failed_quota",
            "rate_limit": "failed_rate_limit",
            "refusal": "failed_refusal",
            "timeout": "failed_timeout",
            "truncated": "failed_truncated",
        }[error.category]

    text = str(error).lower()
    if "timed out" in text or "timeout" in text:
        return "failed_timeout"
    if "usage limit" in text or "quota" in text or "insufficient_quota" in text:
        return "failed_quota"
    if "rate limit" in text:
        return "failed_rate_limit"
    return "failed_provider"
