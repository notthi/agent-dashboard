# Agent Dashboard

Claude Code・Codex・Antigravity のセッションを、1つの画面で一覧できるローカルのダッシュボードです。
それぞれが今なにをしているかを見て、そのチャットを開いたり、ここから返信したりできます。

> **非公式ツールです。** 各ツールがパソコン内に残している履歴ファイル（公開されていない内部形式）を読んでいます。
> どれかのツールがアップデートで形式を変えると、その列が読めなくなることがあります。

## できること

- **一覧**：Claude Code / Codex / Antigravity のセッションを列ごとに表示（動作中・開いている・終わったもの）。5秒ごとに自動更新
- **中身**：最後の指示と応答、使っているモデル、期間内のトークン数。長い応答は「全文」で展開、「応答をコピー」でコピー
- **使用制限**：Claude・Codex・Antigravity の 5時間枠・週の枠
- **開く**：ボタンで各アプリの該当チャットを開く
- **返信**：ダッシュボードから CLI 経由で続きを送り、返事をカードに表示。画像の貼り付け添付にも対応
- **ほかのマシン**：Tailscale でつながった自分の別のパソコン（Mac / Windows）のセッションも同じ画面に並べる
- **列のオン・オフ**、自動実行（サブエージェントや定期実行）の表示切り替え
- **Muse**（Meta）は会話がパソコンに残らないため、アプリを開くボタンだけを置いています（Windows はアプリがないので、ブラウザで Web 版 https://muse.ai/ を開きます）

## 必要なもの

- Python 3.8 以上（標準ライブラリだけで動きます。追加インストールは不要）
- macOS または Windows（Linux は一覧表示のみ想定）
- 使うツールの CLI：`claude`、`codex`、`agy`（入っていないツールの列は空になるだけです）
- ほかのマシンと共有するなら [Tailscale](https://tailscale.com/)

## 使い方

```bash
python3 server.py
```

ブラウザで http://localhost:8765 を開きます。

### ログイン時に自動で起動する

**Windows**

```bash
python server.py --install
```

スタートアップに登録され、次回のログインから `--share` 付きで自動起動します。外すときは `--uninstall`。

**macOS**（launchd）

`~/Library/LaunchAgents/com.example.agent-dashboard.plist` を作り、パスを自分の環境に合わせて書き換えます。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.agent-dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/path/to/agent-dashboard/server.py</string>
    <string>--share</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/tmp/agent-dashboard.log</string>
  <key>StandardErrorPath</key><string>/tmp/agent-dashboard.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.agent-dashboard.plist
```

## ほかのマシンのセッションも並べる

### Tailscale（おすすめ）

各マシンで `--share` を付けて起動するだけです。設定ファイルは要りません。

**準備（はじめの1回）**

1. 並べたいすべてのマシンに [Tailscale](https://tailscale.com/download) を入れる
2. どのマシンでも **同じ Tailscale アカウント** でログインする（別のアカウントの端末は、安全のため受け付けません）
3. `tailscale status` で、お互いが一覧に出てオンラインになっているか確かめる

**起動**

- 自分のセッションを **見せる側** のマシンで、`--share` を付けて起動します

  ```bash
  python3 server.py --share
  ```

- **見るだけの側** は、普通に `python3 server.py` で起動すれば十分です（相手のマシンを自動で見つけます）
- お互いに見せ合うなら、両方を `--share` で起動します
- 自動起動（上の「ログイン時に自動で起動する」）を使う場合、Windows の `--install` は最初から `--share` 付きです。Mac は plist の引数に `--share` を入れます

**Windows の注意**

初めて `--share` で起動したとき、Windows ファイアウォールの確認が出ることがあります。「許可」を押してください。許可しないと、ほかのマシンから見えません。

**表示されるまで**

相手のマシンで起動してから、こちらの画面の右上にマシン名が出るまで、数十秒かかることがあります。そのあとは 10 秒ごとに中身が更新されます。

**出てこないとき**

1. `tailscale status` で、相手のマシンがオンラインか確かめる
2. 相手のマシンで http://localhost:8765 が開けるか（ダッシュボードが動いているか）確かめる
3. 相手のマシンが `--share` 付きで起動しているか確かめる（起動時に「共有中: http://100.x.x.x:8765」と表示されます）
4. 同じ Tailscale アカウントでログインしているか確かめる
5. Windows なら、ファイアウォールで Python の通信が許可されているか確かめる

右上のマシン名に ✕ が付いているときは、マウスを乗せるとつながらない理由が表示されます。

**仕組みと安全**

- `--share` のマシンは、自分の Tailscale アドレスでも待ち受けます
- 受け付けるのは、`tailscale whois` で **自分と同じ Tailscale ユーザーの端末** と確認できた相手だけです
- 見る側は、Tailscale の端末一覧（スマホを除く、オンラインの自分の端末）にダッシュボードが動いているか問い合わせて、応答したマシンだけを並べます
- 共有の窓口でできるのは「一覧の取得」と「返信」だけです（アプリを開く操作などは外からは使えません）
- 別のマシンのセッションには、画像の添付と「アプリで開く」は使えません（Remote Control が付いた Claude のチャットは、claude.ai で開けます）

### SSH

`hosts.example.json` をまねて `hosts.json` を置くと、そのマシンへ SSH でつないで読み取ります。
相手側には何もインストール不要ですが、パスワードなしでつながる鍵認証が必要です。`hosts.json` は公開しないでください（`.gitignore` 済み）。

## 返信の仕組みと注意

| チャットの状態 | 「送信」の動き |
|---|---|
| アプリで閉じている | CLI で同じ会話の続きを実行し、返事をカードに表示 |
| アプリで開いたまま待機中（Mac） | **止めて送る**：アプリ側のそのチャットのプロセスだけを止めてから CLI で送る。会話は分かれません。送ったあと、Claude アプリの「表示 → 再読み込み」を自動で押すので、アプリの画面にも出ます（アプリが手前にあるときは押しません）。アプリには「中断」と出ます。**そのチャットの Remote Control は外れる**ので、必要ならアプリのツールバーでオンに戻してください。自動の再読み込みには、ダッシュボードの python に「アクセシビリティ」の許可が要ります |
| アプリで開いたまま待機中（Windows） | アプリでそのチャットを開き、貼り付けて Enter を押す（キー操作を送るので、その瞬間に別の窓が前に出ると誤送信の恐れがあります） |
| 処理中 | 送れません（止めると作業が失われるため） |

- アプリで開いたまま動いているチャットに CLI から直接書き込むと、会話が二股に分かれます。このため上のように使い分けています
- 「開いてコピー」を選ぶと、アプリで開いて本文をクリップボードに入れるだけにできます
- ダッシュボードからの返信は、各 CLI の **既定の権限** で動きます。ファイル編集など許可が必要な操作は、確認を出せないので止まります
- Codex は、アプリで開いているスレッドに CLI から書き込めないため、そのときは自動で「開いてコピー」に切り替わります
- Antigravity のアプリ（IDE）で作った会話は CLI から見えないため、返信できません

## Claude の使用制限（5時間枠・週の枠）

設定は要りません。ダッシュボードが 5 分おきに `claude -p --no-session-persistence /usage` を動かして取得します
（モデルは呼ばないので使用量は減りません。履歴ファイルも増えません）。デスクトップアプリだけを使っていても更新されます。

**おまけ：ステータスライン**

ターミナル版の Claude Code を使うなら、`statusline.py` をステータスラインに登録すると、画面下に「モデル · 5h 12% · 週 30%」と出て、
そのたびにダッシュボードの数字も更新されます（/usage の取得と新しい方を使います）。`~/.claude/settings.json` に次を追加します。

```json
"statusLine": {
  "type": "command",
  "command": "python3 -I /path/to/agent-dashboard/statusline.py"
}
```


## 安全について

- ダッシュボードは `127.0.0.1` でだけ待ち受けます。操作系の API は独自ヘッダー必須で、ほかのサイトから呼ばれないようにしています
- **インターネットに公開しないでください。** ダッシュボードはあなたのパソコンで CLI を実行して返信できます
- 貼り付けた画像は OS のキャッシュフォルダ（macOS: `~/Library/Caches/agent-dashboard/uploads`）に保存されます
- 外部に送るデータはありません（Tailscale / SSH でつないだ自分のマシンどうしを除く）

## exe 化（Windows）

```bash
pyinstaller --onefile --noconsole --add-data "index.html;." --add-data "server.py;." server.py
```

`hosts.json` を使う場合は exe と同じフォルダに置きます。

## ライセンス

MIT
