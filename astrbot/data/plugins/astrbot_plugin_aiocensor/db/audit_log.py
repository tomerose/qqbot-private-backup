import json
import uuid
import sqlite3
from typing import Any

from ..common.types import (  # type: ignore
    AuditLogEntry,
    CensorResult,
    DBError,
    Message,
    RiskLevel,
)


class AuditLogMixin:
    """审计日志相关功能"""

    _db: sqlite3.Connection | None

    def _create_tables(self) -> None:
        """
        创建审计日志表及其索引。
        如果表或索引已存在，则不会重复创建。
        """
        try:
            with self._locked_db() as db:
                db.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message_timestamp INTEGER NOT NULL,
                    risk_level INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    result_extra TEXT,
                    entry_extra TEXT
                )""")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_source ON audit_logs(source)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_time ON audit_logs(message_timestamp)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_risk ON audit_logs(risk_level)"
                )
        except sqlite3.Error as e:
            raise DBError(f"创建审计日志表失败: {e!s}")

    def add_audit_log(self, result: CensorResult, extra: dict | None = None) -> str:
        """
        添加一条审计日志记录。

        Args:
            result: 审查结果对象，包含消息内容、来源、时间戳、风险等级、原因和额外信息。
            extra: 审计日志条目的额外信息，可选。
        Returns:
            新添加的审计日志记录的ID。
        Raises:
            DBError: 数据库未初始化或查询失败。
        """
        log_id = str(uuid.uuid4())
        reason_str = json.dumps(list(result.reason)) if result.reason else ""
        result_extra_str = json.dumps(result.extra) if result.extra else None
        entry_extra_str = json.dumps(extra) if extra else None

        try:
            with self._locked_db() as db:
                cursor = db.cursor()
                cursor.execute(
                    """
                    INSERT INTO audit_logs
                    (id, content, source, message_timestamp, risk_level, reason, result_extra, entry_extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        log_id,
                        result.message.content,
                        result.message.source,
                        result.message.timestamp,
                        result.risk_level.value,
                        reason_str,
                        result_extra_str,
                        entry_extra_str,
                    ),
                )
                return log_id
        except sqlite3.Error as e:
            raise DBError(f"添加审计日志失败: {e!s}")

    def get_audit_logs_count(
        self,
        start_time: int | None = None,
        end_time: int | None = None,
        source: str | None = None,
        risk_level: RiskLevel | None = None,
        search_term: str | None = None,
    ) -> int:
        """
        获取符合条件的审计日志记录总数。

        Args:
            start_time: 起始时间戳，可选。
            end_time: 结束时间戳，可选。
            source: 消息来源，可选。
            risk_level: 风险等级，可选。

        Returns:
            符合条件的审计日志记录总数。

        Raises:
            DBError: 数据库未初始化或查询失败。
        """
        query = "SELECT COUNT(*) FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if start_time:
            query += " AND message_timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND message_timestamp <= ?"
            params.append(end_time)
        if source:
            query += " AND source = ?"
            params.append(source)
        if risk_level:
            query += " AND risk_level = ?"
            params.append(risk_level.value)
        if search_term:
            query += " AND (content LIKE ? OR result_extra LIKE ?)"
            pattern = f"%{search_term}%"
            params.extend([pattern, pattern])
        try:
            with self._locked_db() as db:
                cursor = db.execute(query, params)
                result = cursor.fetchone()
                return result[0] if result else 0
        except sqlite3.Error as e:
            raise DBError(f"获取审计日志总数失败：{e!s}")

    def get_audit_logs(
        self,
        start_time: int | None = None,
        end_time: int | None = None,
        source: str | None = None,
        risk_level: RiskLevel | None = None,
        search_term: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLogEntry]:
        """
        获取符合条件的审计日志记录列表。

        Args:
            start_time: 起始时间戳，可选。
            end_time: 结束时间戳，可选。
            source: 消息来源，可选。
            risk_level: 风险等级，可选。
            limit: 返回的最大记录数，默认为100。
            offset: 偏移量，用于分页，默认为0。

        Returns:
            符合条件的审计日志记录列表。

        Raises:
            DBError: 数据库未初始化或查询失败。
        """
        query = """
            SELECT id, content, source, message_timestamp, risk_level, reason, result_extra, entry_extra
            FROM audit_logs WHERE 1=1
        """
        params: list[Any] = []
        if start_time:
            query += " AND message_timestamp >= ?"
            params.append(start_time)
        if end_time:
            query += " AND message_timestamp <= ?"
            params.append(end_time)
        if source:
            query += " AND source = ?"
            params.append(source)
        if risk_level:
            query += " AND risk_level = ?"
            params.append(risk_level.value)
        if search_term:
            query += " AND (content LIKE ? OR result_extra LIKE ?)"
            pattern = f"%{search_term}%"
            params.extend([pattern, pattern])
        query += " ORDER BY message_timestamp DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        try:
            with self._locked_db() as db:
                cursor = db.execute(query, params)
                rows = cursor.fetchall()
                return [self._parse_audit_log(row) for row in rows]
        except sqlite3.Error as e:
            raise DBError(f"获取审计日志失败：{e!s}")

    def delete_audit_log(self, log_id: str) -> bool:
        """
        删除指定ID的审计日志记录。

        Args:
            log_id: 要删除的审计日志记录的ID。

        Returns:
            如果删除成功返回True，否则返回False。
        Raises:
            DBError: 数据库未初始化或删除失败。
        """
        try:
            with self._locked_db() as db:
                cursor = db.cursor()
                cursor.execute("DELETE FROM audit_logs WHERE id = ?", (log_id,))
                deleted = cursor.rowcount > 0
                return deleted
        except sqlite3.Error as e:
            raise DBError(f"删除审计日志失败：{e!s}")

    def get_audit_log(self, log_id: str) -> AuditLogEntry | None:
        """
        获取指定ID的审计日志记录。

        Args:
            log_id: 要获取的审计日志记录的ID。

        Returns:
            如果找到记录则返回AuditLogEntry对象，否则返回None。

        Raises:
            DBError: 数据库未初始化或查询失败。
        """
        try:
            with self._locked_db() as db:
                cursor = db.execute(
                    """
                    SELECT id, content, source, message_timestamp, risk_level, reason, result_extra, entry_extra
                    FROM audit_logs WHERE id = ?
                    """,
                    (log_id,),
                )
                row = cursor.fetchone()
                return self._parse_audit_log(row) if row else None
        except sqlite3.Error as e:
            raise DBError(f"获取审计日志失败：{e!s}")

    def _parse_audit_log(self, row) -> AuditLogEntry:
        """
        解析数据库查询结果行，将其转换为AuditLogEntry对象。

        Args:
            row: 数据库查询结果的一行数据。

        Returns:
            解析后的AuditLogEntry对象。
        """
        (
            log_id,
            content,
            source_str,
            message_ts,
            risk_level_value,
            reason_str,
            result_extra_str,
            entry_extra_str,
        ) = row

        try:
            reason_set = set(json.loads(reason_str)) if reason_str else set()
        except json.JSONDecodeError:
            reason_set = set()

        try:
            result_extra = json.loads(result_extra_str) if result_extra_str else None
        except json.JSONDecodeError:
            result_extra = None

        try:
            entry_extra = json.loads(entry_extra_str) if entry_extra_str else None
        except json.JSONDecodeError:
            entry_extra = None

        risk_level_enum = RiskLevel(risk_level_value)
        message = Message(content=content, source=source_str, timestamp=message_ts)
        censor_result = CensorResult(
            message=message,
            risk_level=risk_level_enum,
            reason=reason_set,
            extra=result_extra,
        )

        return AuditLogEntry(id=log_id, result=censor_result, extra=entry_extra)
