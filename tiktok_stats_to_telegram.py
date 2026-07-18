"""
TikTok Stats (nhiều tài khoản) -> Telegram Report
====================================================
Chạy 1 lần/ngày. Với mỗi tài khoản TikTok trong TIKTOK_ACCOUNTS,
lấy số liệu, tính tăng trưởng "hôm qua" + lũy kế trong tháng,
rồi gửi 1 tin nhắn Telegram gồm: từng tài khoản + tổng cộng tất cả.

Trạng thái lưu trong file tiktok_state.json (1 file, nhiều tài khoản
bên trong), được commit lại sau mỗi lần chạy.

Yêu cầu biến môi trường (đặt trong GitHub Secrets):
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
  TIKTOK_ACCOUNTS       (JSON: [{"name": "...", "refresh_token": "..."}, ...])
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
TIKTOK_ACCOUNTS = json.loads(os.environ["TIKTOK_ACCOUNTS"])  # [{"name","refresh_token"}, ...]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

VN_TZ = timezone(timedelta(hours=7))
STATE_FILE = "tiktok_state.json"


# ----------------------------
# 1. TikTok: refresh token + lấy danh sách video
# ----------------------------
def refresh_access_token(refresh_token):
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
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
# 2. State file (nhiều tài khoản trong 1 file)
# ----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def empty_totals():
    return {"views": 0, "likes": 0, "comments": 0, "shares": 0}


def empty_account_state(month_key):
    return {
        "month": month_key,
        "date": None,
        "totals": empty_totals(),
        "known_video_ids": [],
    }


# ----------------------------
# 3. Xử lý số liệu 1 tài khoản
# ----------------------------
def process_account(account, all_state, now, month_key, today_str):
    name = account["name"]
    refresh_token = account["refresh_token"]

    access_token = refresh_access_token(refresh_token)
    all_videos = get_all_videos(access_token)

    videos = [
        v for v in all_videos
        if datetime.fromtimestamp(v["create_time"], tz=VN_TZ).strftime("%Y-%m") == month_key
    ]

    month_totals = {
        "views": sum(v.get("view_count", 0) for v in videos),
        "likes": sum(v.get("like_count", 0) for v in videos),
        "comments": sum(v.get("comment_count", 0) for v in videos),
        "shares": sum(v.get("share_count", 0) for v in videos),
    }
    current_video_ids = {str(v["id"]) for v in videos}

    acc_state = all_state.get(name)
    if not acc_state or acc_state.get("month") != month_key:
        acc_state = empty_account_state(month_key)

    if acc_state["date"] == today_str:
        deltas = empty_totals()
        new_video_count = 0
    else:
        deltas = {
            k: max(month_totals[k] - acc_state["totals"].get(k, 0), 0)
            for k in month_totals
        }
        known_ids = set(acc_state.get("known_video_ids", []))
        new_video_count = len(current_video_ids - known_ids)

    acc_state["date"] = today_str
    acc_state["totals"] = month_totals
    acc_state["known_video_ids"] = list(current_video_ids)
    all_state[name] = acc_state

    return {
        "name": name,
        "video_count": len(videos),
        "new_video_count": new_video_count,
        "deltas": deltas,
        "month_totals": month_totals,
    }


# ----------------------------
# 4. Telegram
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


def build_report(yesterday_str, month_start_str, month_end_str, account_results):
    lines = [f"📊 BÁO CÁO TIKTOK — {yesterday_str}", ""]

    total_new_videos = 0
    total_deltas = empty_totals()
    total_month_videos = 0
    total_month = empty_totals()

    for r in account_results:
        lines.append(f"🏢 {r['name']}")
        lines.append(f"— Tăng trưởng hôm qua {yesterday_str} —")
        lines.append(f"🎬 Video đăng mới: {r['new_video_count']}")
        lines.append(f"👁 Views: +{r['deltas']['views']:,}")
        lines.append(f"❤️ Likes: +{r['deltas']['likes']:,}")
        lines.append(f"💬 Comments: +{r['deltas']['comments']:,}")
        lines.append(f"🔁 Shares: +{r['deltas']['shares']:,}")
        lines.append(f"— Lũy kế tháng ({month_start_str} - {month_end_str}) —")
        lines.append(f"🎬 Số video trong tháng: {r['video_count']}")
        lines.append(f"👁 Tổng views: {r['month_totals']['views']:,}")
        lines.append(f"❤️ Tổng likes: {r['month_totals']['likes']:,}")
        lines.append(f"🔁 Tổng shares: +{r['month_totals']['shares']:,}")
        lines.append("---------------------------")

        total_new_videos += r["new_video_count"]
        total_month_videos += r["video_count"]
        for k in total_deltas:
            total_deltas[k] += r["deltas"][k]
            total_month[k] += r["month_totals"][k]

    lines.append("📌 TỔNG CỘNG TẤT CẢ TÀI KHOẢN")
    lines.append(f"— Tăng trưởng hôm qua {yesterday_str} —")
    lines.append(f"🎬 Video đăng mới: {total_new_videos}")
    lines.append(f"👁 Views: +{total_deltas['views']:,}")
    lines.append(f"❤️ Likes: +{total_deltas['likes']:,}")
    lines.append(f"💬 Comments: +{total_deltas['comments']:,}")
    lines.append(f"🔁 Shares: +{total_deltas['shares']:,}")
    lines.append(f"— Lũy kế tháng ({month_start_str} - {month_end_str}) —")
    lines.append(f"🎬 Số video trong tháng: {total_month_videos}")
    lines.append(f"👁 Tổng views: {total_month['views']:,}")
    lines.append(f"❤️ Tổng likes: {total_month['likes']:,}")
    lines.append(f"🔁 Tổng shares: +{total_month['shares']:,}")

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
    month_end_str = yesterday_str

    all_state = load_state()
    account_results = []

    for account in TIKTOK_ACCOUNTS:
        print(f"Đang xử lý tài khoản: {account['name']}...")
        try:
            result = process_account(account, all_state, now, month_key, today_str)
            account_results.append(result)
        except Exception as e:
            print(f"Lỗi khi xử lý tài khoản {account['name']}: {e}")
            # Không dừng toàn bộ script nếu 1 tài khoản lỗi, tiếp tục các tài khoản khác
            continue

    save_state(all_state)

    if not account_results:
        raise RuntimeError("Không xử lý được tài khoản nào, dừng lại không gửi báo cáo.")

    print("Đang gửi báo cáo qua Telegram...")
    report = build_report(yesterday_str, month_start_str, month_end_str, account_results)
    send_telegram_message(report)

    print("Hoàn tất.")


if __name__ == "__main__":
    main()
