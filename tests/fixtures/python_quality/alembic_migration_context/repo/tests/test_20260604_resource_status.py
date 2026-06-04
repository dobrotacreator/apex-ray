from pathlib import Path


def test_resource_status_migration_drops_server_default() -> None:
    migration = Path("migrations/versions/20260604_resource_status.py").read_text()

    assert "server_default=None" in migration
