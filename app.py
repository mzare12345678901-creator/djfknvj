
import os
import json
import time
import base64
import sqlite3
import tempfile
import subprocess
from pathlib import Path

import requests
import imageio_ffmpeg
from fastapi import FastAPI, Request, HTTPException

# =========================================================
# CONFIGURATION
# =========================================================

BALE_TOKEN = os.getenv("BALE_BOT_TOKEN", "986540587:U_VZeXT1CLfQoF8SOBh0vNzOGQG6ywaA7Z8")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@viiideoai")
DESTINATION_CHANNEL = os.getenv("DESTINATION_CHANNEL", "")

AI_API_KEY = os.getenv("AIMLAPI_KEY", "bf751be0f71269ae1b94e02785a15708")
AI_BASE_URL = os.getenv(
    "AIMLAPI_BASE_URL", "https://api.aimlapi.com/v1"
).rstrip("/")
AI_MODEL = os.getenv(
    "AIMLAPI_MODEL", "google/gemini-2.5-flash"
)

MAX_VIDEO_MB = 35
MAX_VIDEO_SECONDS = 180
DB_PATH = os.getenv("DB_PATH", "bot.db")

app = FastAPI(title="Bale AI Video Rewriter")

BALE_API = (
    "https://tapi.bale.ai/bot" + BALE_TOKEN
    if BALE_TOKEN else ""
)

session = requests.Session()


# =========================================================
# DATABASE
# =========================================================

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_path TEXT NOT NULL,
                caption TEXT NOT NULL,
                source_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                message_key TEXT PRIMARY KEY
            )
        """)


init_db()


# =========================================================
# BALE BOT API
# =========================================================

def bale_call(method, data=None, timeout=60):
    if not BALE_API:
        raise RuntimeError("BALE_BOT_TOKEN تنظیم نشده است.")

    response = session.post(
        f"{BALE_API}/{method}",
        json=data or {},
        timeout=timeout,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok", False):
        raise RuntimeError(
            f"Bale API error: {result}"
        )

    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
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
            "text": text,
        })
    except Exception:
        pass


def send_video(chat_id, video_path, caption):
    # Upload the local video file to the destination channel.
    with open(video_path, "rb") as file:
        response = session.post(
            f"{BALE_API}/sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption[:1000],
            },
            files={
                "video": (
                    Path(video_path).name,
                    file,
                    "video/mp4",
                )
            },
            timeout=180,
        )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok", False):
        raise RuntimeError(
            f"Bale sendVideo error: {result}"
        )

    return result.get("result")


# =========================================================
# VIDEO DOWNLOAD
# =========================================================

def download_bale_video(file_id):
    # This assumes Bale's Bot API supports getFile and the
    # Telegram-compatible file download URL.
    result = bale_call(
        "getFile",
        {"file_id": file_id},
    )

    file_path = result.get("file_path")
    if not file_path:
        raise RuntimeError(
            "API مسیر فایل ویدئو را برنگرداند."
        )

    url = (
        "https://tapi.bale.ai/file/bot"
        + BALE_TOKEN + "/" + file_path
    )

    temp = tempfile.NamedTemporaryFile(
        suffix=".mp4", delete=False
    )
    temp.close()

    total = 0
    try:
        with session.get(
            url, stream=True, timeout=120
        ) as response:
            response.raise_for_status()

            with open(temp.name, "wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if not chunk:
                        continue

                    total += len(chunk)
                    if total > MAX_VIDEO_MB * 1024 * 1024:
                        raise ValueError(
                            "حجم ویدئو بیشتر از حد مجاز است."
                        )

                    output.write(chunk)

        return temp.name

    except Exception:
        Path(temp.name).unlink(missing_ok=True)
        raise


# =========================================================
# VIDEO ANALYSIS
# =========================================================

def extract_frames(video_path):
    """Extract up to four frames for visual analysis."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    frames = []

    with tempfile.TemporaryDirectory() as directory:
        output_pattern = str(
            Path(directory) / "frame-%02d.jpg"
        )

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-i", video_path,
            "-t", str(MAX_VIDEO_SECONDS),
            "-vf",
            "fps=1/8,scale=640:-2",
            "-frames:v", "4",
            "-q:v", "5",
            output_pattern,
            "-y",
        ]

        subprocess.run(
            command,
            check=True,
            timeout=90,
            capture_output=True,
        )

        for path in sorted(Path(directory).glob("frame-*.jpg")):
            encoded = base64.b64encode(
                path.read_bytes()
            ).decode("ascii")

            frames.append(
                "data:image/jpeg;base64," + encoded
            )

    if not frames:
        raise RuntimeError(
            "نتوانستم از ویدئو تصویر استخراج کنم."
        )

    return frames


def generate_caption(video_path):
    if not AI_API_KEY:
        raise RuntimeError("AIMLAPI_KEY تنظیم نشده است.")

    frames = extract_frames(video_path)

    content = [{
        "type": "text",
        "text": (
            "این تصاویر، چند فریم از یک ویدئو هستند. "
            "موضوع و اتفاقات قابل مشاهده را تحلیل کن. "
            "سپس یک کپشن فارسی جذاب برای انتشار در کانال "
            "بله بنویس. اطلاعاتی که در تصاویر مشخص نیست "
            "اختراع نکن. خروجی شامل عنوان کوتاه، توضیح "
            "مرتبط و حداکثر پنج هشتگ باشد. فقط کپشن را بده."
        ),
    }]

    for frame in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": frame},
        })

    response = session.post(
        AI_BASE_URL + "/chat/completions",
        headers={
            "Authorization": "Bearer " + AI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [{
                "role": "user",
                "content": content,
            }],
            "temperature": 0.7,
        },
        timeout=120,
    )

    response.raise_for_status()
    data = response.json()

    caption = (
        data["choices"][0]["message"]["content"]
    )

    if not isinstance(caption, str) or not caption.strip():
        raise RuntimeError("هوش مصنوعی کپشن تولید نکرد.")

    return caption.strip()[:3500]


# =========================================================
# PROCESS NEW CHANNEL VIDEO
# =========================================================

def find_video(message):
    for key in ("video", "animation"):
        item = message.get(key)
        if isinstance(item, dict) and item.get("file_id"):
            return item["file_id"]

    document = message.get("document") or {}
    if (
        document.get("file_id")
        and str(document.get("mime_type", "")).startswith("video/")
    ):
        return document["file_id"]

    return None


def process_channel_video(message):
    chat = message.get("chat", {})
    username = chat.get("username", "")
    source_id = str(chat.get("id", ""))

    expected = SOURCE_CHANNEL.lstrip("@")

    if username:
        if username.lower() != expected.lower():
            return
    elif SOURCE_CHANNEL.lstrip("@") != source_id:
        return

    file_id = find_video(message)
    if not file_id:
        return

    message_id = message.get("message_id")
    key = f"{source_id}:{message_id}"

    with sqlite3.connect(DB_PATH) as con:
        exists = con.execute(
            "SELECT 1 FROM processed WHERE message_key=?",
            (key,),
        ).fetchone()

        if exists:
            return

        con.execute(
            "INSERT INTO processed(message_key) VALUES(?)",
            (key,),
        )

    video_path = None

    try:
        video_path = download_bale_video(file_id)
        caption = generate_caption(video_path)

        with sqlite3.connect(DB_PATH) as con:
            cursor = con.execute(
                """
                INSERT INTO pending(
                    video_path, caption,
                    source_message_id, status, created_at
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    video_path,
                    caption,
                    message_id,
                    int(time.time()),
                ),
            )
            pending_id = cursor.lastrowid

        send_message(
            ADMIN_CHAT_ID,
            "🎬 ویدئوی جدید تحلیل شد.\n\n"
            + "📝 کپشن پیشنهادی:\n\n"
            + caption
            + "\n\nبرای انتشار، تأیید کن.",
            keyboard=[[
                {
                    "text": "✅ تأیید و انتشار",
                    "callback_data": f"approve:{pending_id}",
                },
                {
                    "text": "❌ رد",
                    "callback_data": f"reject:{pending_id}",
                },
            ]],
        )

    except Exception as exc:
        if video_path:
            Path(video_path).unlink(missing_ok=True)

        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "DELETE FROM processed WHERE message_key=?",
                (key,),
            )

        send_message(
            ADMIN_CHAT_ID,
            "⚠️ پردازش ویدئو ناموفق بود:\n"
            + str(exc)[:700],
        )


# =========================================================
# APPROVAL / REJECTION
# =========================================================

def handle_callback(callback):
    sender = callback.get("from", {})
    if str(sender.get("id")) != str(ADMIN_CHAT_ID):
        answer_callback(
            callback.get("id"), "اجازه دسترسی نداری."
        )
        return

    data = callback.get("data", "")
    if ":" not in data:
        return

    action, raw_id = data.split(":", 1)
    if action not in ("approve", "reject"):
        return

    try:
        pending_id = int(raw_id)
    except ValueError:
        return

    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """
            SELECT video_path, caption, status
            FROM pending WHERE id=?
            """,
            (pending_id,),
        ).fetchone()

    if not row:
        answer_callback(callback.get("id"), "پست پیدا نشد.")
        return

    video_path, caption, status = row

    if status != "pending":
        answer_callback(callback.get("id"), "این مورد قبلاً بررسی شده.")
        return

    if action == "reject":
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "UPDATE pending SET status='rejected' WHERE id=?",
                (pending_id,),
            )

        Path(video_path).unlink(missing_ok=True)
        answer_callback(callback.get("id"), "پست رد شد.")
        return

    if not DESTINATION_CHANNEL:
        answer_callback(
            callback.get("id"),
            "DESTINATION_CHANNEL تنظیم نشده است.",
        )
        return

    try:
        send_video(
            DESTINATION_CHANNEL,
            video_path,
            caption,
        )

        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "UPDATE pending SET status='published' WHERE id=?",
                (pending_id,),
            )

        Path(video_path).unlink(missing_ok=True)
        answer_callback(callback.get("id"), "منتشر شد.")

    except Exception as exc:
        answer_callback(
            callback.get("id"),
            "انتشار ناموفق بود؛ گزارش خطا را بررسی کن.",
        )
        send_message(
            ADMIN_CHAT_ID,
            "خطای انتشار:\n" + str(exc)[:700],
        )


# =========================================================
# WEBHOOK
# =========================================================

@app.get("/")
def health():
    return {
        "status": "running",
        "service": "Bale AI Video Rewriter",
    }


@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()

    try:
        callback = update.get("callback_query")
        if callback:
            handle_callback(callback)
            return {"ok": True}

        message = (
            update.get("channel_post")
            or update.get("message")
        )

        if not message:
            return {"ok": True}

        chat = message.get("chat", {})
        text = message.get("text", "")

        # Respond to commands sent privately by the admin.
        if (
            str(chat.get("id")) == str(ADMIN_CHAT_ID)
            and text.startswith("/start")
        ):
            send_message(
                ADMIN_CHAT_ID,
                "سلام! ربات تحلیل ویدئو فعال است.\n"
                "برای پایش خودکار، ربات باید دسترسی "
                "لازم به کانال منبع داشته باشد.",
            )
            return {"ok": True}

        if "channel_post" in update:
            process_channel_video(message)

        return {"ok": True}

    except Exception as exc:
        # Avoid repeated webhook retries for the same bad update.
        print("Webhook error:", repr(exc))
        return {"ok": True}