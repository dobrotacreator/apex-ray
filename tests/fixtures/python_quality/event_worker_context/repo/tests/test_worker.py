from src.events import ResourceEvent
from src.worker import ResourceWorker


async def test_worker_publishes_resource_saved_event(repository, publisher) -> None:
    worker = ResourceWorker(repository, publisher)
    event = ResourceEvent(event_id="event-a", account_id="account-a", payload={})

    await worker.handle(event)

    assert publisher.events[0]["event_id"] == "event-a"
    assert publisher.events[0]["account_id"] == "account-a"
