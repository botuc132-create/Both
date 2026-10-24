# ZEXTRON FREE KEY — Telegram Bot (Render Worker)

Bot Telegram thuần Python 3.11+, `python-telegram-bot`, Long Polling và SQLite. Không có website, frontend hay HTTP server.

## Tính năng chính
- Màn hình `/start` theo mẫu ACCESS DENIED, nút JOIN CHANNEL 1/2/... và VERIFY.
- Chặn sử dụng bot cho đến khi thành viên tất cả nhóm/kênh bắt buộc và bấm VERIFY thành công.
- Referral chỉ được ghi nhận/thưởng sau khi người mới xác minh thành công; không thưởng tự mời hay thưởng lặp.
- Mỗi key giá 30 Token; referral +10 Token; cấp key và trừ Token trong giao dịch SQLite nguyên tử.
- Admin menu: thêm key nhiều dòng, thống kê, tra cứu, cộng/trừ Token, broadcast người đã `/start`, quản lý nhóm/kênh bắt buộc (thêm, sửa tên/link, xóa).
- SQLite tự tạo bảng và nâng cấp database cũ; dùng `DB_PATH=/data/bot.db` để lưu trên disk của Render.

## Chạy local
Yêu cầu Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Đặt biến môi trường `BOT_TOKEN`, `ADMIN_IDS`, `DB_PATH`, `BOT_USERNAME`, sau đó chạy `python bot.py`. `.env.example` chỉ là mẫu; Python không tự nạp file `.env`.

## Tạo bot
1. Mở `@BotFather`, gửi `/newbot` và lưu token riêng tư.
2. Lấy Telegram user ID dạng số của bạn bằng công cụ tra ID đáng tin cậy.
3. Đặt `ADMIN_IDS` bằng ID, nhiều ID cách nhau bằng dấu phẩy. Không đưa token thật lên GitHub.

## Triển khai Render Worker
1. Upload toàn bộ dự án lên GitHub (không upload `.env`, token, hoặc database).
2. Trong Render, chọn **New + → Blueprint** và chọn repository có `render.yaml`, hoặc tạo Background Worker thủ công.
3. Đặt biến `BOT_TOKEN`, `ADMIN_IDS`, `BOT_USERNAME`. `render.yaml` cấu hình `DB_PATH=/data/bot.db` và persistent disk tại `/data`.
4. Build command: `pip install -r requirements.txt`; start command: `python bot.py`.
5. Deploy và kiểm tra Logs. Không cần web service, domain hay HTTP server.

Lưu ý: Persistent disk thường chỉ dùng được với gói Render hỗ trợ disk và có thể phát sinh phí. Nếu muốn chạy không mất phí, có thể thử local; không nên trông cậy vào filesystem tạm thời của host để giữ SQLite sau restart/deploy.

## Quản lý nhóm/kênh bắt buộc
Vào `/admin` → **Nhóm/kênh bắt buộc** → chọn Thêm/Sửa/Xóa.
- Thêm: `CHAT_ID | Tên nhóm/kênh | https://t.me/link`
- Sửa: `CHAT_ID | Tên mới | https://t.me/link_moi`
- Xóa: gửi `CHAT_ID`.

Chat ID nhóm/kênh thường có dạng `-100...`. Bot phải được thêm vào chat; để `getChatMember` xác minh ổn định, nên cấp bot quyền phù hợp (đặc biệt trong kênh). Nếu API không xác minh được, bot sẽ từ chối mở khóa thay vì bỏ qua kiểm tra.

## Test
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Bot chỉ gửi broadcast tới người đã mở cuộc trò chuyện với bot. Telegram không cho bot chủ động nhắn người chưa từng bắt đầu bot. Broadcast có bước xác nhận và ghi lại số gửi thành công/thất bại.
