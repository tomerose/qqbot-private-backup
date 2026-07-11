import atexit
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from ..common.types import DBError  # type: ignore


class BaseDBMixin:
    """基础数据库Mixin"""

    def __init__(self, db_path: str):
        """
        初始化数据库Mixin。

        Args:
            db_path: 数据库文件的路径。
        """
        self._db_path: str = db_path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        # fallback
        atexit.register(self.close)

    def __enter__(self) -> "BaseDBMixin":
        """
        同步上下文管理器入口。

        Returns:
            返回自身实例。
        """
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        同步上下文管理器出口。

        Args:
            exc_type: 异常类型。
            exc_val: 异常值。
            exc_tb: 异常回溯信息。
        """
        self.close()

    def initialize(self) -> None:
        """初始化数据库连接和表结构。"""
        with self._lock:
            if self._db is not None:
                return
            try:
                self._db = sqlite3.connect(
                    self._db_path,
                    timeout=30.0,
                    check_same_thread=False,
                )
                self._db.execute("PRAGMA foreign_keys = ON")
                self._db.execute("PRAGMA journal_mode = WAL")
                self._db.execute("PRAGMA synchronous = NORMAL")
                self._db.execute("PRAGMA busy_timeout = 5000")
                self._db.execute("PRAGMA temp_store = MEMORY")
                result = self._db.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise DBError(
                        f"数据库完整性检查失败: {result[0] if result else 'unknown'}"
                    )
                self._create_tables()

            except sqlite3.Error as e:
                if self._db:
                    self._db.close()
                    self._db = None
                raise DBError(f"无法连接到数据库: {e!s}")
            except Exception as e:
                if self._db:
                    self._db.close()
                    self._db = None
                raise DBError(f"初始化数据库失败: {e!s}")

    def _create_tables(self):
        """
        创建数据库表结构。

        Raises:
            NotImplementedError: 如果子类没有实现此方法。
        """
        raise NotImplementedError

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            if self._db is not None:
                try:
                    self._db.execute("PRAGMA wal_checkpoint")
                    self._db.close()
                except sqlite3.Error as e:
                    raise DBError(f"关闭数据库连接失败：{e!s}")
                finally:
                    self._db = None

    @contextmanager
    def _locked_db(self) -> Iterator[sqlite3.Connection]:
        """获取带锁的数据库连接，确保线程/进程安全。"""
        if not self._db:
            raise DBError("数据库未初始化或连接已关闭")
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except sqlite3.OperationalError as e:
                self._db.rollback()
                if "locked" in str(e).lower():
                    raise DBError("数据库繁忙，请稍后重试") from e
                raise DBError(f"数据库操作失败：{e!s}") from e
            except sqlite3.Error as e:
                self._db.rollback()
                raise DBError(f"数据库操作失败：{e!s}") from e
            except Exception:
                self._db.rollback()
                raise
