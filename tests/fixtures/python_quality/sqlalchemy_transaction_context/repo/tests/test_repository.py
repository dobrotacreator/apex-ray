from src.repository import ResourceRepository


async def test_save_resource_uses_pending_default_and_commits(session) -> None:
    repository = ResourceRepository(session)

    record = await repository.save_resource("account-a", {})

    assert record.account_id == "account-a"
    assert record.resource_id == "pending"
    assert session.committed is True
