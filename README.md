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

## Triển khai Render Web Service (có health endpoint)
1. Upload toàn bộ dự án lên GitHub (không upload `.env`, token, hoặc database).
2. Trong Render, chọn **New + → Blueprint** và chọn repository có `render.yaml`. Bản này dùng Web Service để có URL health-check cho UptimeRobot.
3. Đặt biến `BOT_TOKEN`, `ADMIN_IDS`, `BOT_USERNAME`. `render.yaml` cấu hình `DB_PATH=/data/bot.db` và persistent disk tại `/data`.
4. Build command: `pip install -r requirements.txt`; start command: `python bot.py`.
5. Deploy và kiểm tra Logs. Mở `https://TEN-SERVICE.onrender.com/health`; nếu nhận HTTP 200 thì health endpoint hoạt động. Trong UptimeRobot, tạo HTTP(s) monitor trỏ chính xác đến đường dẫn `/health`.

Lưu ý: Persistent disk thường chỉ dùng được với gói Render hỗ trợ disk và có thể phát sinh phí. Nếu muốn chạy không mất phí, có thể thử local; không nên trông cậy vào filesystem tạm thời của host để giữ SQLite sau restart/deploy.

## Quản lý nhóm/kênh bắt buộc
Vào `/admin` → **Nhóm/kênh bắt buộc** → chọn Thêm/Sửa/Xóa.
- Thêm: bot hỏi link công khai dạng `https://t.me/tenkenh`; bot tự tra chat ID và tên.
- Sửa link: chọn nhóm/kênh hiện có rồi gửi link công khai mới.
- Xóa: bấm nút xóa tương ứng trong danh sách.
- Link mời riêng tư không thể tự tra chat ID chỉ từ URL bằng Telegram Bot API; hãy dùng link công khai hoặc cần bổ sung quy trình nhận ID từ tin nhắn được chuyển tiếp.

Chat ID nhóm/kênh thường có dạng `-100...`. Bot phải được thêm vào chat; để `getChatMember` xác minh ổn định, nên cấp bot quyền phù hợp (đặc biệt trong kênh). Nếu API không xác minh được, bot sẽ từ chối mở khóa thay vì bỏ qua kiểm tra.

## Test
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Bot chỉ gửi broadcast tới người đã mở cuộc trò chuyện với bot. Telegram không cho bot chủ động nhắn người chưa từng bắt đầu bot. Broadcast có bước xác nhận và ghi lại số gửi thành công/thất bại.


## Render + UptimeRobot health check

This version also serves a minimal `/health` endpoint on Render's `PORT` while Telegram updates are handled with Long Polling. Set the Render service type to **Web Service** (not Background Worker) if you need a public `onrender.com` URL. In UptimeRobot, monitor `https://YOUR-SERVICE.onrender.com/health`; a healthy service returns HTTP 200 and `{"status":"ok"}`. This endpoint reports that the process is responding; it does not guarantee Telegram API connectivity.

A free Render Web Service may sleep or have other limits, and an UptimeRobot ping is not a guarantee of uninterrupted operation. For reliable 24/7 bot operation, use an always-on paid instance/worker. Persistent disk availability and pricing depend on your Render plan; verify the disk is attached at `/data` before relying on SQLite persistence.
