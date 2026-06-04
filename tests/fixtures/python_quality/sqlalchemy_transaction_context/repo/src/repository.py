from sqlalchemy.ext.asyncio import AsyncSession

from .models import ResourceRecord


class ResourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_resource(self, account_id: str, payload: dict[str, str]) -> ResourceRecord:
        record = ResourceRecord(
            account_id=account_id,
            resource_id=payload.get("id", "pending"),
            payload=payload,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.commit()
        return record
