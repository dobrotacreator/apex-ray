from fastapi import APIRouter, Depends

from .schemas import ResourceCreate, ResourceRead

router = APIRouter()


def require_account() -> str:
    return "account-a"


@router.post("/resources", response_model=ResourceRead)
def create_resource(payload: ResourceCreate, account_id: str = Depends(require_account)) -> ResourceRead:
    return ResourceRead(id=account_id, name=payload.name.strip())
