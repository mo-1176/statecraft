"""
Web API。会話は session_id で見分け、会話履歴と引き継ぐ記憶はサーバー側で持つ。

    pip install fastapi uvicorn
    python api.py                 # または: uvicorn api:app --reload

    POST /chat  {"message": "深夜特急を予約したい。会員番号はM-001"}
      → {"session_id": "...", "message": "...", "awaiting_reply": false}
    POST /chat  {"session_id": "...", "message": "さっきの予約の受付番号は？"}   # 同じ会話の続き

http://127.0.0.1:8000/docs から試せる。エージェントの経過はサーバーの端末に出る。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from library.api_key import ApiKey
from library.session import LibrarySession


class ChatRequest(BaseModel):
    message: str
    # 省略すると新しい会話を始める。続きを話すときは、前の応答の session_id を渡す
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    message: str
    # 利用者からの返答を待っているか（足りない情報を尋ねたときなど）
    awaiting_reply: bool


@dataclass
class Conversation:
    session: LibrarySession = field(default_factory=LibrarySession)
    # 同じ会話へ同時に2つ届くと、履歴と記憶の持ち越しが食い違う。1つずつ処理する
    lock: Lock = field(default_factory=Lock)


class SessionStore:
    """
    session_id → 会話。プロセスのメモリに置くだけの簡易版。

    再起動で消え、サーバーを複数台に増やすと台ごとに別の会話になる。実運用では
    Redis や DB に置く（保存するのは LibrarySession の history と carry_over だけでよい）。
    """

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._lock = Lock()

    def start(self) -> tuple[str, Conversation]:
        session_id = uuid4().hex
        with self._lock:
            conversation = self._conversations[session_id] = Conversation()
        return session_id, conversation

    def get(self, session_id: str) -> Conversation | None:
        with self._lock:
            return self._conversations.get(session_id)


store = SessionStore()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # サーバーでは入力欄を出さない。キーは環境変数か .env に置く
    ApiKey().ensure(ask=False)
    yield


app = FastAPI(title="statecraft sample: library", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# async ではなく def にしている。エージェントの呼び出しは待ち時間の長い同期処理なので、
# FastAPI が別スレッドで動かす（async def の中で呼ぶと、その間ほかのリクエストが止まる）。
@app.post("/chat")
def chat(request: ChatRequest) -> ChatResponse:
    if request.session_id is None:
        session_id, conversation = store.start()
    else:
        session_id = request.session_id
        found = store.get(session_id)
        if found is None:
            raise HTTPException(status_code=404, detail="session_id が見つかりません")
        conversation = found

    with conversation.lock:
        reply = conversation.session.ask(request.message)

    return ChatResponse(
        session_id=session_id, message=reply.message, awaiting_reply=reply.awaiting_reply
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app)
