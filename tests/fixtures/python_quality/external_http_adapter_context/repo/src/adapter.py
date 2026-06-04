from typing import Any

import httpx


class ResourceClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/resources", json=payload, timeout=5.0)
        response.raise_for_status()
        return _decode_response(response)


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("resource response must be an object")
    return data
