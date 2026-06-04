from api.routes import create_resource
from api.schemas import ResourceCreate


def test_create_resource_trims_name() -> None:
    result = create_resource(ResourceCreate(name=" primary "))

    assert result.name == "primary"
