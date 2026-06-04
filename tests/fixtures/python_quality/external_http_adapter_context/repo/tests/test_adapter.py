from src.adapter import ResourceClient


async def test_send_resource_uses_timeout(fake_http_client) -> None:
    client = ResourceClient(fake_http_client)

    await client.send_resource({"id": "resource-a"})

    assert fake_http_client.calls[0]["timeout"] == 5.0
