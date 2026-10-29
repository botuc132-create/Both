import logging
import os
import sqlite3
import asyncio
from aiohttp import web
from contextlib import contextmanager
from datetime import datetime, timezone
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
DB_PATH = os.getenv("DB_PATH", "bot.db")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
BROADCAST_DELAY = 0.05


store = Store()

def is_admin(user_id):
    return user_id in ADMIN_IDS

def main_keyboard(user_id):
    rows = [
        [KeyboardButton("🎁 Nhận key"), KeyboardButton("👥 Giới thiệu")],
        [KeyboardButton("💰 Số dư Token"), KeyboardButton("🗃 Kho key")],
        [KeyboardButton("⬇️ Tải hack")],
        [KeyboardButton("ℹ️ Hướng dẫn")]
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
        [InlineKeyboardButton("🪙 Cộng/trừ Token", callback_data="adm:tokens"),
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
            "✨ *ZEXTRON FREE KEY* ✨\n━━━━━━━━━━━━━━\nChào mừng quản trị viên! Chọn chức năng bên dưới.",
            parse_mode="Markdown", reply_markup=main_keyboard(user.id))
        return
    if store.user_verified(user.id):
        await update.effective_message.reply_text(
            "✨ *ZEXTRON FREE KEY* ✨\n━━━━━━━━━━━━━━\n✅ Bạn đã xác minh thành công. Chọn chức năng bên dưới.",
            parse_mode="Markdown", reply_markup=main_keyboard(user.id))
        return
    await send_join_gate(update.effective_message, context, user.id)

async def send_join_gate(message, context, user_id):
    chats = store.required_chats()
    if not chats:
        _, inviter = store.verify_user_and_reward(user_id)
        await message.reply_text("✨ *ZEXTRON FREE KEY* ✨\n━━━━━━━━━━━━━━\nChọn chức năng bên dưới.",
                                 parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        if inviter:
            try:
                await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} Token vì người được mời đã xác minh!")
            except TelegramError:
                pass
        return
    buttons = [[InlineKeyboardButton(f"📢 JOIN CHANNEL {i}", url=chat["invite_url"])] for i, chat in enumerate(chats, 1)]
    buttons.append([InlineKeyboardButton("✅ VERIFY", callback_data="verify_gate")])
    await message.reply_text(
        "🚫 *ACCESS DENIED!*\n\nYou must join our official channels to use this bot.\n\n👇 Join below and click ✅ *VERIFY*.",
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
        await q.message.reply_text("⚠️ Không thể xác minh lúc này. Chưa mở khóa bot; hãy thử lại sau.")
        return
    if missing:
        buttons = [[InlineKeyboardButton(f"📢 JOIN CHANNEL {i}", url=chat["invite_url"])] for i, chat in enumerate(missing, 1)]
        buttons.append([InlineKeyboardButton("✅ VERIFY", callback_data="verify_gate")])
        await q.message.reply_text("🚫 ACCESS DENIED! Bạn chưa tham gia đủ tất cả nhóm/kênh. Hãy tham gia rồi bấm VERIFY.", reply_markup=InlineKeyboardMarkup(buttons))
        return
    _, inviter = store.verify_user_and_reward(uid)
    await q.message.reply_text("✅ XÁC MINH THÀNH CÔNG! Bạn đã được mở khóa bot.", reply_markup=main_keyboard(uid))
    if inviter:
        try:
            await context.bot.send_message(inviter, f"🎉 Bạn được cộng {REWARD_TOKENS} Token vì người được mời đã tham gia đủ kênh và xác minh thành công!")
        except TelegramError:
            pass

async def show_referral(update, context):
    uid = update.effective_user.id
    username = BOT_USERNAME or (await context.bot.get_me()).username
    link = f"https://t.me/{username}?start=ref_{uid}"
    details = store.user_details(uid)
    count = details[1] if details else 0
    await update.effective_message.reply_text(
        f"👥 *GIỚI THIỆU BẠN BÈ*\n━━━━━━━━━━━━━━\n🔗 Liên kết của bạn:\n`{link}`\n\n✅ Lượt mời thành công: *{count}*\n💰 Số dư: *{store.balance(uid)} Token*\n🎁 Mỗi người mới hợp lệ mang lại {REWARD_TOKENS} Token.",
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

async def receive_key(update, context):
    uid = update.effective_user.id
    missing, error = await required_check(uid, context)
    if error:
        await update.effective_message.reply_text("⚠️ " + error, reply_markup=main_keyboard(uid)); return
    if missing:
        buttons = [[InlineKeyboardButton("➕ " + c["title"][:45], url=c["invite_url"])] for c in missing]
        buttons.append([InlineKeyboardButton("🔄 Tôi đã tham gia — kiểm tra lại", callback_data="checkjoin")])
        await update.effective_message.reply_text("🔒 Bạn cần tham gia đầy đủ nhóm/kênh bên dưới trước khi nhận key:",
                                                  reply_markup=InlineKeyboardMarkup(buttons)); return
    ok, status, key = store.claim_key(uid)
    if ok:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Sao chép nhanh mã key", copy_text=CopyTextButton(text=key))]])
        await update.effective_message.reply_text(
            f"🎉 Chúc mừng bạn đã đổi key thành công!\n\n🔑 Mã key là: `{key}`\n\nHãy mời bạn bè để nhận thêm nhiều key nhé!\n\n💸 Đã trừ {KEY_COST} Token. Số dư còn: {store.balance(uid)} Token",
            parse_mode="Markdown", reply_markup=kb)
    elif status == "INSUFFICIENT":
        await update.effective_message.reply_text(f"❌ Bạn cần {KEY_COST} Token để nhận key. Số dư hiện tại: {store.balance(uid)} Token.\nMời bạn bè để nhận thêm Token.", reply_markup=main_keyboard(uid))
    elif status == "EMPTY":
        await update.effective_message.reply_text("📭 Hiện đang hết key. Vui lòng nhắn tin cho admin để thêm key: @huybuwin", reply_markup=main_keyboard(uid))
    else:
        await update.effective_message.reply_text("Không thể cấp key lúc này. Vui lòng thử lại.", reply_markup=main_keyboard(uid))

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
        await q.edit_message_text(f"📊 *THỐNG KÊ*\nNgười dùng: {store.users_count()}\nLượt giới thiệu: {refs}\nTổng Token đang lưu: {tokens}\nKey còn/đã cấp/tổng: {s['available']}/{s['claimed']}/{s['total']}\nLượt nhận key: {claims}", parse_mode="Markdown", reply_markup=admin_keyboard())
    elif action == "adm:addkeys":
        context.user_data["admin_state"] = "addkeys"
        await q.edit_message_text("Gửi các key, mỗi dòng một bản ghi. Dòng trùng vẫn được lưu riêng.\nGõ /cancel để hủy.")
    elif action == "adm:lookup":
        context.user_data["admin_state"] = "lookup"
        await q.edit_message_text("Gửi Telegram ID người dùng cần tra cứu. /cancel để hủy.")
    elif action == "adm:tokens":
        context.user_data["admin_state"] = "tokens_user"
        await q.edit_message_text("Nhập Telegram ID của người cần cộng/trừ Token. /cancel để hủy.")
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
        d=store.user_details(target)
        if not d: await update.effective_message.reply_text("Không tìm thấy người dùng đã bắt đầu bot.")
        else:
            u, refs, claims, txs=d
            claim_text="\n".join(f"• {x['key_text']} ({x['claimed_at']})" for x in claims) or "Chưa nhận key"
            tx_text="\n".join(f"• {x['amount']:+} → {x['balance_after']} Token: {x['reason']}" for x in txs) or "Chưa có giao dịch"
            await update.effective_message.reply_text(f"👤 ID: {target}\nTên: {u['first_name']}\nUsername: @{u['username'] or '-'}\nSố dư: {u['token_balance']} Token\nGiới thiệu: {refs}\n\n🔑 Key đã cấp gần đây:\n{claim_text}\n\n🪙 Giao dịch gần đây:\n{tx_text}")
        store.audit(uid,"lookup_user",str(target))
    elif state == "tokens_user":
        try:
            target = int(text.strip())
            if not store.get_user(target):
                await update.effective_message.reply_text("Không tìm thấy người dùng đã /start bot. Nhập ID khác hoặc /cancel.")
                return True
            context.user_data["token_target_id"] = target
            context.user_data["admin_state"] = "tokens_amount"
            await update.effective_message.reply_text(f"Nhập số Token cho ID {target}. Dùng số dương để cộng, số âm để trừ. Ví dụ: 50 hoặc -30.")
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
            await update.effective_message.reply_text(f"✅ Đã {'cộng' if amount > 0 else 'trừ'} {abs(amount)} Token cho ID {target}. Số dư mới: {balance} Token.", reply_markup=admin_keyboard())
            context.user_data.pop("token_target_id", None)
        except (ValueError, TypeError) as e:
            await update.effective_message.reply_text(f"❌ Không thực hiện được: {e}. Nhập số nguyên khác 0 hoặc /cancel.")
            return True
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
    if not is_admin(uid) and not store.user_verified(uid):
        await send_join_gate(update.effective_message, context, uid)
        return
    if text == "🎁 Nhận key": await receive_key(update,context)
    elif text == "👥 Giới thiệu": await show_referral(update,context)
    elif text == "💰 Số dư Token":
        await update.effective_message.reply_text(f"💰 Số dư hiện tại: *{store.balance(uid)} Token*",parse_mode="Markdown",reply_markup=main_keyboard(uid))
    elif text == "🗃 Kho key":
        s=store.key_stats()
        await update.effective_message.reply_text(f"🗃 *TRẠNG THÁI KHO*\nKey còn: {s['available']}\nKey đã cấp: {s['claimed']}\nTổng: {s['total']}",parse_mode="Markdown",reply_markup=main_keyboard(uid))
    elif text == "⬇️ Tải hack":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Bấm vào đây ✅", url="https://link4sub.com/HRpFZdcmMs")]])
        await update.effective_message.reply_text(
            "⬇️ *TẢI HACK*\n\nBấm vào đây để tải:",
            parse_mode="Markdown", reply_markup=kb)
    elif text == "🛡 Quản trị": await admin_command(update,context)
    elif text == "ℹ️ Hướng dẫn":
        await update.effective_message.reply_text(f"🎁 Nhận key: {KEY_COST} Token\n👥 Giới thiệu người mới: +{REWARD_TOKENS} Token\nDùng /start để mở menu.",reply_markup=main_keyboard(uid))

async def check_join_callback(update, context):
    q=update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id) and not store.user_verified(q.from_user.id):
        await q.message.reply_text("🚫 Bạn cần xác minh đầy đủ nhóm/kênh trước khi sử dụng bot.")
        await send_join_gate(q.message, context, q.from_user.id)
        return
    missing,error=await required_check(q.from_user.id,context)
    if error:
        await q.message.reply_text("⚠️ "+error); return
    if missing:
        buttons=[[InlineKeyboardButton("➕ "+c["title"][:45],url=c["invite_url"])] for c in missing]
        buttons.append([InlineKeyboardButton("🔄 Kiểm tra lại",callback_data="checkjoin")])
        await q.message.reply_text("Bạn chưa tham gia đủ các nhóm/kênh bắt buộc.",reply_markup=InlineKeyboardMarkup(buttons))
    else:
        # Re-run full claim checks only after membership is confirmed.
        ok,status,key=store.claim_key(q.from_user.id)
        if ok:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Sao chép nhanh mã key", copy_text=CopyTextButton(text=key))]])
            await q.message.reply_text(f"🎉 Chúc mừng bạn đã đổi key thành công!\n\n🔑 Mã key là: `{key}`\n\nHãy mời bạn bè để nhận thêm nhiều key nhé!\n\n💸 Đã trừ {KEY_COST} Token. Số dư còn: {store.balance(q.from_user.id)} Token",parse_mode="Markdown",reply_markup=kb)
        elif status=="INSUFFICIENT":
            await q.message.reply_text(f"Bạn cần {KEY_COST} Token; hiện có {store.balance(q.from_user.id)}.")
        elif status=="EMPTY":
            await q.message.reply_text("📭 Hiện đang hết key. Vui lòng nhắn tin cho admin để thêm key: @huybuwin")
        else: await q.message.reply_text("Không thể cấp key lúc này.")

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
