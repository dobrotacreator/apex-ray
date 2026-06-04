from repository import InMemoryResourceRepository


def test_repository_scopes_saved_resources_by_account() -> None:
    repository = InMemoryResourceRepository()

    assert repository.save("account-a", {"name": "primary"}) == "account-a:primary"
