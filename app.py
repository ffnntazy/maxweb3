import logging
import os
from typing import Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # например: https://your-service.onrender.com

if not TELEGRAM_BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN не задан – телеграм-бот работать не будет.")
if not BASE_URL:
    logger.warning("BASE_URL не задан – webapp URL будет некорректным.")


app = FastAPI(title="MAX Telegram Bridge + Bot")

# Хранилище сессий MAX по telegram_user_id (память процесса)
SESSIONS: Dict[str, httpx.Cookies] = {}

MAX_BASE_URL = "https://web.max.ru"

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ========= Telegram bot (webhook) =========

tg_app: Application | None = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — отправляем кнопку, которая открывает web-app (miniapp) с нашим мостом.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    user = update.effective_user
    logger.info(
        "User %s (%s) used /start in chat %s",
        user.id if user else None,
        user.username if user else None,
        chat_id,
    )

    if not BASE_URL:
        await update.message.reply_text("Сервис ещё не настроен. Обратись к администратору.")
        return

    webapp_url = f"{BASE_URL}/"
    web_app_info = WebAppInfo(url=webapp_url)

    keyboard = [
        [
            KeyboardButton(
                text="Открыть MAX",
                web_app=web_app_info,
            )
        ]
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    await update.message.reply_text(
        "Привет!\n\n"
        "Нажми кнопку ниже, чтобы открыть MAX в мини-приложении Telegram.",
        reply_markup=reply_markup,
    )


@app.on_event("startup")
async def on_startup() -> None:
    """
    Поднимаем Telegram-бота в режиме webhook внутри того же процесса.
    """
    global tg_app

    if not TELEGRAM_BOT_TOKEN or not BASE_URL:
        logger.warning("TELEGRAM_BOT_TOKEN или BASE_URL не заданы, бот не будет инициализирован.")
        return

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))

    webhook_url = f"{BASE_URL}/telegram/webhook"
    logger.info("Setting Telegram webhook to %s", webhook_url)

    # Настраиваем webhook и запускаем обработчик обновлений.
    await tg_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    await tg_app.initialize()
    await tg_app.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """
    Корректно останавливаем Telegram-бота при завершении приложения.
    """
    global tg_app
    if tg_app is not None:
        await tg_app.stop()
        await tg_app.shutdown()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """
    Точка входа для Telegram webhook.
    """
    global tg_app
    if tg_app is None:
        return Response(status_code=503, content="Bot not initialized")

    # Тело запроса — JSON-объект от Telegram, его нужно распарсить в dict.
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.update_queue.put(update)
    return Response(status_code=204)


# ========= WebApp + MAX proxy =========

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """
    Стартовая страница для Telegram WebApp.
    Получает telegram_user_id через JS API Telegram и редиректит на /app?tid=...
    """
    html = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>MAX Bridge</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      width: 100%;
      background: #050509;
      color: #fff;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .center {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100%;
      text-align: center;
      flex-direction: column;
      gap: 12px;
    }
    .btn {
      padding: 10px 18px;
      border-radius: 999px;
      border: none;
      background: #5865F2;
      color: #fff;
      font-size: 15px;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="center">
    <div>Подготовка моста к MAX…</div>
    <button class="btn" id="openBtn" disabled>Открыть MAX</button>
  </div>

  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script>
    const tg = window.Telegram.WebApp;
    tg.expand();

    const btn = document.getElementById('openBtn');

    function init() {
      const unsafe = tg.initDataUnsafe || {};
      const user = unsafe.user;

      let tid = null;

      if (user && user.id) {
        tid = String(user.id);
      } else if (tg.initData && tg.initData.length > 0) {
        // Фолбэк: используем всю строку initData как идентификатор.
        // Это не идеально, но позволит работать даже если user пустой.
        tid = tg.initData;
      }

      if (!tid) {
        btn.textContent = 'Telegram не передал ID. Открой миниаппу из диалога с ботом.';
        return;
      }

      fetch('/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telegram_user_id: tid })
      }).finally(() => {
        btn.disabled = false;
        btn.onclick = () => {
          window.location.href = '/app?tid=' + encodeURIComponent(tid);
        };
      });
    }

    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.post("/register")
async def register(request: Request) -> Response:
    """
    Регистрирует telegram_user_id.
    """
    data = await request.json()
    telegram_user_id = str(data.get("telegram_user_id", ""))
    if not telegram_user_id:
        return Response(status_code=400, content="telegram_user_id is required")

    if telegram_user_id not in SESSIONS:
        SESSIONS[telegram_user_id] = httpx.Cookies()
        logger.info("Registered new Telegram user id=%s for MAX session", telegram_user_id)
    return Response(status_code=204)


@app.get("/app", response_class=HTMLResponse)
async def app_view(tid: str) -> HTMLResponse:
    """
    Страница с iframe, который показывает MAX через наш прокси.
    """
    if tid not in SESSIONS:
        SESSIONS[tid] = httpx.Cookies()

    html = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>MAX Web</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      height: 100%;
      width: 100%;
      background: #000;
    }}
    iframe {{
      border: none;
      width: 100%;
      height: 100%;
    }}
  </style>
</head>
<body>
  <iframe src="/max/root?tid={tid}" allow="clipboard-read; clipboard-write;"></iframe>
</body>
</html>
    """
    return HTMLResponse(content=html)


async def _proxy_to_max(
    method: str,
    path: str,
    tid: str,
    request: Request,
) -> Response:
    """
    Прокси-запрос к MAX с десктопным User-Agent
    и сессией, привязанной к telegram_user_id.
    """
    if tid not in SESSIONS:
        SESSIONS[tid] = httpx.Cookies()

    target_path = path
    if target_path == "root":
        target_path = ""

    url = f"{MAX_BASE_URL}/{target_path}"

    query_params = dict(request.query_params)
    query_params.pop("tid", None)

    body = await request.body()

    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": request.headers.get("accept", "*/*"),
        "Accept-Language": request.headers.get(
            "accept-language", "ru-RU,ru;q=0.9,en;q=0.8"
        ),
        "Referer": MAX_BASE_URL + "/",
    }

    async with httpx.AsyncClient(
        headers=headers,
        cookies=SESSIONS[tid],
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        resp = await client.request(
            method=method,
            url=url,
            content=body if method in ("POST", "PUT", "PATCH") else None,
            params=query_params,
        )

        SESSIONS[tid] = client.cookies

    content_type = resp.headers.get("content-type", "text/html; charset=utf-8")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
    )


@app.api_route(
    "/max/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def max_proxy(path: str, tid: str, request: Request) -> Response:
    """
    Универсальный прокси для MAX.
    """
    method = request.method.upper()
    return await _proxy_to_max(method=method, path=path, tid=tid, request=request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

