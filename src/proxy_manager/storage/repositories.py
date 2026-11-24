from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from proxy_manager.storage.database import Database
from proxy_manager.storage.models import Operation, Proxy, ProxyAnnotation, SyncRun, utc_now


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def get_proxy(self, profile_id: str, provider_id: str) -> Proxy | None:
        with self.database.sessions() as session:
            return session.get(Proxy, (profile_id, provider_id))

    def list_proxies(self, profile_id: str) -> list[Proxy]:
        statement = (
            select(Proxy)
            .where(Proxy.profile_id == profile_id)
            .order_by(Proxy.address, Proxy.provider_id)
        )
        with self.database.sessions() as session:
            return list(session.scalars(statement))

    def save_annotation(self, row: Proxy, field: str, value: str) -> ProxyAnnotation:
        if field not in {"region", "tag"}:
            raise ValueError("unknown_annotation_field")
        update = {field: value or None}
        statement = insert(ProxyAnnotation).values(
            profile_id=row.profile_id, provider_id=row.provider_id, **update,
        )
        with self.database.sessions.begin() as session:
            annotation = session.scalars(statement.on_conflict_do_update(
                index_elements=["profile_id", "provider_id"], set_=update,
            ).returning(ProxyAnnotation)).one()
            session.add(Operation(happened_at=utc_now(), action="annotation_edit", outcome="success"))
        return annotation

    def sync_history(self, profile_id: str) -> list[SyncRun]:
        statement = (
            select(SyncRun).where(SyncRun.profile_id == profile_id)
            .order_by(SyncRun.id.desc()).limit(200)
        )
        with self.database.sessions() as session:
            return list(session.scalars(statement))

    def latest_success(self, profile_id: str) -> SyncRun | None:
        statement = (
            select(SyncRun)
            .where(SyncRun.profile_id == profile_id, SyncRun.outcome == "success")
            .order_by(SyncRun.id.desc()).limit(1)
        )
        with self.database.sessions() as session:
            return session.scalar(statement)

    def operations(self) -> list[Operation]:
        with self.database.sessions() as session:
            return list(session.scalars(select(Operation).order_by(Operation.id.desc()).limit(200)))

    def log_operation(self, action: str, outcome: str) -> None:
        with self.database.sessions.begin() as session:
            session.add(Operation(happened_at=utc_now(), action=action, outcome=outcome))
