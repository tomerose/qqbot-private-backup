import asyncio
import os
import atexit
from functools import wraps
from multiprocessing.queues import Queue as MPQueue
from typing import Any

import jwt
from hypercorn.asyncio import serve
from hypercorn.config import Config
from quart import Quart, Response, jsonify, request, send_from_directory

from astrbot.api import logger

from .db import DBManager  # type:ignore


class WebUIServer:
    """AIOCENSOR Web UI类"""

    def __init__(
        self,
        password: str,
        secret_key: str,
        notification_queue: MPQueue | None = None,
    ):
        data_path = os.path.join(os.getcwd(), "data", "aiocensor")
        if not os.path.exists(data_path):
            os.makedirs(data_path)
        self._db_mgr = DBManager(os.path.join(data_path, "censor.db"))
        self._db_mgr.initialize()
        # fallback
        atexit.register(self._db_mgr.close)
        self._app = Quart(__name__, static_folder="static", static_url_path="")
        self._password = password
        self._secret_key = secret_key
        self._notification_queue = notification_queue
        self._server_task: asyncio.Task | None = None
        self._setup_app()

    def _notify_change(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        """向主进程发送数据更新通知。"""
        if not self._notification_queue:
            return
        message: dict[str, Any] = {"type": event_type}
        if payload:
            message["payload"] = payload
        try:
            self._notification_queue.put_nowait(message)
        except Exception as exc:
            logger.warning(f"发送更新通知失败: {exc!s}")

    def _setup_app(self):
        """配置Quart应用实例"""

        async def format_response(
            data: dict[str, Any] | None = None,
            message: str = "",
            status_code: int = 200,
        ) -> tuple[Response, int]:
            """格式化API响应"""
            response = {"success": status_code < 400, "message": message}
            if data is not None:
                response.update(data)
            return jsonify(response), status_code

        def generate_token() -> str:
            """生成长期有效的 JWT 访问令牌"""
            return jwt.encode(
                {
                    "role": "admin",
                },
                self._secret_key,
                algorithm="HS256",
            )

        def verify_token(token: str) -> dict[str, Any] | None:
            """验证JWT令牌"""
            try:
                payload = jwt.decode(token, self._secret_key, algorithms=["HS256"])
                return payload
            except jwt.ExpiredSignatureError:
                return None
            except jwt.InvalidTokenError:
                return None

        def clean_input(text: str) -> str:
            """预处理输入文本，去除空格"""
            if text:
                return text.strip()
            return ""

        def token_required(func):
            """验证请求中的令牌的装饰器"""

            @wraps(func)
            async def decorated(*args, **kwargs):
                auth_header = request.headers.get("Authorization")
                if not auth_header or not auth_header.startswith("Bearer "):
                    return await format_response(
                        message="缺少或无效的令牌", status_code=401
                    )

                token = auth_header.split(" ")[1].strip()
                payload = verify_token(token)
                if not payload:
                    return await format_response(
                        message="令牌无效或已过期", status_code=401
                    )

                request.auth = payload
                return await func(*args, **kwargs)

            return decorated

        @self._app.route("/api/login", methods=["POST"])
        async def login() -> tuple[Response, int]:
            """处理用户登录并发放令牌"""
            try:
                data = await request.get_json()
                if not data:
                    return await format_response(
                        message="无效的请求数据", status_code=400
                    )

                password = data.get("password", "")
                password = clean_input(password)

                if not password:
                    return await format_response(message="缺少密码", status_code=400)

                expected_password = self._password
                if password != expected_password:
                    logger.warning("无效的登录尝试，密码错误")
                    return await format_response(message="密码错误", status_code=401)

                access_token = generate_token()
                return await format_response(
                    data={"access_token": access_token},
                    message="登录成功",
                )
            except Exception as e:
                logger.error(f"登录错误: {e!s}")
                return await format_response(message="登录失败", status_code=500)

        @self._app.route("/api/audit-logs", methods=["GET"])
        @token_required
        async def get_audit_logs() -> tuple[Response, int]:
            """获取审计日志列表"""
            try:
                args = request.args
                limit = int(args.get("limit", 10))
                offset = int(args.get("offset", 0))
                search = args.get("search")

                logs = self._db_mgr.get_audit_logs(
                    search_term=search,
                    limit=limit,
                    offset=offset,
                )
                total = self._db_mgr.get_audit_logs_count(search_term=search)

                return await format_response(
                    data={
                        "logs": [
                            {
                                "id": log.id,
                                "content": log.result.message.content,
                                "user_id": (
                                    log.result.extra.get("user_id_str")
                                    if log.result.extra
                                    else ""
                                ),
                                "source": log.result.message.source,
                                "systemJudgment": log.result.risk_level.name,
                                "reason": str(log.result.reason),
                                "time": log.result.message.timestamp,
                            }
                            for log in logs
                        ],
                        "total": total,
                    }
                )
            except Exception as e:
                logger.error(f"获取审计日志失败: {e!s}")
                return await format_response(message="获取日志失败", status_code=500)

        @self._app.route("/api/audit-logs/<string:log_id>/dispose", methods=["POST"])
        @token_required
        async def dispose_log(log_id: str) -> tuple[Response, int]:
            """处理审计日志的处置操作"""
            try:
                log_id = clean_input(log_id)
                data = await request.get_json()
                if not data:
                    return await format_response(
                        message="无效的请求数据", status_code=400
                    )

                actions = data.get("actions", [])
                log_entry = None
                notify_dispose = False

                for action in actions:
                    if action == "block":
                        if log_entry is None:
                            log_entry = self._db_mgr.get_audit_log(log_id)
                        if not log_entry:
                            return await format_response(
                                message="找不到指定的审核日志", status_code=404
                            )

                        if (
                            not log_entry.result.extra
                            or "user_id_str" not in log_entry.result.extra
                        ):
                            return await format_response(
                                message="审核日志中缺少用户ID信息", status_code=400
                            )

                        user_id = log_entry.result.extra["user_id_str"]
                        reason = f"根据审核日志 {log_id} 添加"

                        existing = self._db_mgr.search_blacklist(user_id)
                        if any(entry.identifier == user_id for entry in existing):
                            continue

                        self._db_mgr.add_blacklist_entry(user_id, reason)
                        self._notify_change(
                            "blacklist_updated", {"user_id": user_id, "reason": reason}
                        )
                    elif action == "dispose":
                        if log_entry is None:
                            log_entry = self._db_mgr.get_audit_log(log_id)
                        if not log_entry:
                            return await format_response(
                                message="找不到指定的审核日志", status_code=404
                            )
                        notify_dispose = True
                    else:
                        return await format_response(
                            message="无效的操作", status_code=400
                        )
                if notify_dispose:
                    self._notify_change(
                        "audit_log_dispose",
                        {"log_id": log_id, "actions": actions},
                    )
                self._notify_change("audit_log_updated", {"log_id": log_id})
                return await format_response(message="处置提交成功，请通过控制台查看结果", status_code=200)
            except Exception as e:
                logger.error(f"处置失败 {log_id}: {e!s}", exc_info=True)
                return await format_response(message="处置失败", status_code=500)

        @self._app.route("/api/audit-logs/<string:log_id>/ignore", methods=["POST"])
        @token_required
        async def ignore_log(log_id: str) -> tuple[Response, int]:
            """忽略（删除）审计日志"""
            try:
                log_id = clean_input(log_id)
                if not self._db_mgr.delete_audit_log(log_id):
                    return await format_response(message="日志不存在", status_code=404)
                self._notify_change("audit_log_updated", {"log_id": log_id})
                return await format_response(
                    message="已删除日志", data={"log_id": log_id}
                )
            except Exception as e:
                logger.error(f"删除日志失败 {log_id}: {e!s}", exc_info=True)
                return await format_response(message="删除日志失败", status_code=500)

        @self._app.route("/api/blacklist", methods=["GET"])
        @token_required
        async def get_blacklist() -> tuple[Response, int]:
            """获取黑名单列表"""
            try:
                args = request.args
                limit = int(args.get("limit", 10))
                offset = int(args.get("offset", 0))
                search = args.get("search")

                if search:
                    entries = self._db_mgr.search_blacklist(
                        search, limit=limit, offset=offset
                    )
                else:
                    entries = self._db_mgr.get_blacklist_entries(
                        limit=limit, offset=offset
                    )
                total = self._db_mgr.get_blacklist_entries_count(search)

                return await format_response(
                    data={
                        "records": [
                            {
                                "id": entry.id,
                                "user": entry.identifier,
                                "reason": entry.reason,
                            }
                            for entry in entries
                        ],
                        "total": total,
                    }
                )
            except Exception as e:
                logger.error(f"获取黑名单失败: {e!s}")
                return await format_response(message="获取黑名单失败", status_code=500)

        @self._app.route("/api/blacklist", methods=["POST"])
        @token_required
        async def add_to_blacklist() -> tuple[Response, int]:
            """添加条目到黑名单"""
            try:
                data = await request.get_json()
                if not data:
                    return await format_response(
                        message="无效的请求数据", status_code=400
                    )

                user_id = data.get("userId", "")
                reason = data.get("reason", "")

                user_id = clean_input(user_id)
                reason = clean_input(reason)

                if not user_id or not reason:
                    return await format_response(
                        message="缺少必要参数", status_code=400
                    )

                existing = self._db_mgr.search_blacklist(user_id)
                if any(entry.identifier == user_id for entry in existing):
                    return await format_response(
                        message="用户已在黑名单中", status_code=409
                    )

                record_id = self._db_mgr.add_blacklist_entry(user_id, reason)
                self._notify_change(
                    "blacklist_updated", {"user_id": user_id, "reason": reason}
                )
                return await format_response(
                    data={"id": record_id, "user": user_id, "reason": reason},
                    message="已添加至黑名单",
                    status_code=201,
                )
            except Exception as e:
                logger.error(f"添加黑名单失败: {e!s}")
                return await format_response(message="添加黑名单失败", status_code=500)

        @self._app.route("/api/blacklist/<string:record_id>", methods=["DELETE"])
        @token_required
        async def delete_blacklist(record_id: str) -> tuple[Response, int]:
            """从黑名单中删除条目"""
            try:
                record_id = clean_input(record_id)
                if not self._db_mgr.delete_blacklist_entry(record_id):
                    return await format_response(
                        message="黑名单记录不存在", status_code=404
                    )
                self._notify_change("blacklist_updated", {"record_id": record_id})
                return await format_response(
                    data={"record_id": record_id}, message="已移出黑名单"
                )
            except Exception as e:
                logger.error(f"移除黑名单失败: {e!s}", exc_info=True)
                return await format_response(message="移除黑名单失败", status_code=500)

        @self._app.route("/api/sensitive-words", methods=["GET"])
        @token_required
        async def get_sensitive_words() -> tuple[Response, int]:
            """获取敏感词列表"""
            try:
                args = request.args
                limit = int(args.get("limit", 10))
                offset = int(args.get("offset", 0))
                search = args.get("search")

                words = self._db_mgr.get_sensitive_words(
                    search_term=search, limit=limit, offset=offset
                )
                total = self._db_mgr.get_sensitive_words_count(search)

                return await format_response(
                    data={
                        "words": [
                            {
                                "id": word.id,
                                "word": word.word,
                                "updatedAt": word.updated_at,
                            }
                            for word in words
                        ],
                        "total": total,
                    }
                )
            except Exception as e:
                logger.error(f"获取敏感词失败: {e!s}")
                return await format_response(message="获取敏感词失败", status_code=500)

        @self._app.route("/api/sensitive-words", methods=["POST"])
        @token_required
        async def add_sensitive_word() -> tuple[Response, int]:
            """添加敏感词"""
            try:
                data = await request.get_json()
                if not data:
                    return await format_response(
                        message="无效的请求数据", status_code=400
                    )

                word = data.get("word", "")
                word = clean_input(word)

                if not word:
                    return await format_response(
                        message="缺少敏感词内容", status_code=400
                    )

                existing = self._db_mgr.get_sensitive_words(word, limit=1)
                if any(entry.word == word for entry in existing):
                    return await format_response(
                        message="敏感词已存在", status_code=409
                    )

                word_id = self._db_mgr.add_sensitive_word(word)
                self._notify_change("sensitive_words_updated", {"word": word})
                return await format_response(
                    data={"id": word_id, "word": word},
                    message="敏感词添加成功",
                    status_code=201,
                )
            except Exception as e:
                logger.error(f"添加敏感词失败: {e!s}")
                return await format_response(message="添加敏感词失败", status_code=500)

        @self._app.route("/api/sensitive-words/<string:word_id>", methods=["DELETE"])
        @token_required
        async def delete_sensitive_word(word_id: str) -> tuple[Response, int]:
            """删除敏感词"""
            try:
                word_id = clean_input(word_id)
                if not self._db_mgr.delete_sensitive_word(word_id):
                    return await format_response(
                        message="敏感词不存在", status_code=404
                    )
                self._notify_change("sensitive_words_updated", {"word_id": word_id})
                return await format_response(
                    data={"word_id": word_id}, message="敏感词删除成功"
                )
            except Exception as e:
                logger.error(f"删除敏感词失败: {e!s}")
                return await format_response(message="删除敏感词失败", status_code=500)

        @self._app.route("/", strict_slashes=False)
        async def serve_root():
            """提供前端静态文件"""
            return await send_from_directory(self._app.static_folder, "index.html")

    async def start(self, host: str, port: int):
        """启动HTTP服务"""
        config = Config()
        config.bind = [f"{host}:{port}"]
        self._server_task = asyncio.create_task(serve(self._app, config))
        logger.info(f"AIOCENSOR WebUI 服务已启动于 {host}:{port}")

        try:
            await self._server_task
        except asyncio.CancelledError:
            logger.info("请求关闭服务")
        finally:
            await self.close()

    async def close(self):
        """关闭资源"""
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass


def run_server(
    secret_key: str,
    password: str,
    host: str,
    port: int,
    notification_queue: MPQueue | None = None,
):
    """子进程入口"""
    server = WebUIServer(password, secret_key, notification_queue)
    asyncio.run(server.start(host, port))
