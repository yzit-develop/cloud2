"""問い合わせ管理アプリの API（Azure Functions / Python）

エンドポイント
  GET  /api/health      動作確認
  GET  /api/inquiries   問い合わせの一覧を返す（新しい順）
  POST /api/inquiries   問い合わせを1件登録する  {"title": "...", "body": "..."}
  GET  /api/ai/config-check  OpenRouter のキーが届いているかを確かめる（コマ6。値は返さない）
  POST /api/chat        アシスタントに質問する（コマ7。応答をストリーミングで返す）
                        {"conversationId": "...（省略すると新しい会話）", "message": "..."}
  GET  /api/conversations?id=...  会話の履歴を返す（コマ7。画面を開き直したときに使う）

接続先などの設定は、コードに書かずに「アプリ設定」（環境変数）から読む。
注意: この時点では認証がなく、誰でも読み書きできる（コマ11で認証を加える）。

コマ7から、応答を少しずつ返す「HTTP ストリーミング」を使う。
Azure Functions（Python）では、ストリーミングを有効にすると、この関数アプリの
すべての HTTP の関数で FastAPI の Request / Response の型を使う必要がある。
"""

import datetime
import hashlib
import json
import os
import uuid

import azure.functions as func
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from azurefunctions.extensions.http.fastapi import JSONResponse, Request, StreamingResponse

import llm
from history import trim_history

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MAX_TITLE = 100
MAX_BODY = 2000
MAX_MESSAGE = 1000  # アシスタントへの質問1件の長さの上限（文字）

_database = None


def get_database():
    """Cosmos DB のデータベースに接続する（最初の1回だけ接続し、以降は使い回す）"""
    global _database
    if _database is None:
        client = CosmosClient(
            os.environ["COSMOS_ENDPOINT"],
            credential=DefaultAzureCredential(),  # キーではなく、マネージドIDで接続する（コマ6）
        )
        _database = client.get_database_client(os.environ["COSMOS_DATABASE"])
    return _database


def get_inquiries():
    return get_database().get_container_client(os.environ["COSMOS_CONTAINER"])


def get_conversations():
    return get_database().get_container_client(os.environ["COSMOS_CONVERSATIONS_CONTAINER"])


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@app.route(route="health", methods=["GET"])
async def health(req: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "llmProvider": llm.provider_name()})


@app.route(route="inquiries", methods=["GET"])
async def list_inquiries(req: Request) -> JSONResponse:
    query = (
        "SELECT c.id, c.title, c.body, c.status, c.createdAt "
        "FROM c ORDER BY c.createdAt DESC"
    )
    items = list(get_inquiries().query_items(query=query, enable_cross_partition_query=True))
    return JSONResponse(items)


@app.route(route="inquiries", methods=["POST"])
async def create_inquiry(req: Request) -> JSONResponse:
    try:
        data = await req.json()
    except ValueError:
        return JSONResponse({"error": "JSON の形式が正しくない"}, status_code=400)

    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()

    # 入力の検証（サーバー側で必ず行う）
    if not title or not body:
        return JSONResponse({"error": "件名と本文は必須である"}, status_code=400)
    if len(title) > MAX_TITLE or len(body) > MAX_BODY:
        return JSONResponse({"error": "件名または本文が長すぎる"}, status_code=400)

    item = {
        "id": str(uuid.uuid4()),
        "title": title,
        "body": body,
        "status": "未対応",
        "createdAt": now_iso(),
    }
    get_inquiries().create_item(item)
    return JSONResponse(item, status_code=201)


@app.route(route="ai/config-check", methods=["GET"])
async def ai_config_check(req: Request) -> JSONResponse:
    """OpenRouter のキー（OPENROUTER_API_KEY）がアプリに届いているかを確かめる（コマ6）

    キーの値そのものは絶対に返さない。値から計算した「指紋」（SHA-256 の先頭8文字）を返す。
    """
    value = os.environ.get("OPENROUTER_API_KEY", "")
    if not value:
        status = "missing"
    elif value.startswith("@Microsoft.KeyVault("):
        status = "unresolved"
    elif value.startswith("sk-or-"):
        status = "ok"
    else:
        status = "unexpected-format"

    result = {"status": status}
    if status == "ok":
        result["fingerprint"] = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return JSONResponse(result)


# =====================================================================
# アシスタント（コマ7）
# =====================================================================
def sse(event):
    """イベント1つを、Server-Sent Events（SSE）の形式の文字列にする"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_response(events, status_code=200):
    return StreamingResponse(
        events,
        status_code=status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def parse_conversation_id(value):
    """会話の ID を確かめる（UUID の形でなければ None）"""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def load_history(conversation_id, limit):
    """この会話の履歴のうち、新しいほうから limit 件を、古い順に並べて返す"""
    query = (
        "SELECT TOP @limit c.role, c.content, c.createdAt FROM c "
        "WHERE c.conversationId = @id ORDER BY c.createdAt DESC"
    )
    items = list(
        get_conversations().query_items(
            query=query,
            parameters=[
                {"name": "@limit", "value": limit},
                {"name": "@id", "value": conversation_id},
            ],
            partition_key=conversation_id,  # 1つの会話は1つのパーティションに入っている
        )
    )
    items.reverse()
    return [{"role": i["role"], "content": i["content"]} for i in items]


def save_message(conversation_id, role, content, created_at):
    """メッセージ1件を Cosmos DB に保存する（1件＝1ドキュメント）"""
    get_conversations().create_item(
        {
            "id": str(uuid.uuid4()),
            "conversationId": conversation_id,
            "role": role,
            "content": content,
            "createdAt": created_at,
        }
    )


@app.route(route="conversations", methods=["GET"])
async def get_conversation(req: Request) -> JSONResponse:
    conversation_id = parse_conversation_id(req.query_params.get("id", ""))
    if conversation_id is None:
        return JSONResponse({"error": "会話の ID が正しくない"}, status_code=400)
    return JSONResponse(load_history(conversation_id, 100))


@app.route(route="chat", methods=["POST"])
async def chat(req: Request) -> StreamingResponse:
    # 入力の検証。エラーも、ストリームの途中のエラーと同じ「イベント」の形で返す
    try:
        data = await req.json()
    except ValueError:
        return sse_response(iter([sse({"type": "error", "message": "JSON の形式が正しくない"})]), 400)

    message = str(data.get("message", "")).strip()
    if not message or len(message) > MAX_MESSAGE:
        return sse_response(iter([sse({"type": "error", "message": "質問が空か、長すぎる"})]), 400)

    if data.get("conversationId"):
        conversation_id = parse_conversation_id(data["conversationId"])
        if conversation_id is None:
            return sse_response(iter([sse({"type": "error", "message": "会話の ID が正しくない"})]), 400)
    else:
        conversation_id = str(uuid.uuid4())  # 新しい会話

    config = llm.load_config()
    budget = int(os.environ.get("HISTORY_TOKEN_BUDGET", config["history_token_budget"]))
    user_created_at = now_iso()

    # TODO(コマ7 演習手順5-1): 送る messages を、会話の履歴から組み立てる。
    #   いまは今回の質問しか送っていないので、モデルはそれまでの会話を知らない。
    history = load_history(conversation_id, config["max_history_messages"])
    history.append({"role": "user", "content": message})
    messages = [{"role": "system", "content": config["system_prompt"]}] + trim_history(history, budget)


    def generate():
        # 最初のイベント: 会話の ID と、モデルに送ったメッセージの件数を知らせる
        yield sse({"type": "start", "conversationId": conversation_id, "sentMessages": len(messages)})

        try:
            # TODO(コマ7 演習手順5-2): いまは応答を最後まで受け取ってから、まとめて1回で送っている。
            #   受け取ったそばから、少しずつブラウザに送るように書き換える。
            parts = []
            for piece in llm.stream_chat(messages, config["max_output_tokens"]):
                parts.append(piece)
                yield sse({"type": "delta", "text": piece})
            reply = "".join(parts)

        except llm.LLMError as e:
            # 途中で失敗した。HTTP の状態（200）は送ってしまっているので、エラーもイベントで知らせる
            yield sse({"type": "error", "message": str(e)})
            return

        # TODO(コマ7 演習手順5-3): 応答が最後まで届いたら、質問と応答を Cosmos DB に保存する。
        save_message(conversation_id, "user", message, user_created_at)
        save_message(conversation_id, "assistant", reply, now_iso())

        yield sse({"type": "end"})

    return sse_response(generate())
