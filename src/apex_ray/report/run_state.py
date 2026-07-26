from collections.abc import Iterable
from dataclasses import dataclass

from apex_ray.models import LLMRun


@dataclass(frozen=True)
class EffectiveLLMPackRunState:
    reviewer_id: str
    context_pack_id: str
    review: LLMRun | None = None
    verify_runs: tuple[LLMRun, ...] = ()


def reduce_llm_pack_run_states(
    runs: Iterable[LLMRun],
) -> dict[tuple[str, str], EffectiveLLMPackRunState]:
    states: dict[tuple[str, str], EffectiveLLMPackRunState] = {}
    for run in runs:
        if run.kind not in {"review", "review_shallow", "verify"}:
            continue
        key = (run.reviewer_id, run.context_pack_id)
        prior = states.get(key)
        if run.kind in {"review", "review_shallow"}:
            states[key] = EffectiveLLMPackRunState(
                reviewer_id=run.reviewer_id,
                context_pack_id=run.context_pack_id,
                review=run,
            )
            continue
        states[key] = EffectiveLLMPackRunState(
            reviewer_id=run.reviewer_id,
            context_pack_id=run.context_pack_id,
            review=prior.review if prior is not None else None,
            verify_runs=(*(prior.verify_runs if prior is not None else ()), run),
        )
    return states
