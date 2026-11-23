import json

from app.models import PingCreate


def parse_ping(value: bytes | None) -> PingCreate:
    if value is None:
        raise ValueError("A Kafka ping must have a message value")
    data = json.loads(value)
    ping = PingCreate.model_validate(data)
    if ping.sequence_number is None:
        raise ValueError("A Kafka ping must include sequence_number")
    return ping
