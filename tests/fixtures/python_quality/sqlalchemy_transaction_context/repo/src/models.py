from dataclasses import dataclass


@dataclass(slots=True)
class ResourceRecord:
    account_id: str
    resource_id: str
    payload: dict[str, str]
