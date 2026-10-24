# Telegram Token & Key Bot (Python)

Bot Telegram thuần Python, dùng `python-telegram-bot` Long Polling và SQLite. Không có website, frontend hay HTTP server.

## Tính năng

- Menu Telegram bằng ReplyKeyboard và InlineKeyboard.
- Deep link giới thiệu `https://t.me/<bot>?start=ref_<user_id>`, thưởng 10 Token một lần cho người mới.
- Nhận một key tốn 30 Token; trừ Token và đánh dấu key trong cùng giao dịch SQLite.
- Dòng key trùng vẫn là các bản ghi riêng.
- Kiểm tra nhóm/kênh bắt buộc bằng `getChatMember`; không cấp key nếu thiếu thành viên hoặc API lỗi.
- Admin qua `ADMIN_IDS`: thêm key nhiều dòng, thống kê, tra cứu ID, cộng/trừ Token, broadcast người đã /start, quản lý chat bắt buộc, audit log.
- SQLite tự khởi tạo bảng và lưu bền khi `DB_PATH` trỏ tới Railway Volume.

## Chạy thử trên máy tính

Cần Python 3.11+.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Sao chép `.env.example` thành `.env`, điền `BOT_TOKEN`, `ADMIN_IDS`. Python không tự nạp `.env`; hãy đặt biến môi trường trong terminal hoặc dùng một trình quản lý biến môi trường. Ví dụ Linux/macOS:

```bash
export BOT_TOKEN='token-tu-BotFather'
export ADMIN_IDS='123456789'
export DB_PATH='./bot.db'
python bot.py
```

Windows PowerShell:

```powershell
$env:BOT_TOKEN="token-tu-BotFather"
$env:ADMIN_IDS="123456789"
$env:DB_PATH="./bot.db"
python bot.py
```

## Tạo bot và lấy ID

1. Mở Telegram, tìm `@BotFather`, gửi `/newbot`, làm theo hướng dẫn.
2. Sao chép token BotFather trả về và đặt vào biến `BOT_TOKEN`. Không đăng token lên GitHub.
3. Để lấy Telegram ID, có thể nhắn `/start` cho bot lấy ID đáng tin cậy mà bạn tự kiểm soát, hoặc dùng bot tra ID đã biết. Đặt các ID số, phân cách dấu phẩy, vào `ADMIN_IDS`.
4. Đặt `BOT_USERNAME` không có dấu `@` (tùy chọn; nếu bỏ trống bot tự lấy username từ Telegram).

## Đưa mã nguồn lên GitHub

1. Tải và giải nén ZIP.
2. Tạo repository GitHub mới, tốt nhất để Private khi mới cấu hình.
3. Upload các file dự án (`bot.py`, `database.py`, `requirements.txt`, `Procfile`, `README.md`, `.gitignore`, `.env.example`, `tests/`). **Không upload `.env`, token hoặc file `.db`.**
4. Railway có thể triển khai trực tiếp từ repository GitHub.

## Triển khai Railway Worker

1. Tạo project Railway và chọn **Deploy from GitHub Repo**.
2. Chọn repository.
3. Trong service, phần Variables, thêm:
   - `BOT_TOKEN` = token từ BotFather
   - `ADMIN_IDS` = ID admin, ví dụ `123456789,987654321`
   - `DB_PATH` = `/data/bot.db`
   - `BOT_USERNAME` = username bot không có `@` (tùy chọn)
4. Trong service, thêm **Volume** và đặt mount path chính xác là `/data`. Nếu không có Volume, dữ liệu SQLite có thể mất khi redeploy.
5. Đặt Start Command là `python bot.py` nếu Railway không tự dùng Procfile. Đây là worker chạy polling, không cần domain hoặc HTTP server.
6. Deploy và xem Logs. Khi `BOT_TOKEN` hợp lệ, bot bắt đầu polling.

## Quản trị

- `/admin` mở menu admin (chỉ ID trong `ADMIN_IDS`).
- **Thêm key**: chọn nút “Thêm key”, gửi mỗi key trên một dòng.
- **Cộng/trừ Token**: chọn “Cộng/trừ Token”, gửi `USER_ID +50` hoặc `USER_ID -20`.
- **Tra cứu ID**: tra số dư, lịch sử giới thiệu, 10 lần nhận key gần nhất và giao dịch Token gần nhất.
- **Nhóm/kênh bắt buộc**: trong menu admin, lệnh:
  - `/addchat CHAT_ID | Tên hiển thị | https://t.me/tenkenh`
  - `/disablechat CHAT_ID`
  - `/enablechat CHAT_ID`
  - `/delchat CHAT_ID`
- Với kênh riêng tư, dùng chat ID dạng `-100...` và URL mời hợp lệ. Bot phải được thêm vào chat. Để xác minh thành viên ổn định, hãy thêm bot vào nhóm/kênh và cấp quyền cần thiết; ở kênh nên cấp quyền quản trị. Bot API có thể không xác minh được nếu bot không có quyền hoặc chat ID/URL sai.
- Broadcast hiện gửi tới những người đã bắt đầu bot. Telegram không cho bot tự mở cuộc trò chuyện với người chưa từng nhắn bot. Trước khi gửi có nút xác nhận; số thành công/thất bại được lưu.
- Để hủy bước nhập admin, gửi `/cancel`.

## Kiểm thử

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Kiểm thử không gọi Telegram thật, dùng SQLite tạm thời. Để chạy bot thật cần token Telegram hợp lệ.

## Lưu ý bảo mật và vận hành

- Không commit token. Nếu lỡ lộ token, thu hồi qua BotFather.
- SQLite sử dụng `BEGIN IMMEDIATE` cho các thao tác cấp key và Token để tránh cấp trùng khi nhiều update đến đồng thời.
- Thông báo nhóm/kênh: hiện chức năng broadcast được triển khai cho người dùng đã /start. Gửi broadcast tới nhóm/kênh chưa có giao diện soạn/xác nhận riêng; bot cần quyền gửi trong đích đến. Không dùng bot để gửi spam.
- Admin không thể tự cộng/trừ Token cho người chưa từng /start bot vì chưa có bản ghi người dùng.
