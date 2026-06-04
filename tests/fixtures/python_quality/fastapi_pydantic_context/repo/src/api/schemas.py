from pydantic import BaseModel


class ResourceCreate(BaseModel):
    name: str
    enabled: bool = True


class ResourceRead(BaseModel):
    id: str
    name: str
