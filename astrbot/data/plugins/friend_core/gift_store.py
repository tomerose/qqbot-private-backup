"""Private, approval-gated birthday gift workflow storage."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

try:
    from data.plugins.claude_code_agent.encrypted_payload_store import (
        _dpapi,
        _harden_private_path,
    )
except ImportError:
    from claude_code_agent.encrypted_payload_store import _dpapi, _harden_private_path


_QQ = re.compile(r"^\d{5,12}$")
_ORDER = re.compile(r"^[a-f0-9]{12}$")
_PHONE = re.compile(r"^1[3-9]\d{9}$")
_VALID_STATES = {
    "candidate",
    "approved",
    "consented",
    "address_submitted",
    "address_relayed",
    "shipped",
    "completed",
    "rejected",
    "cancelled",
    "expired",
}
_MAGIC = b"XNG1"


@dataclass(frozen=True)
class GiftAddress:
    recipient: str
    phone: str
    address: str


@dataclass(frozen=True)
class GiftOrder:
    order_id: str
    qq_id: str
    birthday_year: int
    display_name: str
    status: str
    created_at: int
    expires_at: int
    carrier: str = ""
    tracking_no: str = ""


def parse_address_payload(raw: str) -> GiftAddress:
    """Parse recipient|phone|address without sending private data to an LLM."""
    parts = [part.strip() for part in str(raw or "").split("|")]
    if len(parts) != 3:
        raise ValueError("请按 收件人|手机号|详细地址 的格式填写")
    recipient, phone, address = parts
    if not 1 <= len(recipient) <= 30 or any(ch in recipient for ch in "\r\n|"):
        raise ValueError("收件人格式不正确")
    phone = re.sub(r"[ -]", "", phone)
    if not _PHONE.fullmatch(phone):
        raise ValueError("手机号格式不正确")
    if not 8 <= len(address) <= 200 or any(ch in address for ch in "\r\n|"):
        raise ValueError("详细地址应为 8 到 200 个字符")
    return GiftAddress(recipient, phone, address)


class GiftAddressVault:
    """Current-Windows-user encrypted address files with locked-down ACLs."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        _harden_private_path(self.root)

    def _path(self, order_id: str) -> Path:
        if not _ORDER.fullmatch(str(order_id or "")):
            raise ValueError("invalid order id")
        return self.root / f"{order_id}.bin"

    @staticmethod
    def _entropy(order_id: str) -> bytes:
        return hashlib.sha256(
            f"xiaoning-birthday-gift-v1:{order_id}".encode("ascii")
        ).digest()

    def write(self, order_id: str, address: GiftAddress) -> None:
        target = self._path(order_id)
        body = json.dumps(
            {
                "version": 1,
                "recipient": address.recipient,
                "phone": address.phone,
                "address": address.address,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = _dpapi(body, self._entropy(order_id), protect=True)
        temporary = self.root / f".{order_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(_MAGIC + protected)
            _harden_private_path(self.root, (temporary,))
            os.replace(temporary, target)
            _harden_private_path(self.root, (target,))
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, order_id: str) -> GiftAddress:
        raw = self._path(order_id).read_bytes()
        if not raw.startswith(_MAGIC):
            raise ValueError("invalid address payload")
        body = _dpapi(raw[len(_MAGIC) :], self._entropy(order_id), protect=False)
        data = json.loads(body.decode("utf-8"))
        if data.pop("version", None) != 1:
            raise ValueError("invalid address payload")
        return parse_address_payload(
            f"{data.get('recipient', '')}|{data.get('phone', '')}|{data.get('address', '')}"
        )

    def delete(self, order_id: str) -> None:
        self._path(order_id).unlink(missing_ok=True)


class GiftStore:
    """SQLite state machine; plaintext addresses never enter the database."""

    def __init__(self, database: Path, vault: GiftAddressVault | None = None):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault or GiftAddressVault(self.database.parent / "gift_addresses")
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gift_orders (
                    order_id TEXT PRIMARY KEY,
                    qq_id TEXT NOT NULL,
                    birthday_year INTEGER NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    carrier TEXT NOT NULL DEFAULT '',
                    tracking_no TEXT NOT NULL DEFAULT '',
                    UNIQUE(qq_id, birthday_year)
                )
                """
            )

    @staticmethod
    def _order(row: sqlite3.Row | None) -> GiftOrder | None:
        if row is None:
            return None
        return GiftOrder(
            row["order_id"], row["qq_id"], int(row["birthday_year"]),
            row["display_name"], row["status"], int(row["created_at"]),
            int(row["expires_at"]), row["carrier"], row["tracking_no"],
        )

    def get(self, order_id: str) -> GiftOrder | None:
        if not _ORDER.fullmatch(str(order_id or "")):
            return None
        with self._connect() as db:
            return self._order(
                db.execute("SELECT * FROM gift_orders WHERE order_id=?", (order_id,)).fetchone()
            )

    def active_for_user(self, qq_id: str) -> GiftOrder | None:
        if not _QQ.fullmatch(str(qq_id or "")):
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM gift_orders WHERE qq_id=?
                   ORDER BY birthday_year DESC, created_at DESC LIMIT 1""",
                (qq_id,),
            ).fetchone()
        return self._order(row)

    def pending_candidates(self, limit: int = 20) -> list[GiftOrder]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM gift_orders WHERE status='candidate'
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [order for row in rows if (order := self._order(row)) is not None]

    def create_candidate(self, qq_id: str, year: int, display_name: str = "") -> GiftOrder:
        if not _QQ.fullmatch(str(qq_id or "")) or not 2020 <= int(year) <= 2200:
            raise ValueError("invalid gift candidate")
        now = int(time.time())
        order_id = uuid.uuid4().hex[:12]
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO gift_orders
                   (order_id,qq_id,birthday_year,display_name,status,created_at,updated_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (order_id, qq_id, int(year), str(display_name or "")[:30],
                 "candidate", now, now, now + 30 * 86400),
            )
            row = db.execute(
                "SELECT * FROM gift_orders WHERE qq_id=? AND birthday_year=?",
                (qq_id, int(year)),
            ).fetchone()
        order = self._order(row)
        assert order is not None
        return order

    def transition(self, order_id: str, expected: set[str], target: str) -> GiftOrder:
        if target not in _VALID_STATES:
            raise ValueError("invalid target state")
        order = self.get(order_id)
        if order is None or order.status not in expected:
            raise ValueError("当前状态不允许此操作")
        now = int(time.time())
        expires = now + (
            7 * 86400
            if target in {"approved", "consented", "address_submitted"}
            else 30 * 86400
        )
        with self._connect() as db:
            changed = db.execute(
                """UPDATE gift_orders SET status=?,updated_at=?,expires_at=?
                   WHERE order_id=? AND status=?""",
                (target, now, expires, order_id, order.status),
            ).rowcount
        if changed != 1:
            raise ValueError("状态已变化，请重新查询")
        result = self.get(order_id)
        assert result is not None
        return result

    def submit_address(self, order_id: str, qq_id: str, raw: str) -> GiftOrder:
        order = self.get(order_id)
        if order is None or order.qq_id != qq_id or order.status != "consented":
            raise ValueError("请先完成管理员审批和隐私同意")
        address = parse_address_payload(raw)
        self.vault.write(order_id, address)
        try:
            return self.transition(order_id, {"consented"}, "address_submitted")
        except Exception:
            self.vault.delete(order_id)
            raise

    def relay_address(self, order_id: str) -> GiftAddress:
        order = self.get(order_id)
        if order is None or order.status != "address_submitted":
            raise ValueError("地址不可转交")
        return self.vault.read(order_id)

    def mark_address_relayed(self, order_id: str) -> GiftOrder:
        order = self.transition(order_id, {"address_submitted"}, "address_relayed")
        self.vault.delete(order_id)
        return order

    def mark_shipped(self, order_id: str, carrier: str, tracking_no: str) -> GiftOrder:
        carrier = str(carrier or "").strip()[:30]
        tracking_no = str(tracking_no or "").strip()[:60]
        if not carrier or not re.fullmatch(r"[A-Za-z0-9-]{5,60}", tracking_no):
            raise ValueError("快递公司或单号格式不正确")
        order = self.get(order_id)
        if order is None or order.status != "address_relayed":
            raise ValueError("地址尚未安全转交")
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                """UPDATE gift_orders SET status='shipped',carrier=?,tracking_no=?,updated_at=?
                   WHERE order_id=? AND status='address_relayed'""",
                (carrier, tracking_no, now, order_id),
            )
        result = self.get(order_id)
        assert result is not None
        return result

    def close(self, order_id: str, target: str) -> GiftOrder:
        expected_by_target = {
            "rejected": {"candidate", "approved"},
            "cancelled": {
                "candidate", "approved", "consented", "address_submitted", "address_relayed"
            },
            "completed": {"shipped"},
        }
        expected = expected_by_target.get(target)
        if expected is None:
            raise ValueError("invalid closing state")
        result = self.transition(order_id, expected, target)
        self.vault.delete(order_id)
        return result

    def purge_expired(self, now: int | None = None) -> int:
        now = int(now or time.time())
        with self._connect() as db:
            rows = db.execute(
                """SELECT order_id FROM gift_orders WHERE expires_at<?
                   AND status NOT IN ('completed','rejected','cancelled','expired')""",
                (now,),
            ).fetchall()
            db.execute(
                """UPDATE gift_orders SET status='expired',updated_at=? WHERE expires_at<?
                   AND status NOT IN ('completed','rejected','cancelled','expired')""",
                (now, now),
            )
        for row in rows:
            self.vault.delete(row["order_id"])
        return len(rows)


def consent_notice(admin_qq: str) -> str:
    return (
        "管理员已批准一份生日礼物候选，但这不是强制领取。若要邮寄，需要收集收件人、手机号和详细地址，"
        f"仅用于本次寄送，并通过私聊转交给管理员 QQ {admin_qq}；转交成功后机器人立即删除地址密文，"
        "最迟 7 天自动删除。你可以随时发送 /生日礼物 取消 撤回。\n"
        "已满 14 岁请回复：/生日礼物 同意且确认已满14岁\n"
        "未满 14 岁须由监护人确认后回复：/生日礼物 同意且已获监护人授权"
    )
