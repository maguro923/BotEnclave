# BotEnclave

複数の Discord Bot を管理する、セキュリティ重視・Docker 不要の Web パネルです。

English: [README.md](README.md)

BotEnclave は、小規模で信頼できるグループが 1 台の Linux ホストで複数の
Discord Bot を運用し、1 つの Web UI から管理するためのツールです。git や
zip からのデプロイ、起動・停止、ログ閲覧、対話コンソール、リリースの
ロールバックができ、各 Bot は専用の OS ユーザー、ファイル、シークレット
の中で互いに分離されます。

## 対象読者 — と対象でない用途

BotEnclave は**互いに素性の知れた小規模グループ**（管理者＋招待された
ゲスト）が、自分たちのホストで自分たちの Bot を動かすためのものです。
ゲストは自分の Bot の OS ユーザー範囲で任意のコードを実行できます。
つまり**ゲストは「コード実行を許可できる相手」でなければなりません**。
不特定多数向けの公開ホスティングサービスには使えません。

## 特長

- **Python (discord.py) ネイティブ、Docker 不要** — Bot は通常のプロセス
  として動作します。小規模 VPS、Raspberry Pi、Docker の使えない制限
  コンテナでも動きます。
- **Git ネイティブなデプロイ** — git URL または zip アップロードから
  デプロイ。リリースごとに仮想環境を構築し、入口ファイルを検証して
  `current` を原子的に切り替えます。前世代へのロールバックは 1 クリック。
  Bot 単位の署名付き Webhook でプッシュ時の自動デプロイもできます。
- **既定で分離** — Bot ごとに専用 OS ユーザー、厳格なディレクトリ権限、
  コンソール用の Bot 別 tmux サーバー、そして root が必要な少数の操作を
  固定の検証済み操作セットで実行する小さな特権ヘルパー。Web アプリ
  本体は root では動きません。
- **漏らさないシークレット** — Bot ごとの環境変数を 3 つのバックエンド
  で保持。systemd ホスト（標準）では systemd-creds で暗号化し、PID 1 が
  Bot のユニットへ復号します（`LoadCredentialEncrypted`）。パネルが平文を
  見ることはありません。保存した値は再表示されず、既知の値はログ表示で
  マスキングされます。
- **実効性のあるアカウント保護** — 招待制ユーザー、管理者／ゲストの
  2 ロールと全ルートでの所有権チェック（IDOR テスト済み）、Argon2id
  パスワード、使い切りバックアップコード付き TOTP 二要素認証、ログイン
  ロックアウト、ゲストクォータ、CSRF 対策、ハッシュチェーン付きで
  二重化された監査ログ。

## 要件

- Linux（または WSL2）
- Python 3.11+
- ホストに `tmux` と `git`
- systemd と sudo（サポートする構成は管理アプリを systemd で動かし、
  ヘルパーへは sudo 経由で到達します）

## インストール

```sh
git clone https://github.com/maguro923/BotEnclave.git
cd BotEnclave
sudo sh scripts/install.sh
```

インストーラが本番構成を一括でセットアップします。前提パッケージ
（Debian/Ubuntu）、`/usr/local/lib/botenclave/venv` へのパッケージ
導入（Bot ユーザーが読める場所です。ランチャ shim と tmux 設定を
そこから読みます）、管理ユーザー `botenclave`、データルート
`/srv/botenclave`、systemd ユニット、初回起動までを行います。
冪等で、再実行できます。手作業の手順は
[docs/setup.md](docs/setup.md) にあります。

## 最初のステップ

1. パネルは `127.0.0.1:8000` でのみ、HTTPS で待ち受けます。
   ホスト上では `https://127.0.0.1:8000/` を開き、別のマシン
   からは SSH 転送
   （`ssh -L 8000:127.0.0.1:8000 <ユーザー>@<ホスト>`）してから
   同じアドレスを開くか、`access.toml` に自分のマシンの
   アドレスを許可リストとして登録すると、初回セットアップへ
   誘導されます。
   証明書はホスト上で生成される自己署名のため、ブラウザの警告が
   一度表示されます。
   セットアップトークンはインストール実行の最後に表示され、
   `/srv/botenclave/setup-token`（権限 600）にも保存されます。
   アクセス方式の考え方は [docs/setup.md](docs/setup.md) の
   セクション6を参照してください。
2. 管理者アカウントを作成してサインインします。
3. ダッシュボードで **Bot を追加**（名前＋コマンド）し、Bot のページの
   **Deploy** セクションからコードをデプロイします（git URL または
   zip）。入口ファイル（`main.py`、`bot.py`、`app.py`、`run.py`）を
   検出し、`requirements.txt` から仮想環境を構築して起動コマンドを
   そちらへ向けます。
4. **Environment variables** セクションで環境変数（例: `DISCORD_TOKEN`）
   を設定し、**Start** を押します。
5. ログの閲覧や対話コンソールが使えます。コンソール接続中はログ捕捉が
   止まる（プライベートモード）ため、そこで打った資格情報がログファイル
   に残りません。

信頼できる LAN の外では HTTPS（または TLS 終端プロキシ配下）で公開して
ください。セッション Cookie は既定で `Secure` で、`--no-cookie-secure`
はローカル開発専用です。

## 構成（systemd）

セキュリティ設計が前提とする構成（管理アプリは専用の非特権ユーザー、
ヘルパーは狭い sudoers ルール経由、Bot ごとに systemd ユニット）は
[docs/systemd.md](docs/systemd.md) を参照してください。

## シークレットバックエンド

Bot ごとに Environment セクションで選択します。

| バックエンド | 保存先 | 備考 |
|---|---|---|
| systemd-creds（標準） | ホスト鍵で暗号化した `.env` | 起動時にヘルパーが復号、systemd ユニットでは PID 1 が復号。ホスト鍵に束縛されるため、ホスト移行後は復号できません。 |
| Fernet | 管理側の鍵ファイルで暗号化した `.env` | systemd のない環境向けフォールバック。鍵ファイルと保存先の両方を取られると復号される弱さがあります。 |
| メモリのみ | ディスクに残さない | 管理プロセス再起動で値は失われ、再入力まで Bot は `secret-pending` で待機します。 |

環境変数を読めない Bot 向けに、起動時に値を `data/.env` へ書き出す
dotenv 互換モードもあります。平文がディスクに残るため既定オフで、
有効時は画面に表示され続けます。

## ドキュメント

- [docs/setup.md](docs/setup.md) — 前提条件から最初の Bot 起動までの
  セットアップ手順
- [docs/architecture.md](docs/architecture.md) — 設計原則、プロセス
  モデル、セキュリティ設計のまとめ
- [docs/systemd.md](docs/systemd.md) — systemd による本番導入
- [docs/backup.md](docs/backup.md) — バックアップ、リストア、監査の保持
- [docs/upgrading.md](docs/upgrading.md) — Bot を止めないアップグレード
- [SECURITY.md](SECURITY.md) — 脆弱性の報告方法と脅威モデルの要約

## 開発

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # 全テスト（特権テストは root が必要）
.venv/bin/ruff check botmgr tests
.venv/bin/mypy botmgr
```

開発は TDD で進めています。セキュリティ要件には拒否確認のテスト
（他ユーザーの Bot への全経路でのアクセス拒否、悪意ある Bot や
アーカイブの脱出不能）が付属します。CI は GitHub の標準ランナーで
同じチェックを実行します。

## ライセンス

Apache License 2.0 — [LICENSE](LICENSE) を参照してください。
