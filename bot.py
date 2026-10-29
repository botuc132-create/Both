import logging
import os
import sqlite3
import asyncio
from aiohttp import web
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError, RetryAfter, Forbidden
from database import Store, now_iso, REWARD_TOKENS, KEY_COST

from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO")
)
log = logging.getLogger("token-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
def resolve_db_path():
    """Use the configured SQLite path when writable; fall back if Render has no /data disk."""
    configured = os.getenv("DB_PATH", "/data/bot.db").strip() or "/data/bot.db"
    candidates = [configured]
    fallback = os.path.abspath("bot.db")
    if os.path.abspath(configured) != fallback:
        candidates.append(fallback)

    for candidate in candidates:
        try:
            parent = os.path.dirname(os.path.abspath(candidate))
            os.makedirs(parent, exist_ok=True)
            # Test actual write permission before Store initializes the database.
            probe = os.path.join(parent, f".db_write_test_{os.getpid()}")
            with open(probe, "w", encoding="utf-8") as test_file:
                test_file.write("ok")
            os.remove(probe)
            if candidate != configured:
                log.warning("Configured DB path %s is not writable; using %s instead.", configured, candidate)
            return candidate
        except OSError as exc:
            log.warning("Database path %s is not writable: %s", candidate, exc)

    raise RuntimeError("Không có đường dẫn database nào ghi được. Hãy kiểm tra quyền ghi trên Render.")

DB_PATH = resolve_db_path()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
BROADCAST_DELAY = 0.05


store = Store(DB_PATH)
# Key requests wait for admin approval; points are held until the request is decided.
with store.connect() as _conn:
    _conn.execute("""CREATE TABLE IF NOT EXISTS key_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        key_id INTEGER NOT NULL,
        cost INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by INTEGER,
        reject_reason TEXT
    )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_key_requests_status ON key_requests(status, id)")


def get_invite_count(user_id):
    """Count only real successful referrals; no admin-set invite adjustments."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM referrals WHERE inviter_id=?", (user_id,)
        ).fetchone()
    return int(row["n"])

def is_admin(user_id):
    return user_id in ADMIN_IDS

def main_keyboard(user_id):
    rows = [
        [KeyboardButton("🔑 Đổi key")],
        [KeyboardButton("💰 Số dư điểm"), KeyboardButton("🗃 Kho key")],
        [KeyboardButton("📨 Mời bạn bè"), KeyboardButton("🏆 Top 5 người mời")],
        [KeyboardButton("🆘 Hỗ trợ")]
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton("🛡 Quản trị")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Thêm key", callback_data="adm:addkeys"),
         InlineKeyboardButton("📦 Thống kê kho", callback_data="adm:keys")],
        [InlineKeyboardButton("👤 Tổng người dùng", callback_data="adm:users"),
         InlineKeyboardButton("🔎 Tra cứu ID", callback_data="adm:lookup")],
        [InlineKeyboardButton("🪙 Cộng/trừ điểm", callback_data="adm:tokens"),
         InlineKeyboardButton("📣 Phát thông báo", callback_data="adm:broadcast")],
        [InlineKeyboardButton("🔗 Nhóm/kênh bắt buộc", callback_data="adm:chats")],
        [InlineKeyboardButton("📊 Thống kê", callback_data="adm:stats"),
         InlineKeyboardButton("🏠 Menu chính", callback_data="home")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    is_new_user = store.get_user(user.id) is None
    store.upsert_user(user)
    args = context.args
    if is_new_user and args and args[0].startswith("ref_"):
        try:
            inviter = int(args[0][4:])
        except ValueError:
            inviter = 0
        if inviter and inviter != user.id and store.get_user(inviter):
            store.set_pending_inviter(user.id, inviter)
    if is_admin(user.id):
        await update.effective_message.reply_text(
            "✨ *JINNY CHEAT FREE KEY* ✨\n━━━━━━━━━━━━━━\nChào mừng quản trị viên! Chọn chức năng bên dưới.",
            parse_mode="Markdown", reply_markup=main_keyboard(user.id))
        return
    # Always re-check the live required-chat list. Admins can add a new channel
    # at any time, and previously verified users must join it before continuing.
    if is_admin(user.id):
        await update.effective_message.reply_text(
            "✨ *JINNY CHEAT FREE KEY* ✨\n━━━━━━━━━━━━━━\nChào mừng quản trị viên! Chọn chức năng bên dưới.",
            parse_mode="Markdown", reply_markup=main_keyboard(user.id))
        return
    missing, error = await required_check(user.id, context)
    if error:
        await update.effective_message.reply_text("⚠️ Chưa thể xác minh thành viên. Vui lòng thử lại sau.")
        return
    if missing:
        await send_join_gate(update.effective_message, context, user.id, missing)
        return
    if not store.user_verified(user.id):
        _, inviter = store.verify_user_and_reward(user.id)
        if inviter:
            try:
                await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} điểm vì người được mời đã xác minh!")
            except TelegramError:
                pass
    await update.effective_message.reply_text(
        "✨ *JINNY CHEAT FREE KEY* ✨\n━━━━━━━━━━━━━━\n✅ Bạn đã xác minh đủ nhóm/kênh. Chọn chức năng bên dưới.",
        parse_mode="Markdown", reply_markup=main_keyboard(user.id))

async def send_join_gate(message, context, user_id, chats=None):
    if chats is None:
        chats = store.required_chats()
    if not chats:
        if not store.user_verified(user_id):
            _, inviter = store.verify_user_and_reward(user_id)
            if inviter:
                try:
                    await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} điểm vì người được mời đã xác minh!")
                except TelegramError:
                    pass
        await message.reply_text("✨ *JINNY CHEAT FREE KEY* ✨\n━━━━━━━━━━━━━━\nChọn chức năng bên dưới.",
                                 parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return
    buttons = [[InlineKeyboardButton(f"📢 {chat['title'][:40]}", url=chat["invite_url"])] for chat in chats]
    buttons.append([InlineKeyboardButton("✅ VERIFY", callback_data="verify_gate")])
    await message.reply_text(
        "🚫 *ACCESS DENIED!*\n\nBạn cần tham gia các nhóm/kênh bên dưới để tiếp tục sử dụng bot.\n\n👇 Tham gia xong hãy bấm ✅ *VERIFY*.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def verify_gate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if is_admin(uid):
        await q.message.reply_text("✅ Quản trị viên có thể sử dụng bot.", reply_markup=main_keyboard(uid))
        return
    missing, error = await required_check(uid, context)
    if error:
        await q.message.reply_text("⚠️ Không thể xác minh lúc này. Bot vẫn bị khóa; hãy thử lại sau.")
        return
    if missing:
        await send_join_gate(q.message, context, uid, missing)
        return
    inviter = None
    if not store.user_verified(uid):
        _, inviter = store.verify_user_and_reward(uid)
    await q.message.reply_text("✅ XÁC MINH THÀNH CÔNG! Bạn đã tham gia đủ các nhóm/kênh hiện được yêu cầu.", reply_markup=main_keyboard(uid))
    if inviter:
        try:
            await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} điểm vì người được mời đã tham gia đủ kênh và xác minh thành công!")
        except TelegramError:
            pass

async def show_referral(update, context):
    uid = update.effective_user.id
    username = BOT_USERNAME or (await context.bot.get_me()).username
    link = f"https://t.me/{username}?start=ref_{uid}"
    details = store.user_details(uid)
    count = details[1] if details else 0
    await update.effective_message.reply_text(
        f"👥 *GIỚI THIỆU BẠN BÈ*\n━━━━━━━━━━━━━━\n🔗 Liên kết của bạn:\n`{link}`\n\n✅ Lượt mời thành công: *{get_invite_count(uid)}*\n💰 Số dư: *{store.balance(uid)} điểm*\n🎁 Mỗi người mới hợp lệ mang lại {REWARD_TOKENS} điểm.",
        parse_mode="Markdown", reply_markup=main_keyboard(uid))

async def required_check(user_id, context):
    chats = store.required_chats()
    missing = []
    for chat in chats:
        try:
            member = await context.bot.get_chat_member(chat_id=chat["chat_id"], user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) or (
                member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", False)
            ):
                missing.append(chat)
        except TelegramError:
            return None, "Không thể xác minh tư cách thành viên hiện tại. Vui lòng thử lại sau; key chưa được cấp."
    return missing, None

def create_key_request(user_id):
    """Reserve an available key and hold the points until admin decides."""
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute("SELECT token_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            c.execute("ROLLBACK")
            return False, "USER_NOT_FOUND", None
        if user["token_balance"] < KEY_COST:
            c.execute("ROLLBACK")
            return False, "INSUFFICIENT", None
        pending = c.execute("SELECT id FROM key_requests WHERE user_id=? AND status='pending' LIMIT 1", (user_id,)).fetchone()
        if pending:
            c.execute("ROLLBACK")
            return False, "PENDING_EXISTS", None
        key = c.execute("""SELECT k.id,k.key_text FROM keys k
            WHERE k.claimed_at IS NULL AND NOT EXISTS (
                SELECT 1 FROM key_requests r WHERE r.key_id=k.id AND r.status='pending'
            ) ORDER BY k.id LIMIT 1""").fetchone()
        if not key:
            c.execute("ROLLBACK")
            return False, "EMPTY", None
        balance = int(user["token_balance"]) - KEY_COST
        ts = now_iso()
        c.execute("UPDATE users SET token_balance=? WHERE user_id=?", (balance, user_id))
        cur = c.execute("INSERT INTO key_requests(user_id,key_id,cost,status,created_at) VALUES(?,?,?,'pending',?)",
                        (user_id, key["id"], KEY_COST, ts))
        c.execute("INSERT INTO token_transactions(user_id,amount,balance_after,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                  (user_id, -KEY_COST, balance, "Hold points for pending key approval", None, ts))
        c.execute("COMMIT")
        return True, "PENDING", {"request_id": cur.lastrowid, "key_id": key["id"], "key_text": key["key_text"], "balance": balance, "created_at": ts}

async def request_key_approval(message, context, user_id):
    ok, status, data = await asyncio.to_thread(create_key_request, user_id)
    if not ok:
        if status == "INSUFFICIENT":
            balance = store.balance(user_id)
            missing_points = max(0, KEY_COST - balance)
            people_needed = (missing_points + REWARD_TOKENS - 1) // REWARD_TOKENS
            await message.reply_text(f"❌ Bạn còn thiếu {missing_points} điểm nữa để có thể đổi key.\nHãy mời thêm {people_needed} người nữa để nhận đủ điểm nhé!", reply_markup=main_keyboard(user_id))
        elif status == "EMPTY":
            await message.reply_text("📭 Hiện đang hết key. Vui lòng nhắn tin cho admin để thêm key: @huybuwin", reply_markup=main_keyboard(user_id))
        elif status == "PENDING_EXISTS":
            await message.reply_text("⏳ Bạn đang có một yêu cầu đổi key chờ admin duyệt. Vui lòng đợi kết quả trước khi gửi yêu cầu mới.", reply_markup=main_keyboard(user_id))
        else:
            await message.reply_text("Không thể gửi yêu cầu đổi key lúc này.", reply_markup=main_keyboard(user_id))
        return
    await message.reply_text("✅ Đổi key thành công vui lòng đợi admin xét duyệt số điểm sẽ bị tạm giữ nếu thấy lâu quá hãy nhắn @huybuwin để hỏi !!", reply_markup=main_keyboard(user_id))
    from zoneinfo import ZoneInfo
    vn_time = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m/%Y")
    user = store.get_user(user_id)
    uname = f"@{user['username']}" if user and user['username'] else "(chưa có username)"
    admin_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Duyệt", callback_data=f"keyreq:approve:{data['request_id']}"),
        InlineKeyboardButton("❌ Từ chối", callback_data=f"keyreq:reject:{data['request_id']}")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id,
                f"📥 YÊU CẦU ĐỔI KEY\nNgười dùng {uname} ID {user_id}\nĐã đổi key lúc {vn_time} giờ Việt Nam.\nYêu cầu #{data['request_id']} — điểm đang tạm giữ: {KEY_COST} điểm.",
                reply_markup=admin_kb)
        except TelegramError:
            log.exception("Could not notify admin %s about key request", admin_id)

async def receive_key(update, context):
    uid = update.effective_user.id
    missing, error = await required_check(uid, context)
    if error:
        await update.effective_message.reply_text("⚠️ " + error, reply_markup=main_keyboard(uid)); return
    if missing:
        buttons = [[InlineKeyboardButton("➕ " + c["title"][:45], url=c["invite_url"])] for c in missing]
        buttons.append([InlineKeyboardButton("🔄 Tôi đã tham gia — kiểm tra lại", callback_data="checkjoin")])
        await update.effective_message.reply_text("🔒 Bạn cần tham gia đầy đủ nhóm/kênh bên dưới trước khi đổi key:", reply_markup=InlineKeyboardMarkup(buttons)); return
    await request_key_approval(update.effective_message, context, uid)

async def admin_command(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Bạn không có quyền sử dụng chức năng này."); return
    store.audit(uid, "open_admin", "Opened admin menu")
    await update.effective_message.reply_text("🛡 *BẢNG QUẢN TRỊ*\nChọn chức năng:", parse_mode="Markdown", reply_markup=admin_keyboard())

async def admin_callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    action = q.data or ""
    if action.startswith("adm:") and not is_admin(uid):
        await q.edit_message_text("⛔ Bạn không có quyền quản trị."); return
    if action == "home":
        await q.edit_message_text("Đã quay về menu chính. Dùng bàn phím Telegram bên dưới.")
    elif action == "adm:keys":
        s = store.key_stats()
        await q.edit_message_text(f"📦 *KHO KEY*\nTổng: {s['total']}\nCòn: {s['available']}\nĐã cấp: {s['claimed']}", parse_mode="Markdown", reply_markup=admin_keyboard())
    elif action == "adm:users":
        await q.edit_message_text(f"👤 Tổng người dùng đã /start: {store.users_count()}", reply_markup=admin_keyboard())
    elif action == "adm:stats":
        s=store.key_stats()
        with store.connect() as c:
            tokens=c.execute("SELECT COALESCE(SUM(token_balance),0) n FROM users").fetchone()["n"]
            refs=c.execute("SELECT COUNT(*) n FROM referrals").fetchone()["n"]
            claims=c.execute("SELECT COUNT(*) n FROM key_claims").fetchone()["n"]
        await q.edit_message_text(f"📊 *THỐNG KÊ*\nNgười dùng: {store.users_count()}\nLượt giới thiệu: {refs}\nTổng điểm đang lưu: {tokens}\nKey còn/đã cấp/tổng: {s['available']}/{s['claimed']}/{s['total']}\nLượt nhận key: {claims}", parse_mode="Markdown", reply_markup=admin_keyboard())
    elif action == "adm:addkeys":
        context.user_data["admin_state"] = "addkeys"
        await q.edit_message_text("Gửi các key, mỗi dòng một bản ghi. Dòng trùng vẫn được lưu riêng.\nGõ /cancel để hủy.")
    elif action == "adm:lookup":
        context.user_data["admin_state"] = "lookup"
        await q.edit_message_text("Gửi Telegram ID người dùng cần kiểm tra. /cancel để hủy.")
    elif action == "adm:tokens":
        context.user_data["admin_state"] = "tokens_user"
        await q.edit_message_text("Nhập Telegram ID của người cần cộng/trừ điểm. /cancel để hủy.")
    elif action == "adm:broadcast":
        context.user_data["admin_state"] = "broadcast"
        await q.edit_message_text("Gửi nội dung thông báo cho người đã /start bot. Sau đó bot sẽ yêu cầu xác nhận.\n/cancel để hủy.")
    elif action == "adm:chats":
        chats=store.required_chats(False)
        lines=["🔗 *NHÓM/KÊNH BẮT BUỘC*"]
        lines += [f"• {x['title']} — {'Bật' if x['enabled'] else 'Tắt'} (`{x['chat_id']}`)" for x in chats]
        lines += ["\nChọn thao tác bên dưới. Bot phải được thêm vào nhóm/kênh; với kênh nên cấp quyền quản trị."]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Thêm nhóm/kênh bằng link", callback_data="adm:chat_add")],
            [InlineKeyboardButton("✏️ Sửa link nhóm/kênh", callback_data="adm:chat_edit")],
            *[[InlineKeyboardButton("🗑 Xóa " + x["title"][:35], callback_data="adm:chat_del:" + x["chat_id"]) ] for x in chats if x["enabled"]],
            [InlineKeyboardButton("🏠 Menu quản trị", callback_data="adm:back")]
        ])
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    elif action == "adm:chat_add":
        context.user_data["admin_state"] = "chat_add_link"
        await q.edit_message_text("Gửi link công khai của nhóm/kênh, ví dụ https://t.me/tenkenh. Bot phải được thêm vào nhóm/kênh. /cancel để hủy.")
    elif action == "adm:chat_edit":
        chats = store.required_chats(False)
        if not chats:
            await q.edit_message_text("Danh sách nhóm/kênh đang trống.", reply_markup=admin_keyboard())
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(x["title"][:45], callback_data="adm:chat_edit_pick:" + x["chat_id"])] for x in chats] + [[InlineKeyboardButton("⬅️ Quay lại", callback_data="adm:chats")]])
            await q.edit_message_text("Chọn nhóm/kênh cần đổi link:", reply_markup=kb)
    elif action.startswith("adm:chat_del:"):
        chat_id = action.split(":", 2)[2]
        store.chat_setting(chat_id, delete=True)
        store.audit(uid, "delete_required_chat", chat_id)
        await q.edit_message_text("🗑 Đã xóa nhóm/kênh khỏi danh sách bắt buộc.", reply_markup=admin_keyboard())
    elif action.startswith("adm:chat_edit_pick:"):
        chat_id = action.split(":", 2)[2]
        context.user_data["chat_edit_id"] = chat_id
        context.user_data["admin_state"] = "chat_edit_link"
        await q.edit_message_text("Gửi link mới cho nhóm/kênh đã chọn. Ví dụ https://t.me/tenkenh. /cancel để hủy.")
    elif action == "adm:back":
        await q.edit_message_text("🛡 BẢNG QUẢN TRỊ", reply_markup=admin_keyboard())
    else:
        await q.edit_message_text("Chức năng không xác định.", reply_markup=admin_keyboard())

async def cancel(update, context):
    context.user_data.pop("admin_state", None)
    context.user_data.pop("pending_broadcast", None)
    context.user_data.pop("reject_key_request_id", None)
    await update.effective_message.reply_text("Đã hủy thao tác.", reply_markup=main_keyboard(update.effective_user.id))

async def admin_text(update, context):
    uid=update.effective_user.id
    state=context.user_data.get("admin_state")
    if not state: return False
    if not is_admin(uid):
        context.user_data.clear()
        await update.effective_message.reply_text("⛔ Không có quyền."); return True
    text=update.effective_message.text or ""
    if state == "chat_add_link":
        link = text.strip()
        import re
        m = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]{5,})/?", link)
        if not m:
            await update.effective_message.reply_text("Vui lòng gửi link công khai dạng https://t.me/tenkenh. Link mời riêng tư không thể tự tra cứu tên/ID qua Bot API.")
            return True
        username = "@" + m.group(1)
        try:
            chat = await context.bot.get_chat(username)
            store.add_required_chat(str(chat.id), chat.title or username, link, uid)
            store.audit(uid, "add_required_chat", f"{chat.id}|{chat.title}|{link}")
            await update.effective_message.reply_text(f"✅ Đã thêm: {chat.title or username}", reply_markup=admin_keyboard())
        except TelegramError as e:
            await update.effective_message.reply_text("Không lấy được thông tin chat. Hãy kiểm tra link và thêm bot vào nhóm/kênh trước. Với kênh, nên cấp quyền quản trị.")
            return True
    elif state == "chat_edit_link":
        link = text.strip()
        import re
        m = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]{5,})/?", link)
        if not m:
            await update.effective_message.reply_text("Gửi link công khai dạng https://t.me/tenkenh.")
            return True
        try:
            chat = await context.bot.get_chat("@" + m.group(1))
            chat_id = context.user_data.get("chat_edit_id")
            if not store.update_required_chat(chat_id, title=chat.title or link, invite_url=link):
                await update.effective_message.reply_text("Không tìm thấy nhóm/kênh đã chọn.")
            else:
                store.audit(uid, "edit_required_chat_link", f"{chat_id} -> {link}")
                await update.effective_message.reply_text(f"✅ Đã cập nhật link: {chat.title or link}", reply_markup=admin_keyboard())
        except TelegramError:
            await update.effective_message.reply_text("Không xác minh được link mới. Hãy kiểm tra link và quyền của bot.")
            return True
    elif state == "addkeys":
        count=store.add_keys(text.splitlines(), uid)
        store.audit(uid,"add_keys",f"Added {count} keys")
        await update.effective_message.reply_text(f"✅ Đã thêm {count} key vào kho.", reply_markup=admin_keyboard())
    elif state == "lookup":
        try: target=int(text.strip())
        except ValueError:
            await update.effective_message.reply_text("ID không hợp lệ. Gửi số Telegram ID hoặc /cancel."); return True
        if not store.user_details(target):
            await update.effective_message.reply_text("Không tìm thấy người dùng đã bắt đầu bot.")
        else:
            await send_account_card(update.effective_message, target)
        store.audit(uid,"lookup_user",str(target))
    elif state in ("account_add_amount", "account_sub_amount"):
        try:
            amount = int(text.strip())
            if amount <= 0:
                raise ValueError("Hãy nhập số điểm lớn hơn 0.")
            target = int(context.user_data["account_target_id"])
            signed_amount = amount if state == "account_add_amount" else -amount
            balance = store.add_tokens(target, signed_amount, "Admin account check adjustment", uid)
            store.audit(uid, "adjust_tokens", f"user={target}, amount={signed_amount}, balance={balance}")
            context.user_data.pop("account_target_id", None)
            context.user_data.pop("admin_state", None)
            await update.effective_message.reply_text(f"✅ Đã {'cộng' if signed_amount > 0 else 'trừ'} {amount} điểm. Số dư mới: {balance} điểm.")
            await send_account_card(update.effective_message, target)
            return True
        except (ValueError, TypeError, KeyError) as e:
            await update.effective_message.reply_text(f"Số điểm không hợp lệ: {e}. Hãy gửi số nguyên dương hoặc /cancel.")
            return True
    elif state == "tokens_user":
        try:
            target = int(text.strip())
            if not store.get_user(target):
                await update.effective_message.reply_text("Không tìm thấy người dùng đã /start bot. Nhập ID khác hoặc /cancel.")
                return True
            context.user_data["token_target_id"] = target
            context.user_data["admin_state"] = "tokens_amount"
            await update.effective_message.reply_text(f"Nhập số điểm cho ID {target}. Dùng số dương để cộng, số âm để trừ. Ví dụ: 50 hoặc -30.")
            return True
        except ValueError:
            await update.effective_message.reply_text("ID không hợp lệ. Hãy nhập Telegram ID dạng số hoặc /cancel.")
            return True
    elif state == "tokens_amount":
        try:
            amount = int(text.strip())
            target = int(context.user_data.get("token_target_id"))
            balance = store.add_tokens(target, amount, "Admin adjustment", uid)
            store.audit(uid, "adjust_tokens", f"user={target}, amount={amount}, balance={balance}")
            await update.effective_message.reply_text(f"✅ Đã {'cộng' if amount > 0 else 'trừ'} {abs(amount)} điểm cho ID {target}. Số dư mới: {balance} điểm.", reply_markup=admin_keyboard())
            context.user_data.pop("token_target_id", None)
        except (ValueError, TypeError) as e:
            await update.effective_message.reply_text(f"❌ Không thực hiện được: {e}. Nhập số nguyên khác 0 hoặc /cancel.")
            return True
    elif state == "key_reject_reason":
        reason = text.strip()
        if not reason:
            await update.effective_message.reply_text("Vui lòng nhập lý do từ chối hoặc /cancel.")
            return True
        request_id = context.user_data.get("reject_key_request_id")
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = conn.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
            if not req or req["status"] != "pending":
                conn.execute("ROLLBACK")
                await update.effective_message.reply_text("Yêu cầu không còn ở trạng thái chờ duyệt.")
                context.user_data.pop("reject_key_request_id", None)
                context.user_data.pop("admin_state", None)
                return True
            user = conn.execute("SELECT token_balance FROM users WHERE user_id=?", (req["user_id"],)).fetchone()
            new_balance = int(user["token_balance"]) + int(req["cost"])
            conn.execute("UPDATE users SET token_balance=? WHERE user_id=?", (new_balance, req["user_id"]))
            conn.execute("INSERT INTO token_transactions(user_id,amount,balance_after,reason,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                         (req["user_id"], req["cost"], new_balance, "Refund: key request rejected", uid, now_iso()))
            conn.execute("UPDATE key_requests SET status='rejected',decided_at=?,decided_by=?,reject_reason=? WHERE id=?",
                         (now_iso(), uid, reason[:1000], request_id))
            conn.execute("COMMIT")
        store.audit(uid, "reject_key_request", f"request={request_id}; reason={reason}")
        try:
            await context.bot.send_message(req["user_id"], f"❌ Yêu cầu đổi key của bạn đã bị từ chối. Lý do: {reason}\n\nSố điểm đã được hoàn lại: {req['cost']} điểm. Số dư hiện tại: {new_balance} điểm.")
        except TelegramError:
            pass
        await update.effective_message.reply_text(f"❌ Đã từ chối yêu cầu #{request_id}; đã hoàn {req['cost']} điểm cho người dùng.", reply_markup=admin_keyboard())
        context.user_data.pop("reject_key_request_id", None)
    elif state == "broadcast":
        context.user_data["pending_broadcast"]=text
        context.user_data["admin_state"]="broadcast_confirm"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Xác nhận gửi",callback_data="broadcast:yes"),
                                  InlineKeyboardButton("❌ Hủy",callback_data="broadcast:no")]])
        await update.effective_message.reply_text(f"⚠️ Sắp gửi nội dung này đến {store.users_count()} người dùng:\n\n{text[:3000]}\n\nXác nhận?", reply_markup=kb)
    context.user_data.pop("admin_state",None)
    return True

async def broadcast_callback(update, context):
    q=update.callback_query
    await q.answer()
    uid=q.from_user.id
    if not is_admin(uid):
        await q.edit_message_text("⛔ Bạn không có quyền quản trị."); return
    if q.data == "broadcast:no":
        context.user_data.pop("pending_broadcast",None)
        context.user_data.pop("admin_state",None)
        await q.edit_message_text("Đã hủy phát thông báo."); return
    message=context.user_data.get("pending_broadcast")
    if not message:
        await q.edit_message_text("Không tìm thấy nội dung đang chờ."); return
    with store.connect() as c:
        ids=[r["user_id"] for r in c.execute("SELECT user_id FROM users")]
    success=failure=0
    await q.edit_message_text(f"Đang gửi thông báo đến {len(ids)} người dùng…")
    for target in ids:
        try:
            await context.bot.send_message(target, message)
            success += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                await context.bot.send_message(target, message); success += 1
            except TelegramError: failure += 1
        except (TelegramError, Exception):
            failure += 1
        await asyncio.sleep(BROADCAST_DELAY)
    store.broadcast_log(uid,"users",message,success,failure)
    store.audit(uid,"broadcast",f"target=users success={success} failure={failure}")
    context.user_data.pop("pending_broadcast",None)
    await context.bot.send_message(uid, f"📣 Hoàn tất. Thành công: {success}; thất bại: {failure}.")

async def chat_admin_command(update, context):
    uid=update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Không có quyền."); return
    cmd=update.effective_message.text.split()[0].split("@")[0]
    args=update.effective_message.text.partition(" ")[2].strip()
    try:
        if cmd == "/addchat":
            parts=[p.strip() for p in args.split("|")]
            if len(parts)!=3: raise ValueError("Mẫu: /addchat CHAT_ID | Tên | https://t.me/tenkenh")
            store.add_required_chat(parts[0],parts[1],parts[2],uid)
            store.audit(uid,"add_required_chat",args)
            await update.effective_message.reply_text("✅ Đã thêm/bật nhóm-kênh bắt buộc.")
        elif cmd == "/disablechat":
            store.chat_setting(args,enabled=False); store.audit(uid,"disable_chat",args)
            await update.effective_message.reply_text("Đã tắt yêu cầu chat.")
        elif cmd == "/enablechat":
            store.chat_setting(args,enabled=True); store.audit(uid,"enable_chat",args)
            await update.effective_message.reply_text("Đã bật yêu cầu chat.")
        elif cmd == "/delchat":
            store.chat_setting(args,delete=True); store.audit(uid,"delete_chat",args)
            await update.effective_message.reply_text("Đã xóa chat khỏi danh sách.")
    except ValueError as e:
        await update.effective_message.reply_text(str(e))

async def text_router(update, context):
    text=update.effective_message.text or ""
    if await admin_text(update,context): return
    uid=update.effective_user.id
    if not is_admin(uid):
        missing, error = await required_check(uid, context)
        if error:
            await update.effective_message.reply_text("⚠️ Không thể xác minh thành viên lúc này. Vui lòng thử lại sau; bot tạm thời bị khóa.")
            return
        if missing:
            await send_join_gate(update.effective_message, context, uid, missing)
            return
        if not store.user_verified(uid):
            _, inviter = store.verify_user_and_reward(uid)
            if inviter:
                try:
                    await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} điểm vì người được mời đã xác minh!")
                except TelegramError:
                    pass
    if text == "🔑 Đổi key": await receive_key(update,context)
    elif text == "💰 Số dư điểm":
        details = store.user_details(uid)
        total_invites = get_invite_count(uid) if details else 0
        # Referral timestamps are stored in UTC ISO format; convert the local
        # Vietnam calendar day to UTC boundaries for an accurate daily count.
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
        start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = (now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with store.connect() as conn:
            today_invites = conn.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=? AND created_at>=? AND created_at<?", (uid, start_utc, end_utc)).fetchone()["n"]
        balance = store.balance(uid)
        balance_text = (
            "╭━━━ ✨ *THÔNG TIN ĐIỂM* ✨ ━━━╮\n"
            f"💎 *Số điểm hiện có*\n"
            f"　└ 💰 *{balance:,} điểm*\n\n"
            f"🪪 *ID người dùng:* `{uid}`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👥 *THỐNG KÊ GIỚI THIỆU*\n"
            f"🏆 Tổng lượt mời: *{total_invites}*\n"
            f"📅 Lượt mời hôm nay: *{today_invites}*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎁 Mời bạn bè tham gia để tích lũy thêm điểm nhé!\n"
            "╰━━━━━━━━━━━━━━━━━━╯"
        )
        copy_id_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Sao chép ID", copy_text=CopyTextButton(text=str(uid)))],
            [InlineKeyboardButton("📨 Mời bạn bè", callback_data="balance:invite")]
        ])
        await update.effective_message.reply_text(
            balance_text,
            parse_mode="Markdown",
            reply_markup=copy_id_kb
        )
    elif text == "🗃 Kho key":
        s = store.key_stats()
        with store.connect() as conn:
            reserved_keys = conn.execute("SELECT COUNT(*) n FROM key_requests WHERE status='pending'").fetchone()["n"]
        s["available"] = max(0, s["available"] - reserved_keys)
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")
        day_end = (now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with store.connect() as conn:
            today_claims = conn.execute("SELECT COUNT(*) n FROM key_claims WHERE created_at>=? AND created_at<?", (day_start, day_end)).fetchone()["n"]
        await update.effective_message.reply_text(
            "----------------------\n"
            f"Kho key còn: {s['available']}\n\n"
            f"Lượt đổi key hôm nay: {today_claims}\n\n"
            "Mời bạn bè để nhận key ngay nhé 😍\n\n"
            "-----------------------", reply_markup=main_keyboard(uid))
    elif text == "📨 Mời bạn bè":
        username = BOT_USERNAME or (await context.bot.get_me()).username
        link = f"https://t.me/{username}?start=ref_{uid}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Sao chép link mời", copy_text=CopyTextButton(text=link))]])
        await update.effective_message.reply_text(
            f"🔗 Link mời riêng của bạn:\n{link}\n\nBấm nút bên dưới để sao chép link. Mỗi người mới hợp lệ tham gia đủ nhóm và xác minh sẽ giúp bạn nhận {REWARD_TOKENS} điểm.",
            reply_markup=kb)
    elif text == "🏆 Top 5 người mời":
        await show_top_invites(update.effective_message)
    elif text == "🆘 Hỗ trợ":
        await update.effective_message.reply_text("Liên hệ vào thẳng vấn đề @huybuwin", reply_markup=main_keyboard(uid))
    elif text == "🛡 Quản trị": await admin_command(update,context)


async def show_top_invites(message):
    with store.connect() as conn:
        rows = conn.execute("""SELECT u.user_id,u.username,u.first_name,
            COUNT(r.id) AS invite_count
            FROM users u LEFT JOIN referrals r ON r.inviter_id=u.user_id
            GROUP BY u.user_id ORDER BY invite_count DESC, u.user_id ASC LIMIT 5""").fetchall()
    lines = []
    for i in range(5):
        lines.append("------------------")
        if i < len(rows):
            row = rows[i]
            name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID {row['user_id']}")
            lines.append(f"Top {i+1}: {name} ({row['invite_count']} người mời)")
        else:
            lines.append(f"Top {i+1}: Chưa có dữ liệu (0 người mời)")
    lines.append("------------------")
    await message.reply_text("🏆 TOP 5 NGƯỜI MỜI NHIỀU NHẤT\n" + "\n".join(lines))

async def balance_quick_callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "balance:invite":
        username = BOT_USERNAME or (await context.bot.get_me()).username
        link = f"https://t.me/{username}?start=ref_{uid}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Sao chép link mời", copy_text=CopyTextButton(text=link))]])
        await q.message.reply_text(
            f"📨 *LINK MỜI RIÊNG CỦA BẠN*\n\n`{link}`\n\nBấm nút bên dưới để sao chép link.",
            parse_mode="Markdown", reply_markup=kb)


async def key_request_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Bạn không có quyền quản trị.")
        return
    try:
        _, decision, request_id_text = q.data.split(":", 2)
        request_id = int(request_id_text)
    except (ValueError, AttributeError):
        await q.message.reply_text("Yêu cầu không hợp lệ.")
        return
    with store.connect() as conn:
        req = conn.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        await q.message.reply_text("Không tìm thấy yêu cầu này.")
        return
    if req["status"] != "pending":
        await q.message.reply_text(f"Yêu cầu này đã được xử lý: {req['status']}.")
        return
    if decision == "reject":
        context.user_data["reject_key_request_id"] = request_id
        context.user_data["admin_state"] = "key_reject_reason"
        await q.message.reply_text(f"Nhập lý do từ chối yêu cầu đổi key #{request_id}. Lý do này sẽ được gửi cho người dùng. Gõ /cancel để hủy.")
        return
    if decision != "approve":
        await q.message.reply_text("Thao tác không hợp lệ.")
        return
    from zoneinfo import ZoneInfo
    ts = now_iso()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
        if not current or current["status"] != "pending":
            conn.execute("ROLLBACK")
            await q.message.reply_text("Yêu cầu đã được xử lý bởi thao tác khác.")
            return
        key = conn.execute("SELECT key_text,claimed_at FROM keys WHERE id=?", (current["key_id"],)).fetchone()
        if not key or key["claimed_at"] is not None:
            conn.execute("ROLLBACK")
            await q.message.reply_text("Key không còn khả dụng; yêu cầu chưa được duyệt.")
            return
        conn.execute("UPDATE keys SET claimed_by=?,claimed_at=? WHERE id=?", (current["user_id"], ts, current["key_id"]))
        conn.execute("INSERT INTO key_claims(key_id,user_id,cost,created_at) VALUES(?,?,?,?)", (current["key_id"], current["user_id"], current["cost"], ts))
        conn.execute("UPDATE key_requests SET status='approved',decided_at=?,decided_by=? WHERE id=?", (ts, q.from_user.id, request_id))
        conn.execute("COMMIT")
    store.audit(q.from_user.id, "approve_key_request", f"request={request_id}; user={req['user_id']}")
    balance = store.balance(req["user_id"])
    try:
        await context.bot.send_message(req["user_id"],
            f"🎉 Chúc mừng bạn đã được admin duyệt key thành công!\n\n🔑 Mã key là: {key['key_text']}\n\nHãy mời bạn bè để nhận thêm nhiều key nhé!\nSố điểm còn: {balance} điểm",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Sao chép nhanh mã key", copy_text=CopyTextButton(text=key["key_text"]))]]))
        await q.message.reply_text(f"✅ Đã duyệt yêu cầu #{request_id} cho ID {req['user_id']}.")
    except TelegramError:
        await q.message.reply_text("Đã duyệt trong hệ thống nhưng không gửi được tin nhắn cho người dùng (có thể họ đã chặn bot).")

async def check_join_callback(update, context):
    q=update.callback_query
    await q.answer()
    missing,error=await required_check(q.from_user.id,context)
    if error:
        await q.message.reply_text("⚠️ "+error); return
    if missing:
        buttons=[[InlineKeyboardButton("➕ "+c["title"][:45],url=c["invite_url"])] for c in missing]
        buttons.append([InlineKeyboardButton("🔄 Kiểm tra lại",callback_data="checkjoin")])
        await q.message.reply_text("Bạn chưa tham gia đủ các nhóm/kênh bắt buộc.",reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await request_key_approval(q.message, context, q.from_user.id)

async def send_account_card(message, target_id):
    details = store.user_details(target_id)
    if not details:
        await message.reply_text("Không tìm thấy người dùng đã bắt đầu bot.")
        return
    user, referrals, claims, txs = details
    username = f"@{user['username']}" if user["username"] else "Chưa đặt username"
    text = (f"👤 THÔNG TIN TÀI KHOẢN\n"
            f"ID: {target_id}\n"
            f"Username: {username}\n"
            f"Tên: {user['first_name'] or '-'}\n"
            f"Số điểm: {user['token_balance']}\n"
            f"Tổng lượt mời: {get_invite_count(target_id)}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Lịch sử mời", callback_data=f"admuser:invites:{target_id}"),
         InlineKeyboardButton("🔑 Lịch sử đổi key", callback_data=f"admuser:keys:{target_id}")],
        [InlineKeyboardButton("➕ Cộng điểm", callback_data=f"admuser:add:{target_id}"),
         InlineKeyboardButton("➖ Trừ điểm", callback_data=f"admuser:sub:{target_id}")],
        [InlineKeyboardButton("🔄 Làm mới", callback_data=f"admuser:refresh:{target_id}")]
    ])
    await message.reply_text(text, reply_markup=kb)

async def account_admin_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Bạn không có quyền quản trị.")
        return
    try:
        _, action, raw_id = q.data.split(":", 2)
        target = int(raw_id)
    except (ValueError, AttributeError):
        await q.message.reply_text("Dữ liệu tài khoản không hợp lệ.")
        return
    details = store.user_details(target)
    if not details:
        await q.message.reply_text("Không tìm thấy tài khoản này.")
        return
    if action == "refresh":
        await send_account_card(q.message, target)
    elif action == "invites":
        with store.connect() as conn:
            rows = conn.execute("""SELECT r.invitee_id, u.username, u.first_name, r.created_at
                FROM referrals r LEFT JOIN users u ON u.user_id=r.invitee_id
                WHERE r.inviter_id=? ORDER BY r.id DESC LIMIT 50""", (target,)).fetchall()
        lines = [f"👥 LỊCH SỬ MỜI — ID {target}"]
        if rows:
            lines += [f"• @{r['username']} (ID: {r['invitee_id']}) — {r['created_at']}" if r['username'] else f"• {r['first_name'] or 'Người dùng'} (ID: {r['invitee_id']}) — {r['created_at']}" for r in rows]
        else:
            lines.append("Chưa có lượt mời nào.")
        await q.message.reply_text("\n".join(lines))
    elif action == "keys":
        _, _, claims, _ = details
        lines = [f"🔑 LỊCH SỬ ĐỔI KEY — ID {target}"]
        lines += [f"• {r['key_text']} — {r['claimed_at']}" for r in claims] or ["Chưa đổi key lần nào."]
        await q.message.reply_text("\n".join(lines))
    elif action in ("add", "sub"):
        context.user_data["account_target_id"] = target
        context.user_data["admin_state"] = "account_add_amount" if action == "add" else "account_sub_amount"
        verb = "cộng" if action == "add" else "trừ"
        await q.message.reply_text(f"Nhập số điểm cần {verb} cho ID {target} (chỉ nhập số nguyên dương). Gõ /cancel để hủy.")

async def error_handler(update, context):
    log.exception("Unhandled exception", exc_info=context.error)

async def health_handler(request):
    return web.json_response({"status": "ok", "service": "telegram-bot"})

async def start_health_server(application):
    # Render Web Service health endpoint; this does not replace Telegram polling.
    port = int(os.getenv("PORT", "10000"))
    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/health", health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    application.bot_data["health_runner"] = runner
    log.info("Health endpoint listening on port %s", port)

async def stop_health_server(application):
    runner = application.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()

def build_application():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable")
    app=Application.builder().token(BOT_TOKEN).post_init(start_health_server).post_shutdown(stop_health_server).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_command))
    app.add_handler(CommandHandler("cancel",cancel))
    for command in ("addchat","disablechat","enablechat","delchat"):
        app.add_handler(CommandHandler(command,chat_admin_command))
    app.add_handler(CallbackQueryHandler(verify_gate_callback, pattern=r"^verify_gate$"))
    app.add_handler(CallbackQueryHandler(key_request_callback, pattern=r"^keyreq:(approve|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(balance_quick_callback, pattern=r"^balance:invite$"))
    app.add_handler(CallbackQueryHandler(account_admin_callback, pattern=r"^admuser:(invites|keys|add|sub|refresh):\d+$"))
    app.add_handler(CallbackQueryHandler(admin_callback,pattern=r"^(adm:|home$)"))
    app.add_handler(CallbackQueryHandler(check_join_callback,pattern=r"^checkjoin$"))
    app.add_handler(CallbackQueryHandler(broadcast_callback,pattern=r"^broadcast:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN chưa được cấu hình. Hãy tạo file .env cục bộ hoặc đặt biến môi trường.")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
