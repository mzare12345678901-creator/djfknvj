
import os
import json
import time
import base64
import sqlite3
import tempfile
import subprocess
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import requests
import imageio_ffmpeg
from fastapi import FastAPI, Request, HTTPException
from starlette.concurrency import run_in_threadpool

# =========================================================
# CONFIGURATION
# =========================================================

BALE_TOKEN = os.getenv("BALE_BOT_TOKEN", "986540587U_VZeXT1CLfQoF8SOBh0vNzOGQG6ywaA7Z8").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

SOURCE_CHANNEL = os.getenv(
    "SOURCE_CHANNEL", "@viiideoai"
).strip()

DESTINATION_CHANNEL = os.getenv(
    "DESTINATION_CHANNEL",
    os.getenv("DEST_CHANNEL", "")
).strip()

AI_API_KEY = os.getenv("AIMLAPI_KEY", "bf751be0f71269ae1b94e02785a15708").strip()

AI_BASE_URL = os.getenv(
    "AIMLAPI_BASE_URL",
    "https://api.aimlapi.com/v1"
).rstrip("/")

AI_MODEL = os.getenv(
    "AIMLAPI_MODEL",
    os.getenv("AI_MODEL", "google/gemini-2.5-flash")
).strip()

MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "35"))
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "180"))
POLL_TIMEOUT = 30

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = os.getenv(
    "DB_PATH", str(DATA_DIR / "bot.db")
)

BALE_API = (
    f"https://tapi.bale.ai/bot{BALE_TOKEN}"
    if BALE_TOKEN else ""
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("bale-ai-bot")

session = requests.Session()


# =========================================================
# DATABASE
# =========================================================

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_path TEXT NOT NULL,
                caption TEXT NOT NULL,
                source_key TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                admin_message_id INTEGER
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                message_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)


init_db()


# =========================================================
# VALIDATION
# =========================================================

def validate_config():
    missing = []

    if not BALE_TOKEN:
        missing.append("BALE_BOT_TOKEN")
    if not ADMIN_CHAT_ID:
        missing.append("ADMIN_CHAT_ID")
    if not AI_API_KEY:
        missing.append("AIMLAPI_KEY")

    if missing:
        raise RuntimeError(
            "متغیرهای محیطی تنظیم نشده‌اند: "
            + ", ".join(missing)
        )


# =========================================================
# BALE API
# =========================================================

def bale_call(method, data=None, timeout=60):
    if not BALE_API:
        raise RuntimeError("BALE_BOT_TOKEN تنظیم نشده است.")

    response = session.post(
        f"{BALE_API}/{method}",
        json=data or {},
        timeout=timeout
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Bale API {method}: "
            + str(result.get("description", result))[:500]
        )

    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text[:4000]
    }

    if keyboard:
        payload["reply_markup"] = {
            "inline_keyboard": keyboard
        }

    return bale_call("sendMessage", payload)


def answer_callback(callback_id, text):
    try:
        bale_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:180]
        }, timeout=15)
    except Exception:
        log.exception("Callback response failed")


def send_video(chat_id, video_path, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "caption": caption[:1000]
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": keyboard},
            ensure_ascii=False
        )

    with open(video_path, "rb") as video:
        response = session.post(
            f"{BALE_API}/sendVideo",
            data=payload,
            files={
                "video": (
                    Path(video_path).name,
                    video,
                    "video/mp4"
                )
            },
            timeout=180
        )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "ارسال ویدئو ناموفق بود: "
            + str(result.get("description", result))[:500]
        )

    return result.get("result")


# =========================================================
# DOWNLOAD VIDEO
# =========================================================

def download_bale_video(file_id):
    result = bale_call(
        "getFile",
        {"file_id": file_id}
    )

    file_path = (result or {}).get("file_path")

    if not file_path:
        raise RuntimeError(
            "مسیر فایل از API بله دریافت نشد."
        )

    url = (
        f"https://tapi.bale.ai/file/bot"
        f"{BALE_TOKEN}/{file_path}"
    )

    fd, local_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    total = 0

    try:
        with session.get(
            url,
            stream=True,
            timeout=120
        ) as response:
            response.raise_for_status()

            with open(local_path, "wb") as output:
                for chunk in response.iter_content(
                    chunk_size=256 * 1024
                ):
                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_VIDEO_MB * 1024 * 1024:
                        raise ValueError(
                            f"حجم ویدئو بیشتر از {MAX_VIDEO_MB} مگابایت است."
                        )

                    output.write(chunk)

        if total == 0:
            raise RuntimeError("فایل ویدئو خالی است.")

        return local_path

    except Exception:
        Path(local_path).unlink(missing_ok=True)
        raise


# =========================================================
# VIDEO FRAMES
# =========================================================

def extract_frames(video_path):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    frames = []

    with tempfile.TemporaryDirectory() as directory:
        pattern = str(Path(directory) / "frame-%02d.jpg")

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-i", video_path,
            "-t", str(MAX_VIDEO_SECONDS),
            "-vf", "fps=1/8,scale=640:-2",
            "-frames:v", "4",
            "-q:v", "5",
            "-y",
            pattern
        ]

        subprocess.run(
            command,
            check=True,
            timeout=90,
            capture_output=True
        )

        for frame_path in sorted(
            Path(directory).glob("frame-*.jpg")
        ):
            encoded = base64.b64encode(
                frame_path.read_bytes()
            ).decode("ascii")

            frames.append(
                "data:image/jpeg;base64," + encoded
            )

    if not frames:
        raise RuntimeError(
            "از ویدئو فریمی استخراج نشد؛ "
            "ممکن است فایل خراب یا نامعتبر باشد."
        )

    return frames


# =========================================================
# AI CAPTION GENERATION
# =========================================================

def generate_caption(video_path):
    if not AI_API_KEY:
        raise RuntimeError("AIMLAPI_KEY تنظیم نشده است.")

    frames = extract_frames(video_path)

    content = [{
        "type": "text",
        "text": """
تو نویسنده حرفه‌ای کپشن فارسی برای شبکه‌های اجتماعی هستی.

تصاویر، فریم‌هایی از یک ویدئو هستند.

وظایف:
1. موضوع قابل مشاهده را دقیق تشخیص بده.
2. یک عنوان کوتاه و جذاب بنویس.
3. توضیح فارسی روان و مرتبط بنویس.
4. حداکثر پنج هشتگ مرتبط اضافه کن.
5. اطلاعات، نام‌ها یا اتفاقات نامشخص را جعل نکن.
6. از ادعاهای اغراق‌آمیز و توضیحات بی‌ربط پرهیز کن.
7. فقط کپشن نهایی را برگردان؛ مقدمه و تحلیل داخلی ننویس.
"""
    }]

    for frame in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": frame}
        })

    response = session.post(
        AI_BASE_URL + "/chat/completions",
        headers={
            "Authorization": "Bearer " + AI_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "model": AI_MODEL,
            "messages": [{
                "role": "user",
                "content": content
            }],
            "temperature": 0.65,
            "max_tokens": 700
        },
        timeout=150
    )

    if not response.ok:
        raise RuntimeError(
            f"خطای سرویس هوش مصنوعی "
            f"({response.status_code}): "
            + response.text[:500]
        )

    data = response.json()

    try:
        caption = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            "پاسخ هوش مصنوعی قالب مورد انتظار را ندارد."
        )

    if not isinstance(caption, str) or not caption.strip():
        raise RuntimeError("کپشن خالی دریافت شد.")

    return caption.strip()[:3500]


# =========================================================
# FIND VIDEO
# =========================================================

def find_video(message):
    for field in ("video", "animation"):
        item = message.get(field)

        if isinstance(item, dict) and item.get("file_id"):
            return item["file_id"]

    document = message.get("document") or {}

    if (
        document.get("file_id")
        and str(document.get("mime_type", "")).startswith("video/")
    ):
        return document["file_id"]

    return None


def is_source_channel(chat):
    expected = SOURCE_CHANNEL.lstrip("@").lower()
    username = str(chat.get("username", "")).lstrip("@").lower()
    chat_id = str(chat.get("id", ""))

    if username:
        return username == expected

    return SOURCE_CHANNEL == chat_id


# =========================================================
# PROCESS CHANNEL VIDEO
# =========================================================

def process_channel_video(message):
    chat = message.get("chat") or {}

    if not is_source_channel(chat):
        return

    file_id = find_video(message)

    if not file_id:
        return

    source_key = (
        f"{chat.get('id')}:{message.get('message_id')}"
    )

    # Reserve the message key to avoid duplicate processing.
    with db() as con:
        try:
            con.execute(
                """
                INSERT INTO processed(message_key, status, updated_at)
                VALUES (?, 'processing', ?)
                """,
                (source_key, int(time.time()))
            )
        except sqlite3.IntegrityError:
            log.info("Duplicate source post ignored: %s", source_key)
            return

    video_path = None

    try:
        log.info("Processing source post %s", source_key)

        video_path = download_bale_video(file_id)
        caption = generate_caption(video_path)

        with db() as con:
            cursor = con.execute(
                """
                INSERT INTO pending(
                    video_path, caption, source_key,
                    status, created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    video_path,
                    caption,
                    source_key,
                    int(time.time())
                )
            )
            pending_id = cursor.lastrowid

            con.execute(
                """
                UPDATE processed
                SET status='pending', updated_at=?
                WHERE message_key=?
                """,
                (int(time.time()), source_key)
            )

        keyboard = [[
            {
                "text": "✅ تأیید و انتشار",
                "callback_data": f"approve:{pending_id}"
            },
            {
                "text": "❌ رد و حذف",
                "callback_data": f"reject:{pending_id}"
            }
        ]]

        # Send the actual video preview to the administrator.
        sent = send_video(
            ADMIN_CHAT_ID,
            video_path,
            "🎬 پیش‌نمایش ویدئوی جدید\n\n"
            "📝 کپشن پیشنهادی:\n\n"
            + caption,
            keyboard=keyboard
        )

        with db() as con:
            con.execute(
                """
                UPDATE pending
                SET admin_message_id=?
                WHERE id=?
                """,
                (
                    (sent or {}).get("message_id"),
                    pending_id
                )
            )

        log.info("Pending video created: %s", pending_id)

    except Exception as exc:
        log.exception("Video processing failed")

        with db() as con:
            con.execute(
                """
                UPDATE processed
                SET status='error', updated_at=?
                WHERE message_key=?
                """,
                (int(time.time()), source_key)
            )

        try:
            send_message(
                ADMIN_CHAT_ID,
                "⚠️ پردازش ویدئو ناموفق بود.\n"
                + str(exc)[:700]
            )
        except Exception:
            log.exception("Could not notify admin")

        if video_path:
            Path(video_path).unlink(missing_ok=True)


# =========================================================
# APPROVAL / REJECTION
# =========================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    sender_id = str(
        (callback.get("from") or {}).get("id", "")
    )

    callback_message = callback.get("message") or {}
    callback_chat_id = str(
        (callback_message.get("chat") or {}).get("id", "")
    )

    # Accept only the configured administrator chat/user.
    if (
        sender_id != ADMIN_CHAT_ID
        and callback_chat_id != ADMIN_CHAT_ID
    ):
        answer_callback(callback_id, "اجازه دسترسی نداری.")
        return

    data = str(callback.get("data", ""))

    if ":" not in data:
        answer_callback(callback_id, "درخواست نامعتبر است.")
        return

    action, raw_id = data.split(":", 1)

    if action not in ("approve", "reject"):
        answer_callback(callback_id, "عملیات نامعتبر است.")
        return

    try:
        pending_id = int(raw_id)
    except ValueError:
        answer_callback(callback_id, "شناسه نامعتبر است.")
        return

    with db() as con:
        row = con.execute(
            "SELECT * FROM pending WHERE id=?",
            (pending_id,)
        ).fetchone()

    if not row:
        answer_callback(callback_id, "ویدئو پیدا نشد.")
        return

    if row["status"] != "pending":
        answer_callback(callback_id, "این مورد قبلاً بررسی شده است.")
        return

    video_path = row["video_path"]

    if action == "reject":
        with db() as con:
            con.execute(
                """
                UPDATE pending SET status='rejected'
                WHERE id=? AND status='pending'
                """,
                (pending_id,)
            )

        Path(video_path).unlink(missing_ok=True)
        answer_callback(callback_id, "ویدئو رد شد.")
        return

    if not DESTINATION_CHANNEL:
        answer_callback(
            callback_id,
            "کانال مقصد تنظیم نشده است."
        )
        return

    if not Path(video_path).is_file():
        answer_callback(
            callback_id,
            "فایل ویدئو دیگر موجود نیست؛ دوباره بررسی کن."
        )
        return

    # Atomically reserve publication to avoid double-click duplicates.
    with db() as con:
        cursor = con.execute(
            """
            UPDATE pending
            SET status='publishing'
            WHERE id=? AND status='pending'
            """,
            (pending_id,)
        )

        if cursor.rowcount != 1:
            answer_callback(
                callback_id,
                "این ویدئو در حال بررسی است."
            )
            return

    try:
        send_video(
            DESTINATION_CHANNEL,
            video_path,
            row["caption"]
        )

        with db() as con:
            con.execute(
                """
                UPDATE pending SET status='published'
                WHERE id=?
                """,
                (pending_id,)
            )
            con.execute(
                """
                UPDATE processed
                SET status='published', updated_at=?
                WHERE message_key=?
                """,
                (int(time.time()), row["source_key"])
            )

        Path(video_path).unlink(missing_ok=True)
        answer_callback(callback_id, "ویدئو منتشر شد.")

    except Exception as exc:
        log.exception("Publishing failed")

        # A network failure may occur after the destination accepted
        # the video. Check the destination before retrying manually.
        with db() as con:
            con.execute(
                """
                UPDATE pending SET status='publish_error'
                WHERE id=?
                """,
                (pending_id,)
            )

        answer_callback(
            callback_id,
            "انتشار ناموفق یا نامشخص بود؛ دوباره‌کاری نکن."
        )

        try:
            send_message(
                ADMIN_CHAT_ID,
                "⚠️ خطای انتشار:\n"
                + str(exc)[:700]
                + "\nوضعیت کانال مقصد را هم بررسی کن."
            )
        except Exception:
            log.exception("Could not notify administrator")


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app):
    try:
        validate_config()
        log.info("Configuration validated.")
    except Exception:
        log.exception("Configuration is incomplete.")

    yield


app = FastAPI(
    title="Bale AI Video Bot",
    version="2.0.0",
    lifespan=lifespan
)


# =========================================================
# HEALTH
# =========================================================

@app.get("/")
@app.get("/healthz")
def health():
    return {
        "status": "running",
        "service": "Bale AI Video Bot",
        "version": "2.0.0"
    }


# =========================================================
# WEBHOOK
# =========================================================

@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON"
        )

    try:
        callback = update.get("callback_query")

        if callback:
            await run_in_threadpool(
                handle_callback, callback
            )
            return {"ok": True}

        message = (
            update.get("channel_post")
            or update.get("message")
        )

        if not message:
            return {"ok": True}

        chat = message.get("chat") or {}
        text = str(message.get("text", "")).strip()

        # Private admin commands
        if (
            str(chat.get("id")) == ADMIN_CHAT_ID
            and text in ("/start", "/status")
        ):
            send_message(
                ADMIN_CHAT_ID,
                "🤖 ربات هوشمند ویدئو فعال است.\n\n"
                "ویدئوهای کانال منبع پس از تحلیل "
                "برای تأیید دستی ارسال می‌شوند."
            )
            return {"ok": True}

        if "channel_post" in update:
            await run_in_threadpool(
                process_channel_video, message
            )

        return {"ok": True}

    except Exception:
        log.exception("Webhook handler failed")
        return {"ok": True}
