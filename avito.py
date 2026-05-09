"""
avito.py — асинхронный клиент Авито Messenger API.

Авторизация: OAuth 2.0, grant_type=client_credentials
Токен действителен 24 часа, обновляется автоматически.

Используемые эндпоинты:
  POST https://api.avito.ru/token
  GET  https://api.avito.ru/core/v1/accounts/self
  GET  https://api.avito.ru/messenger/v2/accounts/{user_id}/chats
  GET  https://api.avito.ru/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/
  POST https://api.avito.ru/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages
  POST https://api.avito.ru/messenger/v1/accounts/{user_id}/chats/{chat_id}/read
"""

import logging
import time

import httpx

log = logging.getLogger("AVITO")

BASE_URL = "https://api.avito.ru"


class AvitoClient:
    """
    Клиент Авито Messenger API для одного аккаунта.

    Использование:
        client = AvitoClient(client_id="...", client_secret="...")
        chats  = await client.get_chats(unread_only=True)
        await  client.send_message(chat_id, "Привет!")
    """

    def __init__(self, client_id: str, client_secret: str, name: str = "Авито"):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.name          = name          # метка для логов (название объекта)
        self._token:         str      = ""
        self._token_expires: float    = 0.0
        self._user_id:       int|None = None

    # ─── Авторизация ─────────────────────────────────────────────────────────

    async def _ensure_token(self) -> None:
        """Получает / обновляет Bearer-токен если он истёк (запас 60 сек)."""
        if self._token and time.time() < self._token_expires - 60:
            return
        log.info("[%s] Получаем новый токен Авито…", self.name)
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                f"{BASE_URL}/token",
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     self.client_id,
                    "client_secret": self.client_secret,
                    "scope":         "messenger:read messenger:write",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        self._token        = data["access_token"]
        expires_in         = data.get("expires_in", 86400)
        self._token_expires = time.time() + expires_in
        log.info("[%s] ✅ Токен получен, истекает через %d сек", self.name, expires_in)

    def _auth(self) -> dict:
        """Заголовок авторизации."""
        return {"Authorization": f"Bearer {self._token}"}

    # ─── Аккаунт ─────────────────────────────────────────────────────────────

    async def get_user_id(self) -> int:
        """Возвращает числовой user_id аккаунта Авито (кешируется)."""
        if self._user_id:
            return self._user_id
        await self._ensure_token()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{BASE_URL}/core/v1/accounts/self",
                headers=self._auth(),
            )
            resp.raise_for_status()
            self._user_id = resp.json()["id"]
        log.info("[%s] ✅ user_id = %d", self.name, self._user_id)
        return self._user_id

    # ─── Чаты ────────────────────────────────────────────────────────────────

    async def get_chats(
        self,
        unread_only: bool = True,
        chat_types: str = "u2i",
        limit: int = 100,
    ) -> list[dict]:
        """
        Возвращает список чатов.

        unread_only=True  — только непрочитанные (для поллера).
        chat_types="u2i"  — чаты по объявлениям (покупатель → продавец).
        """
        await self._ensure_token()
        uid = await self.get_user_id()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{BASE_URL}/messenger/v2/accounts/{uid}/chats",
                headers=self._auth(),
                params={
                    "unread_only": str(unread_only).lower(),
                    "chat_types":  chat_types,
                    "limit":       limit,
                },
            )
            resp.raise_for_status()
            return resp.json().get("chats", [])

    async def get_chat(self, chat_id: str) -> dict:
        """Возвращает данные конкретного чата и последнее сообщение."""
        await self._ensure_token()
        uid = await self.get_user_id()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{BASE_URL}/messenger/v2/accounts/{uid}/chats/{chat_id}",
                headers=self._auth(),
            )
            resp.raise_for_status()
            return resp.json()

    # ─── Сообщения ───────────────────────────────────────────────────────────

    async def get_messages(self, chat_id: str, limit: int = 20) -> list[dict]:
        """
        Возвращает последние сообщения чата (не помечает прочитанными).
        Вызови mark_read() после обработки.
        """
        await self._ensure_token()
        uid = await self.get_user_id()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{BASE_URL}/messenger/v3/accounts/{uid}/chats/{chat_id}/messages/",
                headers=self._auth(),
                params={"limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            # API возвращает {"messages": [...]} newest-first — извлекаем список
            if isinstance(data, dict):
                return data.get("messages", [])
            return data

    async def send_message(self, chat_id: str, text: str) -> dict:
        """Отправляет текстовое сообщение в чат."""
        await self._ensure_token()
        uid = await self.get_user_id()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                f"{BASE_URL}/messenger/v1/accounts/{uid}/chats/{chat_id}/messages",
                headers=self._auth(),
                json={"message": {"text": text}, "type": "text"},
            )
            resp.raise_for_status()
            return resp.json()

    async def mark_read(self, chat_id: str) -> None:
        """Помечает все сообщения чата прочитанными."""
        await self._ensure_token()
        uid = await self.get_user_id()
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                f"{BASE_URL}/messenger/v1/accounts/{uid}/chats/{chat_id}/read",
                headers=self._auth(),
            )
            resp.raise_for_status()
