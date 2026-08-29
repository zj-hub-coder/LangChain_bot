"""会话管理：user_id -> langgraph thread_id 映射。

langgraph 的 MemorySaver checkpointer 用 thread_id 隔离会话历史，
因此只需维护 user_id 到 thread_id 的一对一映射，不需要管理 conversation_id。
"""
import uuid
from collections import OrderedDict
from threading import RLock


class SessionManager:
    """轻量级会话管理器：线程安全的 user_id -> thread_id 映射。"""

    def __init__(self, capacity: int = 5000):
        self._sessions: OrderedDict[str, str] = OrderedDict()
        self._capacity = capacity
        self._lock = RLock()

    def get_or_create(self, user_id: str) -> str:
        """获取或创建 thread_id。"""
        with self._lock:
            if user_id in self._sessions:
                self._sessions.move_to_end(user_id)
                return self._sessions[user_id]
            thread_id = uuid.uuid4().hex
            self._sessions[user_id] = thread_id
            if len(self._sessions) > self._capacity:
                self._sessions.popitem(last=False)
            return thread_id

    def remove(self, user_id: str) -> None:
        with self._lock:
            self._sessions.pop(user_id, None)
