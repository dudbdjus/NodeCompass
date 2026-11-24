from datetime import UTC, date, datetime

from sqlalchemy import Index, LargeBinary, and_
from sqlalchemy.orm import DeclarativeBase, Mapped, foreign, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class ProxyAnnotation(Base):
    __tablename__ = "proxy_annotations"

    profile_id: Mapped[str] = mapped_column(primary_key=True)
    provider_id: Mapped[str] = mapped_column(primary_key=True)
    region: Mapped[str | None]
    tag: Mapped[str | None]


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (Index("ix_proxies_profile_country", "profile_id", "country"),)

    profile_id: Mapped[str] = mapped_column(primary_key=True)
    provider_id: Mapped[str] = mapped_column(primary_key=True)
    address: Mapped[str]
    port_http: Mapped[int | None]
    port_socks: Mapped[int | None]
    username: Mapped[str | None]
    password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    country: Mapped[str | None]
    status: Mapped[str | None]
    order_id: Mapped[str | None]
    expires_on: Mapped[date | None]

    annotation: Mapped[ProxyAnnotation | None] = relationship(
        primaryjoin=lambda: and_(
            Proxy.profile_id == foreign(ProxyAnnotation.profile_id),
            Proxy.provider_id == foreign(ProxyAnnotation.provider_id),
        ),
        viewonly=True,
        lazy="selectin",
    )

    @property
    def region(self) -> str:
        return (self.annotation.region or "") if self.annotation else ""

    @property
    def tag(self) -> str:
        return (self.annotation.tag or "") if self.annotation else ""


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_profile_started", "profile_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]
    outcome: Mapped[str]
    count: Mapped[int | None]
    reason: Mapped[str | None]


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    happened_at: Mapped[datetime]
    action: Mapped[str]
    outcome: Mapped[str]
