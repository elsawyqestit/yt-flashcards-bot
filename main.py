import os
import re
import json
import html
import asyncio
from datetime import timedelta
from urllib.parse import urlparse, parse_qs

import aiohttp
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)

try:
    from youtubetranscriptapi import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    HAS_YT_TRANSCRIPT = True
except Exception:
    HAS_YT_TRANSCRIPT = False

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YT_API_KEY = os.getenv("YT_API_KEY")

DB_FILE = os.getenv("DB_FILE", "db.json")
STATE = {"users": {}}
STATE_LOCK = asyncio.Lock()


def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "users" in data:
                    STATE["users"] = data["users"]
        except Exception:
            pass


def save_db():
    tmp = {"users": STATE["users"]}
    try:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    except Exception:
        pass
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(tmp, f, ensure_ascii=False, indent=2)


def extract_video_id(url: str) -> str | None:
    try:
        u = urlparse(url)
        if u.netloc in ["youtu.be"]:
            vid = u.path.strip("/")
            return vid if vid else None
        if "youtube.com" in u.netloc:
            if u.path == "/watch":
                q = parse_qs(u.query)
                return q.get("v", [None])[0]
            parts = [p for p in u.path.split("/") if p]
            if parts and parts[0] in ["shorts", "live", "embed"] and len(parts) > 1:
                return parts[1]
    except Exception:
        return None
    return None


def iso8601_duration_to_seconds(d: str) -> int:
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    m = re.match(pattern, d)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    m_ = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + m_ * 60 + s


def fmt_time(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


async def get_video_meta(video_id: str) -> dict | None:
    if not YT_API_KEY:
        return None
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "id": video_id,
        "part": "snippet,contentDetails",
        "key": YT_API_KEY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=20) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                return None
            item = items[0]
            title = item["snippet"]["title"]
            dur_iso = item["contentDetails"]["duration"]
            duration = iso8601_duration_to_seconds(dur_iso)
            return {"title": title, "duration": duration}


async def get_transcript(video_id: str, lang_pref=("ar", "en")) -> list[dict] | None:
    if not HAS_YT_TRANSCRIPT:
        return None
    try:
        for lang in lang_pref:
            try:
                tr = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=[lang])
                if tr:
                    return tr
            except (NoTranscriptFound, TranscriptsDisabled):
                continue
        try:
            list_obj = await asyncio.to_thread(YouTubeTranscriptApi.list_transcripts, video_id)
            for t in list_obj:
                if t.is_translatable:
                    tr = await asyncio.to_thread(t.translate, lang_pref[0])
                    return tr.fetch()
        except Exception:
            pass
    except Exception:
        return None
    return None


def slice_transcript_text(transcript: list[dict] | None, start: int, end: int, max_chars: int = 700) -> str:
    if not transcript:
        return ""
    chunks = []
    for item in transcript:
        s = int(item.get("start", 0))
        d = int(item.get("duration", 0))
        e = s + d
        if e > start and s < end:
            chunks.append(item.get("text", "").strip())
    text = " ".join([c for c in chunks if c]).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def build_segment_link(video_id: str, start_sec: int) -> str:
    return f"https://youtu.be/{video_id}?t={start_sec}"


def default_question(title: str, start: int, end: int) -> str:
    return f"ما أهم الأفكار في جزء {fmt_time(start)} إلى {fmt_time(end)} من «{title}»؟ حاول تلخّصه في 3 نقاط."


def build_flashcard_html(title: str, video_id: str, start: int, end: int, summary_text: str) -> str:
    link = build_segment_link(video_id, start)
    title_html = html.escape(title)
    summary_html = html.escape(summary_text) if summary_text else "شاهد المقطع ثم اكتب ملحوظاتك."
    q = html.escape(default_question(title, start, end))
    msg = (
        f"🎬 <b>{title_html}</b>\n"
        f"⏱️ المقطع: {fmt_time(start)} → {fmt_time(end)}\n"
        f"🔗 <a href=\"{link}\">افتح المقطع من هنا</a>\n\n"
        f"❓ <b>سؤال:</b>\n{q}\n\n"
        f"✅ <b>ملخص/إجابة:</b> <span class=\"tg-spoiler\">{summary_html}</span>\n"
        f"تقدر تدوس على الإجابة لإظهارها"
    )
    return msg


def ensure_user(user_id: int):
    uid = str(user_id)
    if uid not in STATE["users"]:
        STATE["users"][uid] = {
            "video_id": None,
            "video_url": None,
            "title": None,
            "duration": 0,
            "interval_sec": 300,
            "chunk_sec": 90,
            "current_start_sec": 0,
            "transcript": None,
            "active": False,
            "job_name": None,
        }


def parse_duration_str(s: str) -> int | None:
    s = s.strip().lower()
    if not s:
        return None
    if re.match(r"^\d{1,2}:\d{1,2}:\d{1,2}$", s) or re.match(r"^\d{1,2}:\d{1,2}$", s):
        parts = [int(x) for x in s.split(":")]
        if len(parts) == 3:
            h, m, sec = parts
        else:
            h, m, sec = 0, parts[0], parts[1]
        return h * 3600 + m * 60 + sec
    pat = r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$"
    m = re.match(pat, s)
    if m and any(m.groups()):
        h = int(m.group(1) or 0)
        mn = int(m.group(2) or 0)
        sec = int(m.group(3) or 0)
        return h * 3600 + mn * 60 + sec
    if s.isdigit():
        return int(s)
    return None


async def send_next_card(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = int(job.data["user_id"])
    uid = str(user_id)

    async with STATE_LOCK:
        session = STATE["users"].get(uid)
        if not session or not session.get("active"):
            await context.bot.send_message(chat_id=user_id, text="الجلسة مش مفعّلة. استخدم /startsession بعد ضبط الفيديو.")
            try:
                job.schedule_removal()
            except Exception:
                pass
            return

        start = session["current_start_sec"]
        chunk = session["chunk_sec"]
        end = min(start + chunk, session["duration"])
        if not session["video_id"] or session["duration"] == 0:
            await context.bot.send_message(chat_id=user_id, text="محتاج تضيف فيديو الأول بـ /add <رابط_يوتيوب>")
            job.schedule_removal()
            return

        if start >= session["duration"]:
            session["active"] = False
            save_db()
            await context.bot.send_message(chat_id=user_id, text="خلصنا كل مقاطع الفيديو 👏. استخدم /startsession للمراجعة من الأول.")
            try:
                job.schedule_removal()
            except Exception:
                pass
            return

        summary = slice_transcript_text(session.get("transcript"), start, end)
        msg_html = build_flashcard_html(session["title"], session["video_id"], start, end, summary)

        kb = [
            [InlineKeyboardButton("🔗 فتح المقطع", url=build_segment_link(session["video_id"], start))],
            [
                InlineKeyboardButton("⏭️ تخطي", callback_data="skip"),
                InlineKeyboardButton("⏹️ إيقاف", callback_data="stop"),
            ],
        ]
        await context.bot.send_message(
            chat_id=user_id,
            text=msg_html,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
            reply_markup=InlineKeyboardMarkup(kb),
        )

        session["current_start_sec"] = end
        save_db()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        save_db()
    txt = (
        "أهلاً بيك! 👋\n"
        "أنا هاساعدك تذاكر فيديوهات يوتيوب بطريقة فلاش كاردز.\n\n"
        "الاستخدام:\n"
        "• ابعت رابط الفيديو أو استخدم: /add <رابط_يوتيوب>\n"
        "• ظبّط الفترة بين الكروت: /setinterval 5m (أو 300s، 00:05:00)\n"
        "• ظبّط مدة المقطع: /setchunk 90s\n"
        "• ابدأ الجلسة: /startsession\n"
        "• إيقاف: /stop — حالة: /status — مساعدة: /help"
    )
    await update.message.reply_text(txt)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "أوامر مفيدة:\n"
        "/add <link> — إضافة فيديو يوتيوب\n"
        "/setinterval <مدة> — مثال: 5m أو 300s أو 00:05:00\n"
        "/setchunk <مدة> — مثال: 90s أو 1m30s\n"
        "/startsession — بدء الإشعارات بالكروت\n"
        "/stop — إيقاف الجلسة\n"
        "/status — عرض الحالة الحالية\n\n"
        "ملاحظة: البوت بيبعت لينك يبدأ من الدقيقة المطلوبة وملخص/إجابة بنمط Spoiler بدون تنزيل الفيديو."
    )
    await update.message.reply_text(txt)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        url = context.args[0]
    else:
        await update.message.reply_text("ابعت كده: /add <رابط_يوتيوب>")
        return

    vid = extract_video_id(url)
    if not vid:
        await update.message.reply_text("مش عارف أستخرج معرف الفيديو. اتأكد من الرابط.")
        return

    meta = await get_video_meta(vid)
    if not meta:
        await update.message.reply_text("معرفتش أجيب بيانات الفيديو. اتأكد من صلاحية YouTube API Key.")
        return

    transcript = None
    if HAS_YT_TRANSCRIPT:
        transcript = await get_transcript(vid)

    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        uid = str(update.effective_user.id)
        STATE["users"][uid].update({
            "video_id": vid,
            "video_url": url,
            "title": meta["title"],
            "duration": meta["duration"],
            "current_start_sec": 0,
            "transcript": transcript,
        })
        save_db()

    dur_txt = fmt_time(meta["duration"])
    await update.message.reply_text(
        f"تمت إضافة الفيديو:\n"
        f"العنوان: {meta['title']}\n"
        f"المدة: {dur_txt}\n"
        f"{'✅ وجدنا تفريغ للنص' if transcript else 'ℹ️ مافيش تفريغ متاح، هنستخدم سؤال عام'}\n"
        f"ابدأ الجلسة بـ /startsession"
    )


async def message_with_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "youtu" in text:
        update.message.text = f"/add {text.strip()}"
        return await add_cmd(update, context)


async def setinterval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("اكتب: /setinterval 5m مثلاً")
        return
    secs = parse_duration_str("".join(context.args))
    if not secs or secs < 30:
        await update.message.reply_text("الحد الأدنى 30 ثانية. جرّب قيمة صالحة.")
        return
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        uid = str(update.effective_user.id)
        STATE["users"][uid]["interval_sec"] = secs
        save_db()
    await update.message.reply_text(f"تم التعيين: كل {fmt_time(secs)}")


async def setchunk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("اكتب: /setchunk 90s مثلاً")
        return
    secs = parse_duration_str("".join(context.args))
    if not secs or secs < 30 or secs > 600:
        await update.message.reply_text("اختار مدة بين 30 ثانية و10 دقايق.")
        return
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        uid = str(update.effective_user.id)
        STATE["users"][uid]["chunk_sec"] = secs
        save_db()
    await update.message.reply_text(f"مدة المقطع: {fmt_time(secs)}")


async def startsession_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        session = STATE["users"][uid]
        if not session.get("video_id"):
            await update.message.reply_text("ضيف فيديو الأول بـ /add <رابط_يوتيوب>")
            return
        session["active"] = True
        if session["current_start_sec"] >= session["duration"]:
            session["current_start_sec"] = 0
        interval = session["interval_sec"]
        job_name = f"study_{uid}"
        for j in context.job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()
        job = context.job_queue.run_repeating(
            send_next_card,
            interval=timedelta(seconds=interval),
            first=0,
            data={"user_id": int(uid)},
            name=job_name,
        )
        session["job_name"] = job_name
        save_db()

    await update.message.reply_text("بدأت الجلسة ✅ هنبعتلك كارت كل فترة محددة.")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        session = STATE["users"][uid]
        session["active"] = False
        if session.get("job_name"):
            for j in context.job_queue.get_jobs_by_name(session["job_name"]):
                j.schedule_removal()
            session["job_name"] = None
        save_db()
    await update.message.reply_text("تم إيقاف الجلسة ⏹️")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    async with STATE_LOCK:
        ensure_user(update.effective_user.id)
        s = STATE["users"][uid]
        msg = (
            f"العنوان: {s['title'] or '—'}\n"
            f"المتبقي من الفيديو: {fmt_time(max(s['duration'] - s['current_start_sec'], 0))}\n"
            f"الفترة بين الكروت: {fmt_time(s['interval_sec'])}\n"
            f"مدة كل مقطع: {fmt_time(s['chunk_sec'])}\n"
            f"الجلسة: {'شغالة' if s['active'] else 'متوقفة'}"
        )
    await update.message.reply_text(msg)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(update.effective_user.id)

    if q.data == "skip":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("تم تخطي المقطع ⏭️")
        await send_next_card(context)

    elif q.data == "stop":
        async with STATE_LOCK:
            s = STATE["users"].get(uid)
            if s:
                s["active"] = False
                if s.get("job_name"):
                    for j in context.job_queue.get_jobs_by_name(s["job_name"]):
                        j.schedule_removal()
                    s["job_name"] = None
                save_db()
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("تم إيقاف الجلسة ⏹️")


def main():
    if not BOT_TOKEN:
        print("Please set TELEGRAM_BOT_TOKEN in environment.")
        return
    load_db()

    from telegram.ext import Application
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("setinterval", setinterval_cmd))
    app.add_handler(CommandHandler("setchunk", setchunk_cmd))
    app.add_handler(CommandHandler("startsession", startsession_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_with_link))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
