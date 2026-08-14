"""Discordメッセージ内の個人情報をマスキングするモジュール。

「質問回答AI」プロジェクト(discord-qa-extractor)で作成した仕組みを移植したもの。
週次要約Botは複数サーバーを一括処理する仕様のため、サーバー固有のロスター
(受講生名簿)・除外リストは使わず、以下の汎用的な方式のみを適用する(2026-08-14)。

  ① 定型パターンのマスキング: メールアドレス・電話番号・Discordメンション
  ② Sudachi辞書の人名タグ(姓/名)によるマッチング
  ⑤ 自己紹介の定型文パターン(「〇〇と申します」等)

投稿者名(ニックネーム等)は、Discordの投稿者IDから決定論的に生成した匿名ラベルに
置き換える(AuthorPseudonymizer)。同一人物は常に同じラベルになる。
"""

import hashlib
import json
import re
from pathlib import Path

from sudachipy import dictionary as sudachi_dictionary
from sudachipy import tokenizer as sudachi_tokenizer

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 日本の電話番号らしい形式(0始まりのハイフン区切り、または0/+81始まりの10〜11桁の連続数字)のみを対象とする。
# 前後に(?<!\d)/(?!\d)を付け、Discordのスノーフレークid(17〜19桁)等、長い数字列の一部を
# 誤って部分マッチしないようにする。
PHONE_RE = re.compile(
    r"(?<!\d)(?:0\d{1,4}-\d{1,4}-\d{3,4}|0\d{9,10}|\+81-?\d{1,4}-?\d{1,4}-?\d{3,4})(?!\d)"
)
MENTION_RE = re.compile(r"<@!?\d+>")

# ⑤ 自己紹介の定型文。「と申します/といいます」は名前を名乗る文脈以外でほぼ使われないため
# 誤検出が少ない。「名前は〇〇です」も比較的限定的な文脈。
NAME_INTRO_PATTERNS = [
    re.compile(r"名前は(?P<name>[一-龥ァ-ヶーa-zA-Zａ-ｚＡ-Ｚ]{1,10})です"),
    re.compile(r"(?P<name>[一-龥ァ-ヶーa-zA-Zａ-ｚＡ-Ｚ]{1,10})と申します"),
    re.compile(r"(?P<name>[一-龥ァ-ヶーa-zA-Zａ-ｚＡ-Ｚ]{1,10})といいます"),
]

_sudachi_tokenizer = None


def _get_sudachi_tokenizer():
    global _sudachi_tokenizer
    if _sudachi_tokenizer is None:
        _sudachi_tokenizer = sudachi_dictionary.Dictionary().create()
    return _sudachi_tokenizer


def mask_names_by_dict(text: str) -> str:
    """Sudachi辞書の人名タグ(品詞細分類=人名)が付いた形態素をマスクする。

    姓・名のように隣接する人名形態素は1つの[MASKED_NAME]にまとめる。
    """
    tok = _get_sudachi_tokenizer()
    mode = sudachi_tokenizer.Tokenizer.SplitMode.C
    morphemes = tok.tokenize(text, mode)

    result = []
    i = 0
    n = len(morphemes)
    while i < n:
        pos = morphemes[i].part_of_speech()
        if len(pos) >= 3 and pos[2] == "人名":
            j = i + 1
            while (
                j < n
                and len(morphemes[j].part_of_speech()) >= 3
                and morphemes[j].part_of_speech()[2] == "人名"
                and morphemes[j].begin() == morphemes[j - 1].end()
            ):
                j += 1
            result.append("[MASKED_NAME]")
            i = j
        else:
            result.append(morphemes[i].surface())
            i += 1
    return "".join(result)


def mask_names_by_intro_pattern(text: str) -> str:
    """自己紹介の定型文パターンに一致する氏名をマスクする。"""

    def _sub(m):
        return m.group(0).replace(m.group("name"), "[MASKED_NAME]")

    for pattern in NAME_INTRO_PATTERNS:
        text = pattern.sub(_sub, text)
    return text


def mask_content(text: str) -> str:
    """メッセージ本文中の個人情報をマスクする。

    処理順序が重要:
    1. メンション(<@数字ID>)を最初に除去する。後回しにすると電話番号regexが
       メンション内の数字IDを誤って部分マッチしうる。
    2. 氏名マスキング(②辞書 → ⑤自己紹介パターン)をメール・電話番号のマスキングより
       先に行う。逆にすると"[MASKED_PHONE]"等のプレースホルダー文字列自体を
       Sudachiが誤って人名扱いし、二重マスクが発生する。
    3. 最後にメール・電話番号をマスクする。
    """
    text = MENTION_RE.sub("[MENTION]", text)
    text = mask_names_by_dict(text)
    text = mask_names_by_intro_pattern(text)
    text = EMAIL_RE.sub("[MASKED_EMAIL]", text)
    text = PHONE_RE.sub("[MASKED_PHONE]", text)
    return text


class AuthorPseudonymizer:
    """DiscordユーザーIDを匿名ラベルに変換し、対応表をローカルに保持する。

    対応表(author_id -> pseudonym)自体は個人情報ではないが、実名(ニックネーム)と
    紐付けて悪用されないようgitignore対象のdataディレクトリに保存する。
    """

    def __init__(self, map_path: Path, salt: str):
        self._map_path = map_path
        self._salt = salt
        self._map: dict[str, str] = {}
        if map_path.exists():
            self._map = json.loads(map_path.read_text(encoding="utf-8"))

    def pseudonymize(self, author_id: str) -> str:
        if author_id not in self._map:
            digest = hashlib.sha256(f"{self._salt}:{author_id}".encode()).hexdigest()[:8]
            self._map[author_id] = f"参加者_{digest}"
        return self._map[author_id]

    def save(self):
        self._map_path.parent.mkdir(parents=True, exist_ok=True)
        self._map_path.write_text(
            json.dumps(self._map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
