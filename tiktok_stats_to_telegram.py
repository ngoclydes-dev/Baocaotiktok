"""
TikTok Stats -> Telegram Report (Daily + End of Month)
=========================================================
Chỉ báo cáo qua Telegram, không ghi Google Sheet.
Trạng thái (số liệu hôm qua, tăng trưởng lũy kế trong tháng) được lưu
trong file JSON nhỏ (tiktok_state.json) ngay trong repo, được commit
lại sau mỗi lần chạy (xem bước "Commit state file" trong workflow).

Chỉ tính các video ĐĂNG TRONG THÁNG HIỆN TẠI.

Yêu cầu biến môi trường (đặt trong GitHub Secrets):
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
  TIKTOK_REFRESH_TOKEN
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
import calendar
import requests
from datetime import datetime, timezone, timedelta

# ----------------------------
# Cấu hình
# ----------------------------
TIKTOK_CLIENT_KEY = os.environ["TIKTOK_CLIENT_KEY"]
TIKTOK_CLIENT_SECRET = os.environ["TIKTOK_CLIENT_SECRET"]
TIKTOK_REFRESH_TOKEN = os.environ["TIKTOK_REFRESH_TOKEN"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

VN_TZ = timezone(timedelta(hours=7))
STATE_FILE = "tiktok_state.json"


# ----------------------------
# 1. TikTok: refresh token + lấy danh sách video
# ----------------------------
def refresh_access_token():
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": TIKTOK_REFRESH_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Không lấy được access_token: {data}")
    return data["access_token"]


def get_all_videos(access_token, max_count=20):
    """Lấy toàn bộ video (phân trang qua cursor)."""
    videos = []
    cursor = None
    has_more = True

    url = (
        "https://open.tiktokapis.com/v2/video/list/"
        "?fields=id,create_time,view_count,like_count,comment_count,share_count,share_url"
    )

    while has_more:
        body = {"max_count": max_count}
        if cursor:
            body["cursor"] = cursor

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("error", {}).get("code") != "ok":
            raise RuntimeError(f"TikTok API lỗi: {payload.get('error')}")

        data = payload["data"]
        videos.extend(data.get("videos", []))
        has_more = data.get("has_more", False)
        cursor = data.get("cursor")

        if len(videos) >= 200:  # giới hạn an toàn
            break

    return videos


# ----------------------------
# 2. State file (thay cho Google Sheet)
# ----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def empty_totals():
    return {"views": 0, "likes": 0, "comments": 0, "shares": 0}


# ----------------------------
# 3. Telegram
# ----------------------------
def send_telegram_message(text):
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def build_daily_report(today_str, totals, deltas, video_count, top_video, month_label):
    lines = [
        f"📊 *Báo cáo TikTok ngày {today_str}*",
        f"_(chỉ tính video đăng trong tháng {month_label})_",
        "",
        f"🎬 Số video đăng trong tháng: {video_count}",
        f"👁 Tổng views hiện tại: {totals['views']:,}",
        f"❤️ Tổng likes hiện tại: {totals['likes']:,}",
        "",
        "📈 *Tăng trưởng trong ngày:*",
        f"👁 Views: +{deltas['views']:,}",
        f"❤️ Likes: +{deltas['likes']:,}",
        f"💬 Comments: +{deltas['comments']:,}",
        f"🔁 Shares: +{deltas['shares']:,}",
    ]
    if top_video and top_video["delta_views"] > 0:
        lines += ["", "🔥 Video tăng views mạnh nhất hôm nay:"]
        link = top_video.get("share_url", "")
        lines.append(f"+{top_video['delta_views']:,} views" + (f" — {link}" if link else ""))
    return "\n".join(lines)


def build_monthly_report(month_label, month_growth, video_count):
    lines = [
        f"🗓 *Tổng kết TikTok tháng {month_label}*",
        "",
        f"🎬 Số video đã đăng trong tháng: {video_count}",
        f"👁 Views tăng trong tháng: +{month_growth['views']:,}",
        f"❤️ Likes tăng trong tháng: +{month_growth['likes']:,}",
        f"💬 Comments tăng trong tháng: +{month_growth['comments']:,}",
        f"🔁 Shares tăng trong tháng: +{month_growth['shares']:,}",
    ]
    return "\n".join(lines)


# ----------------------------
# Main
# ----------------------------
def main():
    now = datetime.now(VN_TZ)
    today_str = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")
    month_label = now.strftime("%m/%Y")

    print("Đang refresh access token...")
    access_token = refresh_access_token()

    print("Đang lấy danh sách video...")
    all_videos = get_all_videos(access_token)
    print(f"Lấy được {len(all_videos)} video (tất cả thời gian).")

    # --- Chỉ giữ video đăng trong tháng hiện tại ---
    videos = [
        v for v in all_videos
        if datetime.fromtimestamp(v["create_time"], tz=VN_TZ).strftime("%Y-%m") == month_key
    ]
    print(f"Trong đó {len(videos)} video đăng trong tháng {month_label}.")

    current_totals = {
        "views": sum(v.get("view_count", 0) for v in videos),
        "likes": sum(v.get("like_count", 0) for v in videos),
        "comments": sum(v.get("comment_count", 0) for v in videos),
        "shares": sum(v.get("share_count", 0) for v in videos),
    }
    current_video_views = {str(v["id"]): v.get("view_count", 0) for v in videos}

    # --- Load state, reset nếu sang tháng mới ---
    state = load_state()
    if not state or state.get("month") != month_key:
        state = {
            "month": month_key,
            "date": None,
            "totals": empty_totals(),
            "month_growth": empty_totals(),
            "video_prev_views": {},
        }

    # --- Tính tăng trưởng trong ngày ---
    if state["date"] == today_str:
        # Đã chạy hôm nay rồi, không cộng dồn lần nữa
        deltas = empty_totals()
    else:
        deltas = {
            k: max(current_totals[k] - state["totals"].get(k, 0), 0)
            for k in current_totals
        }
        for k in state["month_growth"]:
            state["month_growth"][k] += deltas[k]

    # --- Video tăng views mạnh nhất hôm nay ---
    top_video = None
    best_delta = -1
    for v in videos:
        vid = str(v["id"])
        prev_views = state["video_prev_views"].get(vid, v.get("view_count", 0))
        delta_v = v.get("view_count", 0) - prev_views
        if delta_v > best_delta:
            best_delta = delta_v
            top_video = {**v, "delta_views": delta_v}

    # --- Cập nhật state ---
    state["date"] = today_str
    state["totals"] = current_totals
    state["video_prev_views"] = current_video_views
    save_state(state)

    # --- Gửi báo cáo ngày ---
    print("Đang gửi báo cáo ngày qua Telegram...")
    daily_report = build_daily_report(
        today_str, current_totals, deltas, len(videos), top_video, month_label
    )
    send_telegram_message(daily_report)

    # --- Nếu là ngày cuối tháng -> gửi thêm báo cáo tổng kết tháng ---
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]
    if now.day == last_day_of_month:
        print("Hôm nay là ngày cuối tháng, đang gửi báo cáo tổng kết...")
        monthly_report = build_monthly_report(month_label, state["month_growth"], len(videos))
        send_telegram_message(monthly_report)

    print("Hoàn tất.")


if __name__ == "__main__":
    main()
