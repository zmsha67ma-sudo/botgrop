import os, re, time, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from telegram import (Update, ChatPermissions, InlineKeyboardButton,
                      InlineKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

TOKEN = os.getenv("BOT_TOKEN")          # export BOT_TOKEN="123:ABC"
FLOOD_LIMIT, FLOOD_SECONDS = 5, 5       # 5 رسائل خلال 5 ثواني = تكرار
MUTE_MINUTES = 10                       # مدة الكتم عند التكرار
MAX_WARNS = 3                           # عدد التحذيرات قبل الحظر
CAPTCHA_SECONDS = 60                    # مهلة التحقق للعضو الجديد
LINK_RE = re.compile(r"(https?://|t\.me/|www\.|\.com|\.org|\.net|@\w{5,})", re.I)

logging.basicConfig(level=logging.WARNING)
flood = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))
warns = defaultdict(int)
admin_cache = {}                        # chat_id -> (expiry, set(ids))

FULL = ChatPermissions(can_send_messages=True, can_send_polls=True,
                       can_send_other_messages=True, can_add_web_page_previews=True,
                       can_invite_users=True, can_send_audios=True,
                       can_send_documents=True, can_send_photos=True,
                       can_send_videos=True, can_send_video_notes=True,
                       can_send_voice_notes=True)
NONE = ChatPermissions(can_send_messages=False)


async def admins(ctx, chat_id):
    exp, ids = admin_cache.get(chat_id, (0, set()))
    if exp < time.time():
        ids = {a.user.id for a in await ctx.bot.get_chat_administrators(chat_id)}
        admin_cache[chat_id] = (time.time() + 120, ids)
    return ids


async def mute(ctx, chat_id, uid, minutes):
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await ctx.bot.restrict_chat_member(chat_id, uid, NONE, until_date=until)


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m or not m.from_user:
        return
    chat_id, uid = m.chat_id, m.from_user.id
    if uid in await admins(ctx, chat_id):
        return

    text = m.text or m.caption or ""
    # 1) روابط / توجيه / بوتات inline
    if LINK_RE.search(text) or m.forward_origin or m.via_bot:
        await m.delete()
        warns[(chat_id, uid)] += 1
        if warns[(chat_id, uid)] >= MAX_WARNS:
            await ctx.bot.ban_chat_member(chat_id, uid)
            warns.pop((chat_id, uid), None)
            await ctx.bot.send_message(chat_id, f"🚫 تم حظر {m.from_user.mention_html()} (مخالفات متكررة)", parse_mode="HTML")
        return

    # 2) منع التكرار (Flood)
    q = flood[(chat_id, uid)]
    now = time.time()
    q.append(now)
    if len(q) == FLOOD_LIMIT and now - q[0] < FLOOD_SECONDS:
        q.clear()
        await m.delete()
        await mute(ctx, chat_id, uid, MUTE_MINUTES)
        await ctx.bot.send_message(chat_id, f"🔇 تم كتم {m.from_user.mention_html()} {MUTE_MINUTES} دقائق (تكرار)", parse_mode="HTML")


async def new_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    await m.delete()  # حذف رسالة "انضم فلان"
    for u in m.new_chat_members:
        if u.is_bot:
            continue
        await mute(ctx, m.chat_id, u.id, 60 * 24)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ أنا لست روبوت", callback_data=f"ok:{u.id}")]])
        await ctx.bot.send_message(
            m.chat_id, f"أهلاً {u.mention_html()} 👋\nاضغط الزر خلال {CAPTCHA_SECONDS} ثانية للتحقق.",
            parse_mode="HTML", reply_markup=kb)
        ctx.job_queue.run_once(kick, CAPTCHA_SECONDS, data=(m.chat_id, u.id), name=f"cap{m.chat_id}:{u.id}")


async def kick(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id, uid = ctx.job.data
    await ctx.bot.ban_chat_member(chat_id, uid)
    await ctx.bot.unban_chat_member(chat_id, uid)


async def captcha_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = int(q.data.split(":")[1])
    if q.from_user.id != uid:
        return await q.answer("هذا الزر ليس لك ❌", show_alert=True)
    chat_id = q.message.chat_id
    for j in ctx.job_queue.get_jobs_by_name(f"cap{chat_id}:{uid}"):
        j.schedule_removal()
    await ctx.bot.restrict_chat_member(chat_id, uid, FULL)
    await q.answer("تم التحقق ✅")
    await q.message.delete()


def admin_cmd(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = update.effective_message
        if m.from_user.id not in await admins(ctx, m.chat_id):
            return
        if not m.reply_to_message:
            return await m.reply_text("ردّ على رسالة العضو أولاً.")
        await m.delete()
        await fn(m, m.reply_to_message.from_user, ctx)
    return wrapper


@admin_cmd
async def ban(m, u, ctx):
    await ctx.bot.ban_chat_member(m.chat_id, u.id)
    await ctx.bot.send_message(m.chat_id, f"🚫 تم حظر {u.mention_html()}", parse_mode="HTML")

@admin_cmd
async def unban(m, u, ctx):
    await ctx.bot.unban_chat_member(m.chat_id, u.id, only_if_banned=True)
    await ctx.bot.send_message(m.chat_id, f"✅ تم فك حظر {u.mention_html()}", parse_mode="HTML")

@admin_cmd
async def mute_cmd(m, u, ctx):
    await mute(ctx, m.chat_id, u.id, 60)
    await ctx.bot.send_message(m.chat_id, f"🔇 تم كتم {u.mention_html()} ساعة", parse_mode="HTML")

@admin_cmd
async def unmute(m, u, ctx):
    await ctx.bot.restrict_chat_member(m.chat_id, u.id, FULL)
    await ctx.bot.send_message(m.chat_id, f"🔊 تم فك كتم {u.mention_html()}", parse_mode="HTML")

@admin_cmd
async def warn(m, u, ctx):
    k = (m.chat_id, u.id)
    warns[k] += 1
    if warns[k] >= MAX_WARNS:
        warns.pop(k)
        await ctx.bot.ban_chat_member(m.chat_id, u.id)
        txt = f"🚫 تم حظر {u.mention_html()} (وصل {MAX_WARNS} تحذيرات)"
    else:
        txt = f"⚠️ تحذير {u.mention_html()} ({warns[k]}/{MAX_WARNS})"
    await ctx.bot.send_message(m.chat_id, txt, parse_mode="HTML")


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a):
        pass


def keep_alive():  # منفذ ويب صغير لـ Render و UptimeRobot
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Ping).serve_forever()


def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    G = filters.ChatType.GROUPS
    for name, fn in [("ban", ban), ("unban", unban), ("mute", mute_cmd),
                     ("unmute", unmute), ("warn", warn)]:
        app.add_handler(CommandHandler(name, fn, filters=G))
    app.add_handler(CallbackQueryHandler(captcha_cb, pattern=r"^ok:"))
    app.add_handler(MessageHandler(G & filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members))
    app.add_handler(MessageHandler(G & filters.StatusUpdate.LEFT_CHAT_MEMBER, lambda u, c: u.effective_message.delete()))
    app.add_handler(MessageHandler(G & ~filters.COMMAND & ~filters.StatusUpdate.ALL, guard))
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
