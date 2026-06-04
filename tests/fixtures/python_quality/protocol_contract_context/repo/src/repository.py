from typing import Protocol


class ResourceRepository(Protocol):
    def save(self, account_id: str, payload: dict[str, str]) -> str: ...


class InMemoryResourceRepository(ResourceRepository):
    def __init__(self) -> None:
        self.saved: dict[str, dict[str, str]] = {}

    def save(self, account_id: str, payload: dict[str, str]) -> str:
        key = f"{account_id}:{payload['name']}"
        self.saved[key] = payload
        return key
