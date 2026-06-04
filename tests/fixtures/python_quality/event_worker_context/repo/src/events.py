from dataclasses import dataclass


@dataclass(slots=True)
class ResourceEvent:
    event_id: str
    account_id: str
    payload: dict[str, str]
