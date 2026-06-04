from .events import ResourceEvent


class ResourceWorker:
    def __init__(self, repository, publisher) -> None:
        self._repository = repository
        self._publisher = publisher

    async def handle(self, event: ResourceEvent) -> None:
        await self._repository.save(event)
        await self._publisher.publish(
            "resource.saved",
            event_id=event.event_id,
            account_id=event.account_id,
        )
