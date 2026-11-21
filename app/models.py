from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PingCreate(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=100)
    sequence_number: int | None = Field(default=None, ge=1, le=2**63 - 1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=300)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("recorded_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must include a timezone")
        return value


class PingAccepted(BaseModel):
    vehicle_id: str
    sequence_number: int
    recorded_at: datetime
    accepted_at: datetime


class PingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vehicle_id: str
    sequence_number: int | None = None
    latitude: float
    longitude: float
    speed_kmh: float
    recorded_at: datetime
    received_at: datetime


class PingCount(BaseModel):
    count: int
