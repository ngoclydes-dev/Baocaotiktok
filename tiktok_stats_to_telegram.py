"""
TikTok Stats -> Telegram Report
==================================
Chạy 1 lần/ngày. Mỗi lần chạy gửi 1 tin nhắn Telegram gồm 2 phần:
  1. Tăng trưởng của "hôm qua" (so với lần chạy gần nhất trước đó)
  2. Lũy kế từ đầu tháng đến hôm qua (chỉ tính video đăng trong tháng)

Trạng thái (số liệu hôm qua, danh sách video đã biết) lưu trong file
tiktok_state.json ngay trong repo, được commit lại sau mỗi lần chạy.

Yêu cầu biến môi trường (đặt trong GitHub Secrets):
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
  TIKTOK_REFRESH_TOKEN
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
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
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def build_report(yesterday_str, new_video_count, deltas,
                  month_start_str, month_end_str,
                  month_video_count, month_totals):
    lines = [
        f"📊 Báo cáo TikTok tăng trưởng trong ngày hôm qua {yesterday_str}",
        f"🎬 Số video đăng mới: {new_video_count}",
        f"👁 Views: +{deltas['views']:,}",
        f"❤️ Likes: +{deltas['likes']:,}",
        f"💬 Comments: +{deltas['comments']:,}",
        f"🔁 Shares: +{deltas['shares']:,}",
        "---------------------------",
        f"📊 Báo cáo TikTok Trong tháng từ ngày {month_start_str} đến ngày {month_end_str}",
        f"🎬 Số video đăng trong tháng: {month_video_count}",
        f"👁 Tổng views hiện tại: {month_totals['views']:,}",
        f"❤️ Tổng likes hiện tại: {month_totals['likes']:,}",
        f"🔁 Tổng lượt Shares: +{month_totals['shares']:,}",
    ]
    return "\n".join(lines)


# ----------------------------
# Main
# ----------------------------
def main():
    now = datetime.now(VN_TZ)
    yesterday = now - timedelta(days=1)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = yesterday.strftime("%d/%m/%Y")
    month_key = now.strftime("%Y-%m")
    month_start_str = now.replace(day=1).strftime("%d/%m/%Y")
    month_end_str = yesterday_str  # lũy kế tính đến hết hôm qua

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
    print(f"Trong đó {len(videos)} video đăng trong tháng {now.strftime('%m/%Y')}.")

    month_totals = {
        "views": sum(v.get("view_count", 0) for v in videos),
        "likes": sum(v.get("like_count", 0) for v in videos),
        "comments": sum(v.get("comment_count", 0) for v in videos),
        "shares": sum(v.get("share_count", 0) for v in videos),
    }
    current_video_ids = {str(v["id"]) for v in videos}

    # --- Load state, reset nếu sang tháng mới ---
    state = load_state()
    if not state or state.get("month") != month_key:
        state = {
            "month": month_key,
            "date": None,
            "totals": empty_totals(),
            "known_video_ids": [],
        }

    # --- Tính tăng trưởng "hôm qua" (so với lần chạy trước) ---
    if state["date"] == today_str:
        # Đã chạy hôm nay rồi, không tính trùng
        deltas = empty_totals()
        new_video_count = 0
    else:
        deltas = {
            k: max(month_totals[k] - state["totals"].get(k, 0), 0)
            for k in month_totals
        }
        known_ids = set(state.get("known_video_ids", []))
        new_video_count = len(current_video_ids - known_ids)

    # --- Cập nhật state ---
    state["date"] = today_str
    state["totals"] = month_totals
    state["known_video_ids"] = list(current_video_ids)
    save_state(state)

    # --- Gửi báo cáo ---
    print("Đang gửi báo cáo qua Telegram...")
    report = build_report(
        yesterday_str, new_video_count, deltas,
        month_start_str, month_end_str,
        len(videos), month_totals,
    )
    send_telegram_message(report)

    print("Hoàn tất.")


if __name__ == "__main__":
    main()
