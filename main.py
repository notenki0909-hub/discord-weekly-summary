import csv
import io
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# 実行環境のロケールによらずUTF-8で入出力する(GitHub Actions実行環境での文字化け対策)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CONFIG_CSV_URL = os.environ["CONFIG_CSV_URL"]

DISCORD_API = "https://discord.com/api/v10"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_API = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# データ行の先頭に何行のヘッダー(タイトル・説明・空白・列名ラベル)があるか
CSV_HEADER_ROWS = 5

JST = timezone(timedelta(hours=9))
WEEKDAY_MAP = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}


def parse_schedule_days(schedule_str: str) -> set:
    """「実行曜日」列の文字列を曜日番号の集合に変換する(月曜=0〜日曜=6)。
    空欄または「毎日」は全曜日を対象とする。"""
    s = (schedule_str or "").strip()
    if not s or s == "毎日":
        return set(range(7))
    days = set()
    for part in s.split(","):
        part = part.strip()
        if part in WEEKDAY_MAP:
            days.add(WEEKDAY_MAP[part])
    return days if days else set(range(7))


def compute_lookback_days(schedule_days: set, today_wd: int) -> int:
    """今日の実行が対象とすべき「何日分遡るか」を、設定された実行曜日から逆算する。
    例:月・木指定の場合、月曜実行時は4日分(木→月)、木曜実行時は3日分(月→木)。"""
    if len(schedule_days) >= 7:
        return 1  # 毎日実行の場合は前日から今日まで
    if len(schedule_days) == 1:
        return 7  # 週1回のみの場合は従来通り7日分

    sorted_days = sorted(schedule_days)
    prev = None
    for d in reversed(sorted_days):
        if d < today_wd:
            prev = d
            break
    if prev is None:
        prev = sorted_days[-1]  # 今日が最小の曜日 → 前週の最後の曜日まで遡る
    lookback = (today_wd - prev) % 7
    return lookback if lookback > 0 else 7

class JSTFormatter(logging.Formatter):
    """ログの時刻表示をJST(日本時間)にする(GitHub Actionsランナーの標準時刻はUTCのため)。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=JST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
_formatter = JSTFormatter("%(asctime)s JST [%(levelname)s] %(message)s")
_file_handler = logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8")
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger(__name__)


def discord_headers():
    return {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}


def fetch_config_rows():
    """公開CSVから対象サーバーの設定一覧を取得する。"""
    resp = requests.get(CONFIG_CSV_URL, timeout=30)
    resp.raise_for_status()

    # resp.encoding/resp.text ではなく、生バイトを明示的にUTF-8デコードする
    # (resp.encoding経由だと環境によって正しく反映されないことがあるため)
    text = resp.content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)[CSV_HEADER_ROWS:]

    servers = []
    for row in rows:
        if len(row) < 8:
            continue
        enabled = row[2].strip().upper() == "TRUE"
        name = row[3].strip()
        guild_id = row[4].strip()
        category_ids = [c.strip() for c in row[5].split(",") if c.strip()]
        extra_channel_ids = [c.strip() for c in row[6].split(",") if c.strip()]
        dest_channel_id = row[7].strip()
        schedule_str = row[8].strip() if len(row) > 8 else ""

        if not enabled or not guild_id or not dest_channel_id:
            continue

        servers.append(
            {
                "name": name,
                "guild_id": guild_id,
                "category_ids": category_ids,
                "extra_channel_ids": extra_channel_ids,
                "dest_channel_id": dest_channel_id,
                "schedule_days": parse_schedule_days(schedule_str),
            }
        )
    return servers


def get_target_channels(guild_id: str, category_ids: list, extra_channel_ids: list):
    resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=discord_headers(), timeout=30)
    resp.raise_for_status()
    channels = resp.json()

    matched = [
        {"id": c["id"], "name": c.get("name", "")}
        for c in channels
        if category_ids and c.get("parent_id") in category_ids
    ]
    matched_ids = {c["id"] for c in matched}

    name_by_id = {c["id"]: c.get("name", "") for c in channels}
    for cid in extra_channel_ids:
        if cid not in matched_ids:
            matched.append({"id": cid, "name": name_by_id.get(cid, cid)})
            matched_ids.add(cid)

    return matched


def fetch_recent_messages(channel_id: str, since: datetime, until: datetime):
    """channel_idから[since, until]範囲内の投稿を取得する(sinceより前に届いたら打ち切り)。"""
    messages = []
    before = None
    for _ in range(20):  # 安全のための上限(20回=最大2000件)
        params = {"limit": 100}
        if before:
            params["before"] = before
        resp = requests.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=discord_headers(),
            params=params,
            timeout=30,
        )
        if resp.status_code == 403:
            log.warning("channel %s: アクセス権限なし(403)。スキップします。", channel_id)
            return messages
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.5)
            continue
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        reached_start = False
        for m in batch:
            ts = datetime.fromisoformat(m["timestamp"])
            if ts > until:
                continue
            if ts < since:
                reached_start = True
                continue
            if m.get("content"):
                messages.append(
                    {
                        "author": m.get("member", {}).get("nick")
                        or m.get("author", {}).get("global_name")
                        or m.get("author", {}).get("username", "unknown"),
                        "content": m["content"],
                        "timestamp": ts,
                    }
                )

        before = batch[-1]["id"]
        if reached_start or len(batch) < 100:
            break
        time.sleep(0.5)

    messages.sort(key=lambda m: m["timestamp"])
    return messages


def summarize_with_gemini(server_name: str, since: datetime, until: datetime, channel_messages: dict) -> str:
    # 表示用の日付はJSTに変換する(since/untilはUTCで保持しているため)
    since_jst = since.astimezone(JST)
    until_jst = until.astimezone(JST)
    lines = [f"サーバー名: {server_name}", f"対象期間: {since_jst.strftime('%Y-%m-%d')} 〜 {until_jst.strftime('%Y-%m-%d')}", ""]
    any_posts = False
    for ch_name, msgs in channel_messages.items():
        if not msgs:
            continue
        any_posts = True
        lines.append(f"--- #{ch_name} ({len(msgs)}件) ---")
        for m in msgs:
            lines.append(f"[{m['author']}] {m['content']}")
        lines.append("")

    if not any_posts:
        return f"【{server_name}】{since_jst.strftime('%Y/%m/%d')}〜{until_jst.strftime('%Y/%m/%d')}の期間、対象チャンネルへの投稿はありませんでした。"

    raw_text = "\n".join(lines)
    # Gemini入力量が過大にならないよう安全に切り詰める(概算目安)
    max_chars = 120000
    if len(raw_text) > max_chars:
        raw_text = raw_text[:max_chars] + "\n...(以降省略)"

    prompt = (
        "以下はDiscordサーバーの複数チャンネルにおける、指定期間中の投稿ログです。"
        "日本語で「投稿要点まとめ」を作成してください。\n"
        "- 冒頭にサーバー名と対象期間を明記する\n"
        "- 投稿があったチャンネルごとに見出しを分ける\n"
        "- 主なトピック・質問と回答・盛り上がった話題を簡潔な箇条書きでまとめる\n"
        "- 全文引用ではなく要点を抽出する\n"
        "- Discordの投稿(2000文字制限)に収まるよう、全体で1600文字程度を目安に簡潔にする\n\n"
        f"{raw_text}"
    )

    resp = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                GEMINI_API,
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=120,
            )
            resp.raise_for_status()
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            log.warning("Gemini API呼び出しに失敗しました(%d/3回目): %s", attempt, exc)
            if attempt == 3:
                return f"⚠要約生成エラー: Gemini APIへの接続がタイムアウトしました。({server_name})"
            time.sleep(5 * attempt)
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        log.error("Gemini応答の解析に失敗しました: %s", data)
        return f"⚠要約生成エラー: Gemini APIの応答形式が想定外でした。({server_name})"


def post_to_discord(channel_id: str, content: str):
    content = f"@everyone\n{content}"
    chunks = split_for_discord(content)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        text = chunk if total == 1 else f"({i}/{total})\n{chunk}"
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={**discord_headers(), "Content-Type": "application/json"},
            json={"content": text},
            timeout=30,
        )
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.5)
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers={**discord_headers(), "Content-Type": "application/json"},
                json={"content": text},
                timeout=30,
            )
        if resp.status_code not in (200, 201):
            log.error("Discord投稿失敗 status=%s body=%s", resp.status_code, resp.text)
        time.sleep(0.5)


def split_for_discord(text: str, limit: int = 1900):
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def main():
    now_jst = datetime.now(timezone.utc).astimezone(JST)
    today_wd = now_jst.weekday()
    # 周期の起点/終点は実行時刻(数分〜数十分のズレが生じうる)ではなく、
    # 日本時間0:00固定とする(実行タイミングのズレによる投稿の重複・漏れを防ぐため)。
    period_end = now_jst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    servers = fetch_config_rows()
    log.info("対象サーバー数: %d", len(servers))

    for server in servers:
        if today_wd not in server["schedule_days"]:
            log.info("%s: 本日は実行曜日ではないためスキップします", server["name"])
            continue

        lookback_days = compute_lookback_days(server["schedule_days"], today_wd)
        since = period_end - timedelta(days=lookback_days)
        log.info(
            "処理開始: %s (guild_id=%s, 対象期間=%d日分, %s 〜 %s JST)",
            server["name"], server["guild_id"], lookback_days,
            since.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
            period_end.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        )
        try:
            channels = get_target_channels(
                server["guild_id"], server["category_ids"], server["extra_channel_ids"]
            )
            log.info("%s: 対象チャンネル数 %d", server["name"], len(channels))

            channel_messages = {}
            for ch in channels:
                msgs = fetch_recent_messages(ch["id"], since, period_end)
                channel_messages[ch["name"]] = msgs
                log.info("  #%s: %d件", ch["name"], len(msgs))
                time.sleep(0.5)

            summary = summarize_with_gemini(server["name"], since, period_end, channel_messages)
            post_to_discord(server["dest_channel_id"], summary)
            log.info("%s: 要約投稿完了", server["name"])
        except Exception:
            log.exception("%s の処理中にエラーが発生しました", server["name"])
        time.sleep(1)


if __name__ == "__main__":
    main()
