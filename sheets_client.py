"""スタッフロール投稿カウントをGoogle Sheetsへ書き込むモジュール。

「質問回答AI」プロジェクト(discord-qa-extractor)のsheets_client.pyと同じ
gspread + サービスアカウント認証の方式だが、認証情報(サービスアカウント)は
このプロジェクト専用に新規作成したものを使う(プロジェクト間で認証情報を
分離するため、2026-08-14導入)。
"""

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WORKSHEET_NAME = "投稿カウント"

# 実行ごとに追記していくログ形式(過去の推移が全部残る)。
HEADER = [
    "実行日時(JST)",
    "サーバー名",
    "ユーザー表示名",
    "該当ロール",
    "投稿数",
    "対象期間開始(JST)",
    "対象期間終了(JST)",
]


def connect(service_account_json_path: str, sheet_id: str):
    """サービスアカウントで認証し、対象サーバー設定シート(スプレッドシート全体)を開く。"""
    creds = Credentials.from_service_account_file(service_account_json_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def get_or_create_worksheet(spreadsheet):
    """「投稿カウント」ワークシートを開く。なければ作成しヘッダー行を書く。"""
    try:
        ws = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADER))
        ws.append_row(HEADER)
    return ws


def append_rows(ws, rows: list):
    """rowsは各要素がHEADERの列順に対応するリスト。

    table_rangeを明示しないと、I〜L列のQUERY集計結果もテーブルとして
    誤検出され、追記先がA〜G列からI列以降にずれる不具合が過去に発生した
    （2026-08-15〜2026-08-20の実行分がI〜O列に誤って書き込まれていた）。
    そのため、追記対象を必ずA〜G列に固定する。
    """
    if not rows:
        return
    ws.append_rows(
        rows,
        value_input_option="USER_ENTERED",
        table_range=f"A1:{chr(ord('A') + len(HEADER) - 1)}1",
    )
