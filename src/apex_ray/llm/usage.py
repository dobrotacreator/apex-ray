from typing import Literal

from apex_ray.llm.prompts import build_review_prompt, build_shallow_review_prompt, build_verifier_batch_prompt
from apex_ray.models import ContextPack, Finding


def review_input_chars(pack: ContextPack, *, review_depth: Literal["deep", "shallow"] = "deep") -> int:
    if review_depth == "shallow":
        return len(build_shallow_review_prompt(pack))
    return len(build_review_prompt(pack))


def estimate_review_input_tokens(
    pack: ContextPack,
    *,
    review_depth: Literal["deep", "shallow"] = "deep",
) -> int:
    return estimate_tokens(review_input_chars(pack, review_depth=review_depth))


def verification_batch_input_chars(findings: list[Finding], pack: ContextPack) -> int:
    return len(build_verifier_batch_prompt(findings, pack)) if findings else 0


def estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0
