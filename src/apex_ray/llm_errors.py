class LLMProviderError(RuntimeError):
    pass


def classify_llm_provider_error(error: Exception | str) -> str:
    text = str(error).lower()
    if "timed out" in text or "timeout" in text:
        return "failed_timeout"
    if "usage limit" in text or "rate limit" in text or "quota" in text or "insufficient_quota" in text:
        return "failed_quota"
    return "failed_provider"
