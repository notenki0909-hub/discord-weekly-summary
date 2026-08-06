# discord-weekly-summary

複数のDiscordサーバーの指定カテゴリー・チャンネルに投稿された直近7日分の内容を読み取り、Gemini APIで日本語要約を作成して、指定チャンネルへ自動投稿する仕組み。GitHub Actions上で毎週月曜9:00(JST)に実行されるため、PCの電源が入っていなくても動作する。無料枠のGemini APIを使うため、追加の月額費用はかからない(想定利用量の範囲内)。

- スプレッドシート(対象サーバー設定): https://docs.google.com/spreadsheets/d/16DWNsBuiK6g2bPhfeIWKm9vIvghgNYesDeR_xK1i0DU/edit

## 対象サーバーの追加方法(GitHub操作は不要)

1. Botを対象サーバーに招待する(招待URLは`週次要約Bot`の記録を参照。権限:チャンネルを表示・メッセージ履歴を読む・メッセージを送る)
2. スプレッドシートに1行追加する

| 列 | 内容 | 例 |
|---|---|---|
| 有効／無効 | TRUE/FALSE | TRUE |
| サーバー名 | 表示用 | テクニカル分析講座 |
| サーバーID | | 1455712666692620300 |
| カテゴリーID | 複数はカンマ区切り、空欄可 | 1533475154066149626,1526881849421467728 |
| 追加チャンネルID | カテゴリー外の単独チャンネル、複数はカンマ区切り、空欄可 | |
| 投稿先チャンネルID | 要約を投稿するチャンネル | 1534837856839794808 |

次回の実行(毎週月曜)から自動的に対象に加わる。ワークフローの変更・再作成は不要。

## 動作の仕組み

1. `main.py` がスプレッドシートのCSV公開URLを読み込み、有効な行(サーバー)を抽出する
2. 各サーバーについて、指定カテゴリー配下の全チャンネル＋追加チャンネルの直近7日分の投稿をDiscord APIで取得する
3. チャンネルごとの投稿内容をまとめてGemini APIに渡し、日本語の要約を生成する
4. 生成した要約を、指定の投稿先チャンネルにDiscord APIで投稿する(2000文字を超える場合は分割投稿)

## 手動実行・ログの確認

- GitHubリポジトリの「Actions」タブ →「Discord Weekly Summary」→「Run workflow」で手動実行できる
- 各実行結果の「Artifacts」から `run-log` をダウンロードすると詳細ログを確認できる

## 認証情報(GitHub Secrets)

| Secret名 | 内容 | 取得先 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Bot Token | Discord Developer Portal → 対象アプリ →「Bot」→「Reset Token」 |
| `GEMINI_API_KEY` | Gemini APIキー(無料枠) | https://aistudio.google.com/apikey |
| `CONFIG_CSV_URL` | 対象サーバー設定シートのCSV公開URL | 上記スプレッドシートの `ファイル→共有→ウェブに公開`、またはURL末尾を`/export?format=csv`にしたもの |

いずれもリポジトリの Settings → Secrets and variables → Actions から登録・編集する。

## ローカルでのテスト実行

```
pip install -r requirements.txt
cp .env.example .env
# .env に各値を記入
python main.py
```
