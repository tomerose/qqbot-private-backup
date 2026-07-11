from .base import BaseDBMixin
from .audit_log import AuditLogMixin
from .sensitive_word import SensitiveWordMixin
from .blacklist import BlacklistMixin


class DBManager(BaseDBMixin, AuditLogMixin, SensitiveWordMixin, BlacklistMixin):
    """数据库管理主类"""

    def _create_tables(self) -> None:
        AuditLogMixin._create_tables(self)
        SensitiveWordMixin._create_tables(self)
        BlacklistMixin._create_tables(self)
