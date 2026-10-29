#!/usr/bin/env python3
"""Claude / Codex / Antigravity のセッションを一覧するローカルダッシュボード。

一覧は各ツールがローカルに残している履歴を読むだけ。
カードから各アプリの該当チャットを開いたり、CLI 経由で返信したりできる。
  python3 server.py            -> http://localhost:8765
  python3 server.py --share    -> 上に加え、Tailscale の自分の端末にだけこのマシンの一覧を見せる
  python server.py --install   -> Windows: ログイン時に --share 付きで自動起動するよう登録
"""
import base64
import gzip
import hashlib
import errno
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CODEX_DIR = HOME / ".codex"
AGY_DIR = HOME / ".gemini" / "antigravity"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller で exe 化したとき
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # 同梱した index.html の場所
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent  # hosts.json を置く場所
PORT = int(os.environ.get("PORT", "8765"))
# pythonw から CLI などを起動すると空の黒いウインドウが出るので、出さないようにする
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}

if IS_MAC:
    CACHE_DIR = HOME / "Library/Caches/agent-dashboard"
    CLAUDE_APP_DIR = HOME / "Library/Application Support/Claude"
elif IS_WIN:
    CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local")) / "agent-dashboard"
    CLAUDE_APP_DIR = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming")) / "Claude"
else:
    CACHE_DIR = HOME / ".cache/agent-dashboard"
    CLAUDE_APP_DIR = HOME / ".config/Claude"

# アプリ経由や自動起動ではシェルの PATH を引き継がないことがあるので CLI の置き場所を足す
_EXTRA_PATH = [HOME / ".local/bin"]
if IS_WIN:
    _EXTRA_PATH += [Path(os.environ.get("APPDATA", "")) / "npm", HOME / ".bun/bin",
                    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/agy"]
else:
    _EXTRA_PATH += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
CLI_PATH = os.pathsep.join([*map(str, _EXTRA_PATH), os.environ.get("PATH", "")])
CLI_ENV = {**os.environ, "PATH": CLI_PATH, "NO_COLOR": "1"}


def cli(name):
    """CLI のフルパス。Windows の npm 製 CLI は .cmd なので名前だけでは起動できない"""
    return shutil.which(name, path=CLI_PATH) or name


def open_url(url):
    if IS_WIN:
        os.startfile(url)  # noqa: S606  既定のアプリでカスタム URL を開く
    else:
        subprocess.run(["open" if IS_MAC else "xdg-open", url], check=True)


def copy_text(text):
    if IS_WIN:
        # clip.exe は BOM 付き UTF-16 なら日本語も通る
        subprocess.run(["clip"], input=text.encode("utf-16"), check=True, **NO_WINDOW)
    elif IS_MAC:
        # LANG が無いと pbcopy が UTF-8 として扱わず日本語が化ける
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True,
                       env={**os.environ, "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8"})
    else:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)

ACTIVE_WINDOW = 120  # 秒。これ以内に更新があれば「動作中」とみなす
TAIL_BYTES = 400_000


def now():
    return time.time()


def claude_window(model, tokens):
    """モデルのコンテキストの上限。記録に上限は残らないので、モデル名から決める（推定）。
    Claude 5 系は 100 万、それ以外は 20 万。実際の量が上限を超えていれば 100 万とみなす"""
    m = (model or "").lower()
    window = 1_000_000 if ("[1m]" in m or m.startswith(("claude-opus-5", "claude-sonnet-5", "claude-fable", "claude-haiku-5"))) else 200_000
    return 1_000_000 if tokens > window else window


def head_cwd(path, nbytes=2_000_000):
    """履歴の先頭から、最初に記録された作業フォルダを探す"""
    try:
        with open(path, "rb") as f:
            data = f.read(nbytes)
    except OSError:
        return None
    for raw in data.split(b"\n"):
        if b'"cwd"' not in raw:
            continue
        try:
            cwd = json.loads(raw).get("cwd")
        except ValueError:
            continue
        if cwd:
            return cwd
    return None


def tail_lines(path, nbytes=TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > nbytes:
        lines = lines[1:]  # 途中から始まる行は捨てる
    out = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


def clip(text, n=240):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


FULL_MAX = 50_000


def full_text(text):
    """短縮表示と違うときだけ全文（改行は残す）を返す。カードの「全文」用"""
    text = (text or "").strip()
    if not text or clip(text) == " ".join(text.split()):
        return None
    return text[:FULL_MAX]


def ro_connect(path):
    # as_uri() で Windows のドライブ文字や空白も正しく URI になる
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
        # Windows の os.kill(pid, 0) は確認ではなくプロセスを終了させてしまうので使わない
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------- Claude

def claude_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def access_request(c):
    """画面操作（computer use）の許可の求め。答えるまで履歴には求めた行しか残らない（実行中と見分けられる唯一の許可）"""
    if not str(c.get("name", "")).endswith("request_access"):
        return None
    inp = c.get("input") or {}
    apps = [os.path.basename(str(a)) for a in inp.get("apps") or []]
    return "画面操作の許可を待っています" + (f": {', '.join(apps)}" if apps else "") + (f"\n{inp['reason']}" if inp.get("reason") else "")


def claude_question(content):
    """アシスタントの質問を (文, 種類) で返す。質問がなければ (None, None)。
    "choice": AskUserQuestion の選択式。答えは tool_result で返り、アプリの画面で選んでもらう
    "box": デスクトップアプリの、ターンの最後に出る「次の一手」の箱の質問（wrap_up の ask）。答えは普通の返信"""
    for c in content if isinstance(content, list) else []:
        if not (isinstance(c, dict) and c.get("type") == "tool_use"):
            continue
        inp = c.get("input") or {}
        if c.get("name") == "AskUserQuestion":
            qs = inp.get("questions") or []
            return "\n".join(
                (q.get("question") or "") + "\n選択肢: " + " / ".join(o.get("label", "") for o in q.get("options") or [])
                for q in qs if isinstance(q, dict)), "choice"
        if str(c.get("name", "")).endswith("wrap_up") and inp.get("ask"):
            labels = [a.get("label", "") for a in inp.get("actions") or [] if isinstance(a, dict) and a.get("label")]
            return inp["ask"] + ("\n選択肢: " + " / ".join(labels) if labels else ""), "box"
    return None, None


def stop_idle_session(sid):
    """アプリが開いたまま待機しているセッションのプロセスを止める。処理中なら止めない"""
    lv = claude_live().get(sid)
    if lv is None:
        return  # もう止まっている
    if lv.get("status") != "idle":
        raise ValueError("このチャットは処理中です。終わってから送ってください")
    pid = int(lv["pid"])
    if IS_WIN:
        # 子プロセス（MCP サーバーなど）ごと止める
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **NO_WINDOW)
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    raise ValueError("アプリ側のセッションを止められませんでした")


def claude_live():
    """~/.claude/sessions/<pid>.json = 起動中のプロセス"""
    live = {}
    for f in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_bytes())  # bytes なら Windows でも UTF-8 として読まれる
        except (OSError, ValueError):
            continue
        if not pid_alive(d.get("pid", 0)):
            continue
        live[d.get("sessionId")] = d
    return live


# ---------------------------------------------------------------- Remote Control の記録
# 起動中のチャットの Remote Control（bridgeSessionId）が付いたり外れたりした時刻を残す。
# 切れる原因を、そのとき何をしていたか（送信・スリープ・起動し直し）と突き合わせて探すため
RC_LOG_FILE = CACHE_DIR / "rc_log.jsonl"
RC_SEEN = {}  # sessionId -> {"bridge", "name", "pid"}
RC_INTERVAL = 15  # 秒。session ファイルを読むだけなので軽い


def rc_record(event, sid, info):
    row = {"ts": now(), "event": event, "id": sid, "name": info.get("name") or sid[:8],
           "bridge": (info.get("bridge") or "")[-6:], "status": info.get("status")}
    try:
        RC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RC_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def rc_check(first=False):
    cur = {sid: {"bridge": lv.get("bridgeSessionId"), "name": lv.get("name"), "pid": lv.get("pid"),
                 "status": lv.get("status")} for sid, lv in claude_live().items()
           # claude -p（/usage の取得やダッシュボードからの返信）は Remote Control と関係ないので数えない
           if sid and lv.get("kind", "interactive") == "interactive"}
    if not first:
        for sid, info in cur.items():
            prev = RC_SEEN.get(sid)
            if prev is None or prev["pid"] != info["pid"]:
                rc_record("start-on" if info["bridge"] else "start-off", sid, info)  # 起動・開き直し
            elif prev["bridge"] and not info["bridge"]:
                rc_record("off", sid, info)
            elif info["bridge"] and info["bridge"] != prev["bridge"]:
                rc_record("on", sid, info)
        for sid, prev in RC_SEEN.items():
            if sid not in cur:
                rc_record("end", sid, prev)  # プロセスが終わった（閉じた・止めた・落ちた）
    RC_SEEN.clear()
    RC_SEEN.update(cur)


def rc_watch():
    first = True
    while True:
        try:
            rc_check(first)
            first = False
        except Exception:  # noqa: BLE001  記録が失敗しても本体は止めない
            pass
        time.sleep(RC_INTERVAL)


def rc_log(limit=200):
    try:
        lines = RC_LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def rc_log_all(limit=200):
    """このPCと、Tailscale でつながった端末の記録をまとめて新しい順に返す"""
    rows = [dict(r, host=None) for r in rc_log(limit)]
    for host in ts_hosts():
        if REMOTE.get(host["name"], {}).get("data") is None:
            continue  # ダッシュボードが応答していない端末は聞かない
        try:
            rows += [dict(r, host=host["name"]) for r in ts_get(host["ts"], f"/share/rclog?limit={limit}").get("events", [])]
        except (OSError, ValueError):
            pass
    return sorted(rows, key=lambda r: -r.get("ts", 0))[:limit]


_USAGE_CACHE = {}  # path -> ((size, mtime), 結果)。履歴は追記だけなので変わったものだけ読み直す


def cached_by_stat(path, fn):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    ck = (path, fn.__name__)  # 同じファイルを別の関数でも読むので、関数ごとに覚える
    hit = _USAGE_CACHE.get(ck)
    if hit and hit[0] == key:
        return hit[1]
    val = fn(path)
    _USAGE_CACHE[ck] = (key, val)
    return val


def iso_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def add_tokens(days, ts, inp, out, cache):
    """日付(ローカル)ごとに積む。期間で絞るときは日単位"""
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    t = days.setdefault(day, {"input": 0, "output": 0, "cache": 0})
    t["input"] += inp
    t["output"] += out
    t["cache"] += cache


def tokens_since(days, since):
    first = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
    tot = {"input": 0, "output": 0, "cache": 0}
    for day, t in (days or {}).items():
        if day >= first:
            for k in tot:
                tot[k] += t[k]
    return tot


def claude_usage(path):
    """日ごとのトークン数と最後に使ったモデル。同じ応答が複数行に分かれるので message.id で重複を除く"""
    seen = {}
    model = None
    with open(path, "rb") as f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if d.get("type") != "assistant" or not u:
                continue
            if m.get("model") and not m["model"].startswith("<"):
                model = m["model"]
            seen[m.get("id") or d.get("uuid")] = (iso_ts(d.get("timestamp")), u)
    days = {}
    for ts, u in seen.values():
        add_tokens(days, ts,
                   (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                   u.get("output_tokens") or 0,
                   u.get("cache_read_input_tokens") or 0)
    return {"model": model, "days": days}


_CLAUDE_INC = {}  # path -> {"offset", "seen", "model"}。履歴は追記だけなので、前回の続きから読む


def claude_usage_inc(path):
    """claude_usage と同じ結果を、書き足された分だけ読んで出す。動作中のチャットは数秒ごとに追記されるので、
    数百 MB の履歴を毎回頭から読み直すと、一覧の返事が数十秒かかっていた"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    st = _CLAUDE_INC.get(path)
    if st is None or size < st["offset"]:  # 初めて、または書き直された
        st = _CLAUDE_INC[path] = {"offset": 0, "seen": {}, "model": None}
    if size > st["offset"]:
        # 1行ずつ読む（初回は数百 MB になるので、まとめて読むとメモリを食う）
        with open(path, "rb") as f:
            f.seek(st["offset"])
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # 書きかけの最後の行は、次に回す
                st["offset"] += len(raw)
                if b'"usage"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if d.get("type") != "assistant" or not u:
                    continue
                if m.get("model") and not m["model"].startswith("<"):
                    st["model"] = m["model"]
                # 使う数だけ覚える（usage をまるごと持つと、大きな履歴でメモリを食う）
                st["seen"][m.get("id") or d.get("uuid")] = (
                    iso_ts(d.get("timestamp")),
                    (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                    u.get("output_tokens") or 0,
                    u.get("cache_read_input_tokens") or 0)
    days = {}
    for ts, inp, out, cache in st["seen"].values():
        add_tokens(days, ts, inp, out, cache)
    return {"model": st["model"], "days": days}


def codex_usage(path):
    """rollout の token_count から日ごとのトークン数と、最新の使用制限(5時間/週)を取る"""
    days, limits, context = {}, None, None
    with open(path, "rb") as f:
        for raw in f:
            if b'"token_count"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            p = d.get("payload") or {}
            ts = iso_ts(d.get("timestamp"))
            info = p.get("info") or {}
            u = info.get("last_token_usage")
            if u:
                # 最後の呼び出しの入力（キャッシュ分を含む）が、その時点のコンテキストの量
                context = {"tokens": u.get("input_tokens") or 0, "window": info.get("model_context_window")}
                cached = u.get("cached_input_tokens") or 0
                add_tokens(days, ts, (u.get("input_tokens") or 0) - cached,
                           u.get("output_tokens") or 0, cached)
            rl = p.get("rate_limits")
            if rl and rl.get("primary"):
                limits = {"ts": ts, "primary": rl.get("primary"), "secondary": rl.get("secondary")}
    return {"days": days, "limits": limits, "context": context}


DESKTOP_SESSIONS = CLAUDE_APP_DIR / "claude-code-sessions"


DESKTOP_TITLES = {}  # CLI のセッション ID -> アプリでの題名。貼り付け前に、画面のチャットと照らし合わせる


def claude_desktop_ids():
    """CLI のセッション ID -> デスクトップアプリ側の ID (local_…)。両者は一致しないことがある"""
    ids = {}
    for f in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_bytes())
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId") and not d.get("isArchived"):
            ids[d["cliSessionId"]] = d.get("sessionId") or f.stem
            DESKTOP_TITLES[d["cliSessionId"]] = d.get("title") or ""
    return ids


def collect_claude(since):
    live = claude_live()
    desktop = claude_desktop_ids()
    items = {}
    for f in (CLAUDE_DIR / "projects").glob("*/*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        sid = f.stem
        if mtime < since and sid not in live:
            continue
        title = prompt = reply = cwd = entry = question = qkind = None
        sidechain_only = True
        turn_done = False  # 最後の発言が「応答し終えた」アシスタントか
        # 最後の発言の時刻。ファイルの更新時刻は、アプリが題名などを書き足すだけでも新しくなるので並び順に使わない
        msg_ts = None
        ctx = None  # 最後の応答の時点のコンテキストの量（入力 + キャッシュ読込 + キャッシュ作成）
        last_said = None  # 最後に文を出したのが "user"（指示）か "assistant"（応答）か
        tail_model = None  # 末尾の応答のモデル。トークン数を数えないときもモデル名は出す
        pending_access = {}  # tool_use の id -> 許可を求める文（tool_result が来たら消す）
        turn_msgs = []  # いまのターン（最後の指示のあと）にアシスタントが出した文。動作中のカードにログとして出す
        for d in tail_lines(f):
            t = d.get("type")
            entry = d.get("entrypoint") or entry
            if t in ("user", "assistant") and not d.get("isSidechain"):
                turn_done = t == "assistant" and (d.get("message") or {}).get("stop_reason") in (
                    "end_turn", "stop_sequence", "max_tokens", "refusal")
                msg_ts = iso_ts(d.get("timestamp")) or msg_ts
            if t == "custom-title":
                title = d.get("customTitle") or title
            elif t == "agent-name":
                title = title or d.get("agentName")
            elif t == "last-prompt":
                prompt = d.get("lastPrompt") or prompt
            elif t == "user":
                cwd = d.get("cwd") or cwd
                if not d.get("isSidechain"):
                    sidechain_only = False
                    content = d.get("message", {}).get("content")
                    for c in content if isinstance(content, list) else []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            pending_access.pop(c.get("tool_use_id"), None)
                    txt = claude_text(content)
                    # 選択式は tool_result が来たら答えた。箱の質問は、人が文を送ったら答えた
                    # （箱の tool_result はターンの途中ですぐ返るので、それでは消さない）
                    if qkind == "choice" or txt.strip():
                        question = qkind = None
                    if txt and not txt.lstrip().startswith("<"):
                        prompt = txt
                        last_said = "user"
                        turn_msgs = []
            elif t == "assistant" and not d.get("isSidechain"):
                mm = (d.get("message") or {}).get("model") or ""
                if mm and not mm.startswith("<"):
                    tail_model = mm
                u = (d.get("message") or {}).get("usage") or {}
                if u:
                    ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                txt = claude_text(d.get("message", {}).get("content"))
                if txt.strip():
                    reply = txt
                    last_said = "assistant"
                    turn_msgs.append({"ts": iso_ts(d.get("timestamp")), "text": clip(txt, 200)})
                for c in d.get("message", {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and access_request(c):
                        pending_access[c.get("id")] = access_request(c)
                q, k = claude_question(d.get("message", {}).get("content"))
                if q:
                    question, qkind = q, k
        lv = live.get(sid)
        if lv:
            cwd = lv.get("cwd") or cwd
            title = lv.get("name") or title
            # 質問（AskUserQuestion）への答え待ちは、履歴に未回答の質問が残っていることで見分ける
            # 画面操作の許可を求めたまま答えがなければ、処理中に見えても人の答え待ち（アプリで許可する）
            if pending_access and lv.get("status") == "busy":
                question, qkind = list(pending_access.values())[-1], "choice"
            # 箱の質問は、ターンが終わって待っているときだけ質問中とみなす
            asking = question and (qkind == "choice" or lv.get("status") != "busy")
            state = "ask" if asking else "busy" if lv.get("status") == "busy" else "open"
            # 動作中は今まさに動いているので起動中プロセスの時刻も使う。待機中は最後の発言の時刻
            updated = msg_ts or mtime
            if state == "busy":
                updated = max(updated, (lv.get("updatedAt") or 0) / 1000)
        else:
            # 最近書かれていても、応答し終えていれば待機中。ダッシュボードから送信中のものは処理中
            sending = any(j["session"] == sid and j["status"] == "running" for j in list(JOBS.values()))
            state = "busy" if sending or (now() - mtime < ACTIVE_WINDOW and not turn_done) else "done"
            updated = msg_ts or mtime
            if updated < since and state != "busy":
                continue  # 中身が書き足されただけの古いチャットは、期間の一覧に出さない
        if cwd is None:
            # 末尾だけでは作業フォルダが分からない（大きな履歴）ときは、先頭から探す。
            # フォルダ名から推すと日本語が「-」に化けて存在しないパスになり、返信が別のフォルダで動いてしまう
            cwd = cached_by_stat(str(f), head_cwd) or "/" + f.parent.name.lstrip("-").replace("-", "/")
        # トークン数を数えない設定のときは、履歴を頭から読まない（モデル名は末尾から取る）
        usage = (claude_usage_inc(str(f)) or {}) if COUNT_TOKENS else {}
        model = usage.get("model") or tail_model
        items[sid] = {
            "model": model,
            "tokens": tokens_since(usage.get("days"), since),
            "context": {"tokens": ctx, "window": claude_window(model, ctx)} if ctx else None,
            # 新しい指示にまだ応答の文が出ていない（いまの reply は前のターンのもの）。画面では古い応答を隠して待ちを出す
            "awaiting": state == "busy" and last_said == "user",
            # 動作中のターンで、いま出ている応答より前に出した文（新しい順・8件まで）
            "turn_log": turn_msgs[-9:-1][::-1] if state in ("busy", "ask") else [],
            "tool": "claude",
            "id": sid,
            "title": title or clip(prompt, 60) or "(無題)",
            "cwd": cwd,
            "updated": updated,
            "state": state,
            "prompt": clip(prompt),
            "reply": clip(reply),
            "prompt_full": full_text(prompt),
            "reply_full": full_text(reply),
            "question": question if state == "ask" else None,
            "auto": bool(lv is None and sidechain_only),
            "via": (lv or {}).get("entrypoint") or entry or "",
            "live": lv is not None,
            "desktop_id": (lv or {}).get("hostSessionId") or desktop.get(sid),
            # Remote Control で claude.ai からも操作できるセッションの ID（別マシンから開く・返信するのに使う）
            "bridge_id": (lv or {}).get("bridgeSessionId"),
            # 開いているセッションへ CLI から書き込むと履歴が分岐するので、
            # その場合はアプリで開いて本文をクリップボードに入れるだけにする
            "reply_mode": "paste" if lv else "cli",
            # 質問の画面に文字を貼ると答え方が読めないので、質問中はアプリで答えてもらう
            # 箱の質問には普通の返信で答えられるので、選択式のときだけ止める
            "can_reply": not (state == "ask" and qkind == "choice") and (lv is not None or state != "busy"),
            "no_reply": "質問が出ています。アプリで開いて答えてください" if state == "ask" and qkind == "choice" else None,
            # 待機中ならアプリ側のプロセスを止めてから CLI で送れる（会話は1本のまま。アプリには「中断」と出る）
            "can_stop_send": lv is not None and (state == "open" or (state == "ask" and qkind == "box")),
            # Windows ではアプリでそのチャットを開き、貼り付けて Enter まで自動で押せる（止めずに同じ会話へ届く）
            # Mac は止めている。自動起動から Claude アプリの画面情報を問い合わせると、長時間返ってこないことがあり、
            # 宛先の確認を安定してできないため（誤送信を避けて「止めて送る」を既定にする）
            "auto_paste": IS_WIN and lv is not None and bool((lv or {}).get("hostSessionId") or desktop.get(sid)),
        }
    return sorted(items.values(), key=lambda x: -x["updated"])


# ---------------------------------------------------------------- Codex

def codex_rollout(path):
    prompt = reply = None
    running = False
    for d in tail_lines(path):
        p = d.get("payload") or {}
        pt = p.get("type")
        if d.get("type") == "event_msg":
            if pt == "task_started":
                running = True
            elif pt in ("task_complete", "turn_aborted"):
                running = False
            elif pt == "user_message":
                prompt = p.get("message") or prompt
            elif pt == "agent_message":
                reply = p.get("message") or reply
        elif d.get("type") == "response_item" and pt == "message":
            txt = "\n".join(
                c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)
            )
            if not txt.strip() or txt.lstrip().startswith("<"):
                continue
            if p.get("role") == "user":
                prompt = txt
            elif p.get("role") == "assistant":
                reply = txt
    return prompt, reply, running


def collect_codex(since):
    db = CODEX_DIR / "state_5.sqlite"
    if not db.exists():
        return []
    con = ro_connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select id, rollout_path, coalesce(updated_at_ms, updated_at*1000) as upd,
                  source, cwd, title, name, preview, model, reasoning_effort, tokens_used,
                  agent_nickname, archived
           from threads where coalesce(updated_at_ms, updated_at*1000) >= ?
           order by upd desc limit 200""",
        (since * 1000,),
    ).fetchall()
    con.close()
    items = []
    for r in rows:
        updated = r["upd"] / 1000
        prompt, reply, running = codex_rollout(r["rollout_path"])
        usage = cached_by_stat(r["rollout_path"], codex_usage) or {}
        try:
            updated = max(updated, os.stat(r["rollout_path"]).st_mtime)
        except OSError:
            pass
        src = r["source"] or ""
        auto = src == "exec" or src.startswith("{")
        # rollout に開始/完了の記録があるのでそれで判定（完了記録が無いまま放置されたものは除外）
        state = "busy" if running and now() - updated < 30 * 60 else "done"
        title = r["name"] or r["title"] or r["preview"]
        if r["agent_nickname"]:
            title = f"[{r['agent_nickname']}] {title}"
        items.append({
            "tool": "codex",
            "id": r["id"],
            "title": clip(title, 80),
            "cwd": r["cwd"],
            "updated": updated,
            "state": "archived" if r["archived"] else state,
            "prompt": clip(prompt or r["preview"]),
            "reply": clip(reply),
            "prompt_full": full_text(prompt or r["preview"]),
            "reply_full": full_text(reply),
            "auto": auto,
            "via": src if not src.startswith("{") else "subagent",
            "model": " ".join(x for x in (r["model"], r["reasoning_effort"]) if x) or None,
            "tokens": tokens_since(usage.get("days"), since),
            "context": usage.get("context") if (usage.get("context") or {}).get("window") else None,
            "limits": usage.get("limits"),
            "can_reply": not r["archived"] and state != "busy",
        })
    return items


# ---------------------------------------------------------------- Antigravity

def parse_ts(s):
    s = (s or "").strip()
    if not s:
        return 0
    s = s.replace(" ", "T", 1)
    # 小数秒が6桁を超えると fromisoformat が失敗する環境がある
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())[:6]
        tz = rest[len("".join(ch for ch in rest if ch.isdigit())):]
        s = f"{head}.{frac}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


AGY_CONV_DIRS = [AGY_DIR / "conversations", HOME / ".gemini" / "antigravity-cli" / "conversations"]
# パス中の "claude-501" などを拾わないよう、モデル名らしい形に限る
AGY_MODEL_RE = re.compile(rb"\b(?:gemini-\d[a-z0-9.\-]*|claude-(?:opus|sonnet|haiku|fable)[a-z0-9.\-]*|gpt-\d[a-z0-9.\-]*)")


def agy_model(path):
    """会話 DB の最新の生成メタデータからモデル名を拾う（中身は protobuf なので文字列だけ見る）"""
    try:
        con = ro_connect(path)
        row = con.execute("select data from gen_metadata order by idx desc limit 1").fetchone()
        con.close()
    except sqlite3.Error:
        return None
    m = AGY_MODEL_RE.search(row[0] or b"") if row else None
    return m.group(0).decode() if m else None


def agy_conversation_db(cid):
    for d in AGY_CONV_DIRS:
        p = d / f"{cid}.db"
        if p.exists():
            return str(p)
    return None


def collect_antigravity(since):
    db = AGY_DIR / "conversation_summaries.db"
    if not db.exists():
        return []
    con = ro_connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select conversation_id, title, preview, step_count, last_modified_time,
                  workspace_uris, status, source, agent_name, not_fully_idle, killed,
                  parent_conversation_id
           from conversation_summaries order by last_modified_time desc"""
    ).fetchall()
    con.close()
    items = []
    for r in rows:
        updated = parse_ts(r["last_modified_time"])
        status = r["status"] or ""
        running = "RUNNING" in status or bool(r["not_fully_idle"])
        if updated < since and not running:
            continue
        try:
            ws = json.loads(r["workspace_uris"] or "[]")
        except ValueError:
            ws = []
        cwd = ws[0].replace("file://", "") if ws else ""
        if r["killed"]:
            state = "done"
        elif running:
            state = "busy"
        else:
            state = "done"
        src = (r["source"] or "").replace("CORTEX_TRAJECTORY_SOURCE_", "").lower()
        items.append({
            "tool": "antigravity",
            "id": r["conversation_id"],
            "title": clip(r["title"] or r["preview"] or "(無題)", 80),
            "cwd": cwd,
            "updated": updated,
            "state": state,
            "prompt": clip(r["preview"]),
            "reply": "",
            "prompt_full": full_text(r["preview"]),
            "auto": src == "cli" or bool(r["parent_conversation_id"]) or "/scratchpad/" in cwd,
            "via": src or "ide",
            "steps": r["step_count"],
            "model": cached_by_stat(dbp, agy_model) if (dbp := agy_conversation_db(r["conversation_id"])) else None,
            "tokens": None,  # 使用量は protobuf の中で読めない
            # アプリ(IDE)で作った会話は agy から見えず "trajectory not found" になる
            "can_reply": bool(src) and not running,
            "no_reply": "アプリで作った会話はここから返信できません" if not src else None,
        })
    return items


# `agy -p /usage` は 10 秒近くかかるので裏で定期的に取って使い回す（モデルは呼ばないので枠は減らない）
AGY_QUOTA = {"rows": None, "fetched": 0, "running": False}
AGY_QUOTA_TTL = 5 * 60


def refresh_agy_quota():
    try:
        p = subprocess.run([cli("agy"), "-p", "/usage"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace", cwd=str(HOME), env=CLI_ENV, **NO_WINDOW)
        rows = []
        for line in p.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) == 4 and cols[2].rstrip("%").isdigit():
                rows.append({"group": cols[0], "label": cols[1],
                             "used_percent": 100 - int(cols[2].rstrip("%")), "resets_at": iso_ts(cols[3])})
        if rows:
            AGY_QUOTA["rows"] = rows
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        AGY_QUOTA["fetched"] = now()
        AGY_QUOTA["running"] = False


def agy_quota():
    if not AGY_QUOTA["running"] and now() - AGY_QUOTA["fetched"] > AGY_QUOTA_TTL:
        AGY_QUOTA["running"] = True
        threading.Thread(target=refresh_agy_quota, daemon=True).start()
    return AGY_QUOTA["rows"]


# `claude -p /usage` で使用制限を取る（モデルは呼ばないので枠は減らない）。デスクトップアプリだけを使っていても更新される。
# --no-session-persistence を付けないと、実行のたびに履歴ファイルが1つ増えてしまう
CLAUDE_USAGE = {"limits": None, "fetched": 0, "running": False}
CLAUDE_USAGE_TTL = 5 * 60
_USAGE_RE = re.compile(r"Current (session|week \(all models\)): (\d+)% used(?: · resets (.+?) \((.+?)\))?")


def parse_reset(text, tz):
    """「Oct 29 at 9:29am」「Oct 29, 9:29am」「Nov 1 at 6am」「9:29am」をタイムスタンプにする"""
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001  タイムゾーン名が読めなければこのマシンの時刻で扱う
        zone = None
    now_dt = datetime.now(zone)
    # 版によって日付と時刻の間が " at " のことも ", " のこともある
    t = text.replace(" at ", " ").replace(", ", " ").strip()
    for fmt, has_date in (("%b %d %I:%M%p", True), ("%b %d %I%p", True), ("%I:%M%p", False), ("%I%p", False)):
        try:
            d = datetime.strptime(t, fmt)
        except ValueError:
            continue
        d = d.replace(year=now_dt.year, tzinfo=zone) if has_date else \
            d.replace(year=now_dt.year, month=now_dt.month, day=now_dt.day, tzinfo=zone)
        if d.timestamp() < now_dt.timestamp() - 86400:  # 年をまたぐとき
            d = d.replace(year=d.year + 1)
        return d.timestamp()
    return 0


def refresh_claude_usage():
    try:
        p = subprocess.run([cli("claude"), "-p", "--no-session-persistence", "/usage"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60, cwd=str(HOME),
                           env=CLI_ENV, **NO_WINDOW)
        found = {}
        for kind, pct, reset, tz in _USAGE_RE.findall(p.stdout):
            found["primary" if kind == "session" else "secondary"] = {
                "used_percent": int(pct), "resets_at": parse_reset(reset, tz) if reset else 0}
        if found:
            CLAUDE_USAGE["limits"] = {"ts": now(), **found}
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        CLAUDE_USAGE["fetched"] = now()
        CLAUDE_USAGE["running"] = False


def claude_usage_limits():
    if not CLAUDE_USAGE["running"] and now() - CLAUDE_USAGE["fetched"] > CLAUDE_USAGE_TTL:
        CLAUDE_USAGE["running"] = True
        threading.Thread(target=refresh_claude_usage, daemon=True).start()
    return CLAUDE_USAGE["limits"]


# Codex の使用制限は、Codex アプリと同じく app-server の account/rateLimits/read で取る（モデルは呼ばない）。
# 会話の記録（rollout）に残る値は、記録を残さない実行（使い捨ての exec など）では更新されないため
CODEX_USAGE = {"limits": None, "fetched": 0, "running": False}
CODEX_USAGE_TTL = 5 * 60


def refresh_codex_usage():
    p = None
    try:
        p = subprocess.Popen([cli("codex"), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=str(HOME),
                             env=CLI_ENV, **NO_WINDOW)
        reqs = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-dashboard", "version": "1"}}},
                {"method": "initialized"},
                {"id": 2, "method": "account/rateLimits/read"}]
        p.stdin.write("".join(json.dumps(r) + "\n" for r in reqs))
        p.stdin.flush()
        # 応答が来ないと readline が戻らないので、20 秒たったら止めて読み取りを終わらせる
        timer = threading.Timer(20, p.kill)
        timer.daemon = True
        timer.start()
        result = None
        while True:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2:
                result = m.get("result") or {}
                break
        rl = (result or {}).get("rateLimits") or {}

        def win(w):
            if not w or w.get("usedPercent") is None:
                return None
            return {"used_percent": w["usedPercent"], "resets_at": w.get("resetsAt") or 0,
                    "window_minutes": w.get("windowDurationMins")}

        found = {k: win(rl.get(k)) for k in ("primary", "secondary")}
        if any(found.values()):
            CODEX_USAGE["limits"] = {"ts": now(), **found}
    except (OSError, ValueError):
        pass
    finally:
        if p:
            p.kill()
        CODEX_USAGE["fetched"] = now()
        CODEX_USAGE["running"] = False


def codex_usage_limits():
    if not CODEX_USAGE["running"] and now() - CODEX_USAGE["fetched"] > CODEX_USAGE_TTL:
        CODEX_USAGE["running"] = True
        threading.Thread(target=refresh_codex_usage, daemon=True).start()
    return CODEX_USAGE["limits"]


CLAUDE_LIMITS = CACHE_DIR / "claude_rate_limits.json"


def claude_limits():
    """statusline.py が書き残した rate_limits を Codex と同じ形にそろえる"""
    try:
        d = json.loads(CLAUDE_LIMITS.read_bytes())
    except (OSError, ValueError):
        return None
    rl = d.get("rate_limits") or {}

    def win(w):
        if not w or w.get("used_percentage") is None:
            return None
        reset = w.get("resets_at") or 0
        if isinstance(reset, str):
            reset = iso_ts(reset)
        elif reset > 1e12:  # ミリ秒で来た場合
            reset /= 1000
        return {"used_percent": round(w["used_percentage"]), "resets_at": reset}

    out = {"ts": d.get("ts"), "primary": win(rl.get("five_hour")), "secondary": win(rl.get("seven_day"))}
    return out if out["primary"] or out["secondary"] else None


COLLECTORS = {
    "claude": collect_claude,
    "codex": collect_codex,
    "antigravity": collect_antigravity,
}


INDEX = {}  # (tool, id, host) -> 直近の一覧に出た item。開く・返信の対象はここにあるものだけ


# 一覧は画面・別の端末・自分の確認から同時に頼まれる。並べて集めると、それぞれが数百 MB の履歴を
# 頭から読み合って何倍も遅くなるので、1つずつ集める（2つ目からは書き足された分を読むだけで済む）
_COLLECT_LOCK = threading.Lock()


def snapshot(days, with_remote=True, tokens=True, fresh=False):
    global COUNT_TOKENS
    with _COLLECT_LOCK:
        COUNT_TOKENS = tokens
        out = _snapshot(days, with_remote)
    if with_remote:
        # 別マシンの分は、ページに戻った直後に最新を待つことがあるので、ロックの外で合わせる
        # （ロックの中で待つと、ほかの端末からの共有の問い合わせまで待たされる）
        merge_remote(out, days, fresh)
    return out


COUNT_TOKENS = True  # False なら Claude の期間のトークン数を数えない（大きな履歴を頭から読まないので軽い）


def _snapshot(days, with_remote=True):
    since = now() - days * 86400
    out = {"generated": now(), "days": days, "tools": {}}
    for name, fn in COLLECTORS.items():
        try:
            items = fn(since)
            out["tools"][name] = {"items": items}
            # 使用制限はアカウント全体の値なので、いちばん新しい記録を採る
            limits = [i.pop("limits") for i in items if i.get("limits")]
            if name == "claude":
                # statusline の記録と /usage のうち、新しい方を使う
                limits = [x for x in [claude_limits(), claude_usage_limits() if with_remote else None] if x]
            if name == "codex" and with_remote:
                # 会話の記録に残った値と app-server から取った値のうち、新しい方を使う
                limits += [x for x in [codex_usage_limits()] if x]
            if name == "antigravity" and with_remote:
                out["tools"][name]["quota"] = agy_quota()
            if limits:
                out["tools"][name]["limits"] = max(limits, key=lambda x: x["ts"])
            INDEX.update(((name, i["id"], None), i) for i in items)
        except Exception as e:  # 1つ壊れても他は表示する
            out["tools"][name] = {"items": [], "error": f"{type(e).__name__}: {e}"}
    return out


# ---------------------------------------------------------------- リモート (SSH)
# hosts.json に書いたマシンへ SSH でつなぎ、このファイル自体を送って --dump させる。
# 相手側に何も置かなくてよい。鍵認証で、パスワードを聞かれずにつながる必要がある。
#   [{"name": "mac-mini", "ssh": "you@mac-mini.local"}]

HOSTS_FILE = APP_DIR / "hosts.json"
SELF_SOURCE = HERE / "server.py"  # exe 化したときも同梱しておけば SSH 先へ送れる
REMOTE = {}  # name -> {"data", "fetched", "error", "running", "days"}
REMOTE_TTL = 10  # 別マシンから取り直す間隔（秒）。1回 0.5 秒・100KB ほど
REMOTE_PATH = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH";'


def load_hosts():
    try:
        hosts = json.loads(HOSTS_FILE.read_bytes())
    except (OSError, ValueError):
        return []
    return [h for h in hosts if isinstance(h, dict) and h.get("name") and h.get("ssh")]


def ssh_argv(host, command):
    # BatchMode: パスワードや鍵のパスフレーズを聞かれる設定だと固まるので、聞かずに失敗させる
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host["ssh"], REMOTE_PATH + command]


def fetch_remote(host, days):
    st = REMOTE[host["name"]]
    try:
        if host.get("ts"):
            # Tailscale の相手で --share 付きのダッシュボードが動いていれば、そこから受け取る
            data = ts_get_sessions(host["ts"], days, st)
        else:
            p = subprocess.run(ssh_argv(host, f"python3 - --dump --days {float(days)}"),
                               input=SELF_SOURCE.read_bytes(), capture_output=True, timeout=60)
            if p.returncode != 0:
                raise RuntimeError((p.stderr.decode("utf-8", "replace").strip().splitlines() or ["接続できません"])[-1])
            data = json.loads(p.stdout)
        st["data"], st["error"], st["days"] = data, None, days
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        st["error"] = str(e) or type(e).__name__
    finally:
        st["fetched"] = now()
        st["running"] = False


def merge_remote(out, days, fresh=False):
    out["hosts"] = []
    hosts = load_hosts() + ts_hosts()
    waits = []
    for host in hosts:
        st = REMOTE.setdefault(host["name"], {"data": None, "fetched": 0, "error": None,
                                              "running": False, "days": None})
        # Tailscale の相手はダッシュボードが動いていないことも多いので、失敗したら間隔をあける
        ttl = REMOTE_TTL if (st["data"] or not host.get("ts")) else TS_RETRY
        # fresh（ページに戻った直後）は、表示したことのある相手を間隔に関係なく取り直す
        stale = (now() - st["fetched"] > ttl or (st["days"] != days and st["data"])
                 or (fresh and st["data"] and now() - st["fetched"] > 2))
        if stale and not st["running"]:
            st["running"] = True
            t = threading.Thread(target=fetch_remote, args=(host, days), daemon=True)
            t.start()
            if fresh and st["data"]:
                waits.append(t)
    # 見ていない間は取りに行っていないので、戻った直後は最新が届くまで少し待つ（最大 3 秒。間に合わなければ前のまま）
    deadline = time.time() + 3
    for t in waits:
        t.join(max(0, deadline - time.time()))
    for host in hosts:
        st = REMOTE[host["name"]]
        if host.get("ts") and st["data"] is None:
            continue  # 一度も応答のない端末は、ダッシュボードが無いとみなして出さない
        out["hosts"].append({"name": host["name"], "error": st["error"], "fetched": st["fetched"],
                             "via": "tailscale" if host.get("ts") else "ssh",
                             "loading": st["data"] is None and st["error"] is None})
        for name, t in ((st["data"] or {}).get("tools") or {}).items():
            if name not in out["tools"]:
                continue
            # 使用制限はアカウント全体の値なので、どのマシンの記録でもいちばん新しいものを使う
            rl, cur = t.get("limits"), out["tools"][name].get("limits")
            if rl and rl.get("ts") and (not cur or rl["ts"] > (cur.get("ts") or 0)):
                out["tools"][name]["limits"] = rl
            for i in t.get("items", []):
                i["host"] = host["name"]
                # アプリで開く・貼り付けはそのマシンの画面でしかできない。
                # Remote Control が付いていれば claude.ai で開いて、こちらで貼り付けて送れる
                if i.get("reply_mode") == "paste" and i.get("auto_paste"):
                    pass  # そのマシンのダッシュボードが、アプリに貼り付けて送る
                elif i.get("reply_mode") == "paste" and i.get("bridge_id"):
                    i["reply_mode"] = "bridge"
                elif i.get("reply_mode") == "paste":
                    i["can_reply"], i["no_reply"] = False, f"{host['name']} のアプリで開いているため、ここからは返信できません"
                out["tools"][name]["items"].append(i)
                INDEX[(name, i["id"], host["name"])] = i
            out["tools"][name]["items"].sort(key=lambda x: -x["updated"])


def bridge_url(item):
    bid = item.get("bridge_id") or ""
    if not re.fullmatch(r"session_[A-Za-z0-9]+", bid):
        raise ValueError("Remote Control の ID が想定外の形式です")
    return f"https://claude.ai/code/{bid}"


def remote_host(item):
    return next((h for h in load_hosts() + ts_hosts() if h["name"] == item.get("host")), None)


# ---------------------------------------------------------------- Tailscale
# --share で起動したダッシュボードは、自分の Tailscale アドレスでも待ち受ける。
# 受け付けるのは `tailscale whois` で「自分と同じユーザーの端末」と確認できた相手だけ。
# 見る側は `tailscale status` の端末一覧から、応答した端末を自動で並べる。

TS_NET4 = ipaddress.ip_network("100.64.0.0/10")
TS_NET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TS_RETRY = 30  # 未起動の相手には接続を断られるだけなので短くてよい
TS_CACHE = {"status": None, "fetched": 0, "whois": {}}


def ts_cli():
    found = shutil.which("tailscale", path=CLI_PATH)
    if found:
        return found
    for p in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        if os.path.exists(p):
            return p
    return None


def ts_json(*args):
    exe = ts_cli()
    if not exe:
        return None
    try:
        # --json はサブコマンドの直後に置かないと、後ろの引数扱いになって効かない
        p = subprocess.run([exe, args[0], "--json", *args[1:]], capture_output=True, timeout=10, **NO_WINDOW)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def ts_status():
    if now() - TS_CACHE["fetched"] > 30:
        TS_CACHE["status"], TS_CACHE["fetched"] = ts_json("status"), now()
    return TS_CACHE["status"]


def ts_hosts():
    """同じユーザーのオンラインのパソコン（スマホは除く）"""
    st = ts_status()
    if not st or not st.get("Self"):
        return []
    me = st["Self"].get("UserID")
    hosts = []
    for peer in (st.get("Peer") or {}).values():
        if not peer.get("Online") or peer.get("UserID") != me or peer.get("OS") in ("iOS", "android"):
            continue
        ip4 = next((ip for ip in peer.get("TailscaleIPs") or [] if "." in ip), None)
        if ip4:
            hosts.append({"name": peer.get("HostName") or ip4, "ts": ip4})
    return hosts


def ts_same_user(ip):
    """相手の Tailscale アドレスが、このマシンと同じユーザーの端末か"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr not in TS_NET4 and addr not in TS_NET6:
        return False
    hit = TS_CACHE["whois"].get(ip)
    if hit and now() - hit[0] < 300:
        return hit[1]
    st, who = ts_status(), ts_json("whois", ip)
    ok = bool(st and who and st.get("Self", {}).get("UserID")
              and (who.get("UserProfile") or {}).get("ID") == st["Self"]["UserID"])
    TS_CACHE["whois"][ip] = (now(), ok)
    return ok


def ts_self_ip():
    st = ts_status()
    return next((ip for ip in ((st or {}).get("Self") or {}).get("TailscaleIPs") or [] if "." in ip), None)


def ts_get_sessions(ip, days, st):
    """別マシンの一覧を取る。通信を減らすため、前回と同じなら中身を送らせず（304）、送るときは圧縮させる"""
    req = urllib.request.Request(f"http://{ip}:{PORT}/share/sessions?days={float(days)}",
                                 headers={"Accept-Encoding": "gzip"})
    if st.get("etag") and st.get("data") is not None and st.get("days") == days:
        req.add_header("If-None-Match", st["etag"])
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            st["etag"] = r.headers.get("ETag")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return st["data"]
        raise


def ts_get(ip, path, timeout=10):
    with urllib.request.urlopen(f"http://{ip}:{PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def ts_post(ip, path, body, timeout=20):
    req = urllib.request.Request(f"http://{ip}:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Agent-Dashboard": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- 開く / 返信

def workdir(item):
    cwd = item.get("cwd") or ""
    return cwd if os.path.isdir(cwd) else str(HOME)


def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_session(item):
    tool, sid = item["tool"], item["id"]
    if tool == "claude":
        if item.get("desktop_id"):
            url = f"claude://code/continue?session={item['desktop_id']}"
        else:
            # アプリに無いセッションは取り込んでから開く
            url = f"claude://resume?session={sid}"
        open_url(url)
    elif tool == "codex":
        open_url(f"codex://threads/{sid}")
    elif tool == "antigravity" and item.get("via") == "ide":
        # アプリの会話を直接開く URL は無いので、アプリを前面に出すだけ
        if IS_MAC:
            subprocess.run(["open", "-a", "Antigravity"], check=True)
        else:
            open_url("antigravity://")
    elif tool == "antigravity" and IS_WIN:
        # CLI の会話は新しいコマンドプロンプトで再開する（ID は UUID なのでそのまま渡せる）
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
            raise ValueError("会話 ID の形式が想定外です")
        subprocess.Popen(["cmd", "/c", "start", "agy", "cmd", "/k", cli("agy"), "--conversation", sid],
                         cwd=workdir(item))
    elif tool == "antigravity":
        # CLI の会話はターミナルで再開する
        cmd = f"cd {shlex.quote(workdir(item))} && agy --conversation {shlex.quote(sid)}"
        script = f'tell application "Terminal"\nactivate\ndo script {applescript_str(cmd)}\nend tell'
        subprocess.run(["osascript", "-e", script], check=True)


JOBS = {}
JOBS_LOCK = threading.Lock()
REPLY_TIMEOUT = 30 * 60


UPLOAD_DIR = CACHE_DIR / "uploads"
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGES, MAX_IMAGE_BYTES = 5, 10 * 1024 * 1024


def save_images(images):
    """data URL の画像を保存してパスを返す"""
    paths = []
    for img in (images or [])[:MAX_IMAGES]:
        m = re.match(r"data:(image/[a-z+]+);base64,(.+)", img or "", re.S)
        if not m or m.group(1) not in IMAGE_TYPES:
            raise ValueError("対応していない画像形式です（PNG / JPEG / GIF / WebP）")
        data = base64.b64decode(m.group(2))
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("画像が大きすぎます（1枚 10MB まで）")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        p = UPLOAD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}{IMAGE_TYPES[m.group(1)]}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


# 返信に添えられる書類。保存して、本文にパスを添えて読んでもらう
DOC_TYPES = (".pdf", ".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".csv")
MAX_DOCS, MAX_DOC_BYTES = 5, 15 * 1024 * 1024


def save_files(files):
    """[{name, data(data URL)}] の書類を保存してパスを返す。元の名前を残すので、読む側が何の書類か分かる"""
    paths = []
    for f in (files or [])[:MAX_DOCS]:
        name = os.path.basename(str((f or {}).get("name") or ""))
        stem, ext = os.path.splitext(name)
        if ext.lower() not in DOC_TYPES:
            raise ValueError("添付できるのは PDF・Excel・Word・CSV です")
        m = re.match(r"data:[^;,]*;base64,(.+)", (f or {}).get("data") or "", re.S)
        if not m:
            raise ValueError("添付ファイルを読めませんでした")
        data = base64.b64decode(m.group(1))
        if len(data) > MAX_DOC_BYTES:
            raise ValueError(f"{name} が大きすぎます（1つ 15MB まで）")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", stem)[:60] or "file"
        p = UPLOAD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}-{safe}{ext.lower()}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


def with_file_paths(text, paths):
    if not paths:
        return text
    return text + "\n\n添付ファイル（このファイルを読んで確認してください）:\n" + "\n".join(paths)


def with_image_paths(text, paths):
    # 画像を直接渡せないツールには、ファイルを読んでもらう
    if not paths:
        return text
    return text + "\n\n添付画像（このファイルを読んで確認してください）:\n" + "\n".join(paths)


def reply_command(item, text, images=()):
    """(argv, stdin) を返す。本文はシェルを通さず渡す"""
    tool, sid = item["tool"], item["id"]
    if tool == "claude":
        # 作業フォルダ外の画像も確認なしで読めるよう、保存先を許可フォルダに足す
        extra = ["--add-dir", str(UPLOAD_DIR)] if images or str(UPLOAD_DIR) in text else []
        return ["claude", "-p", "--resume", sid, *extra], with_image_paths(text, images)
    if tool == "codex":
        # `codex queue` は次にスレッドを開くまで実行されないので、その場で続きを実行する
        flags = [a for p in images for a in ("-i", p)]
        return ["codex", "exec", "resume", "--skip-git-repo-check", *flags, sid, "-"], text
    text = with_image_paths(text, images)
    if tool == "antigravity":
        # 画像の保存先をワークスペースに加えて、確認なしで読めるようにする
        extra = ["--add-dir", str(UPLOAD_DIR)] if images or str(UPLOAD_DIR) in text else []
        return ["agy", "--conversation", sid, *extra, f"-p={text}"], None
    raise ValueError(tool)


def copy_and_open(item, text):
    copy_text(text)
    open_session(item)
    app = {"claude": "Claude", "codex": "Codex"}.get(item["tool"], "アプリ")
    paste = "Ctrl+V" if IS_WIN else "⌘V"
    return f"{app} アプリで開きました。本文はコピー済みなので {paste} → Enter で送信してください"


PASTE_LOCK = threading.Lock()
# 本文はクリップボード経由で渡すので、スクリプトに利用者の文字列は入らない。
# チャットを開いただけでは入力先が入力欄に移らない（アプリ内ブラウザに残るなど）ので、
# UI Automation で入力欄（ProseMirror）を探して入力先にしてから貼り付ける。
# 入力欄が1つに決まらないとき（分割表示など）は、誤送信を避けて exit 3
PASTE_SCRIPT = (
    "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes; "
    "$ws = New-Object -ComObject WScript.Shell; "
    "$p = Get-Process Claude -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; "
    "if (-not $p -or -not $ws.AppActivate($p.Id)) { exit 2 }; "
    "$A = [System.Windows.Automation.AutomationElement]; "
    "$isEdit = New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, "
    "[System.Windows.Automation.ControlType]::Edit); "
    "$eds = @(); "
    # アプリの画面の読み取りは最初の問い合わせのあとで有効になるので、少し待ちながら数回探す
    "for ($i = 0; $i -lt 10 -and $eds.Count -eq 0; $i++) { "
    "$eds = @($A::FromHandle($p.MainWindowHandle).FindAll([System.Windows.Automation.TreeScope]::Descendants, $isEdit) "
    "| Where-Object { $_.Current.ClassName -match 'ProseMirror' -and -not $_.Current.IsOffscreen }); "
    "if ($eds.Count -eq 0) { Start-Sleep -Milliseconds 300 } }; "
    "if ($eds.Count -ne 1) { exit 3 }; "
    "$eds[0].SetFocus(); "
    "Start-Sleep -Milliseconds 300; $ws.SendKeys('^v'); Start-Sleep -Milliseconds 400; $ws.SendKeys('{ENTER}')"
)


# Mac 版。AppleScript の中で待ちのループを回すと、自動起動（launchd）から動かしたときに止まることがあったので、
# 1回で終わる短い問い合わせを Python 側で繰り返す。どの問い合わせにも時間制限をかけ、確かめられなければ送らない
MAC_FOCUS_INFO = """
tell application "System Events" to tell process "Claude"
	set g to value of attribute "AXFocusedUIElement"
	set r to role of g
	set c to (value of attribute "AXDOMClassList" of g) as text
	set e to g
	repeat 60 times
		set e to value of attribute "AXParent" of e
		if role of e is "AXWebArea" then exit repeat
	end repeat
	set v to ""
	try
		set v to (value of g) as text
	end try
	return (frontmost as text) & tab & r & tab & c & tab & ((title of e) as text) & tab & v
end tell
"""
MAC_KEYS = """
tell application "System Events"
	if not (frontmost of process "Claude") then return "nofront"
	keystroke "v" using command down
	delay 0.4
	key code 36
end tell
return "ok"
"""


def osa(script, *args, timeout=5):
    """osascript を1回動かして標準出力を返す。失敗や時間切れは None。
    subprocess.run の時間切れは、止めたあとも出力を待ち続けて戻らないことがあったので、止めたらすぐ戻る"""
    p = subprocess.Popen(["osascript", "-e", script, *args], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        return None
    return out.rstrip("\n") if p.returncode == 0 else None


def mac_paste_check(title, wait=6):
    """狙ったチャットの入力欄に入力先があり、書きかけが無ければ None、そうでなければ理由を返す"""
    if osa('tell application "System Events" to return UI elements enabled') != "true":
        return "noaccess"
    # Electron 製のアプリは、補助ツールが使われていると分かるまで画面の情報（入力先など）を出さない
    osa('tell application "System Events" to tell process "Claude" to set value of attribute "AXManualAccessibility" to true')
    osa('tell application "Claude" to activate')
    why = "nofocus"
    deadline = time.time() + wait  # チャットの切り替えには時間がかかることがあるので、しばらく待つ
    while time.time() < deadline:
        info = osa(MAC_FOCUS_INFO)
        if info:
            front, role, cls, shown, value = (info.split("\t", 4) + [""] * 5)[:5]
            if front != "true":
                why = "nofront"
            elif role == "AXTextArea" and "ProseMirror" in cls:
                # 入力欄から親をたどった先の画面の題名（「題名 - Claude Code」）で、どのチャットかを見分ける
                if shown == f"{title} - Claude Code":
                    # 空の入力欄でも改行1つが返るので、空白と改行を除いて残る文字があるときだけ書きかけ
                    return "draft" if value.strip(" \t\r\n\u3000") else None
                why = "wrongchat"
        time.sleep(0.3)
    return why


def mac_paste_send(title, check_only=False):
    why = mac_paste_check(title)
    if why is None and not check_only:
        why = None if osa(MAC_KEYS) == "ok" else "nofront"
    if why == "noaccess":
        return ("Claude アプリで開きました。キー操作の許可（システム設定 → プライバシーとセキュリティ → "
                "アクセシビリティ）が無いため、本文はコピーだけしています。⌘V → Return で送信してください")
    if why:
        reason = {"nofront": "アプリを前面に出せなかった", "nofocus": "入力欄に入力先が移らなかった",
                  "draft": "入力欄に書きかけの文字があった",
                  "wrongchat": "画面に出ているのが別のチャットだった"}.get(why, why)
        return f"Claude アプリで開きました。{reason}ため、本文はコピーだけしています。⌘V → Return で送信してください"
    return "確認だけしました（送信していません）" if check_only else "Claude アプリに貼り付けて送信しました"


def auto_paste_send(item, text):
    """Claude アプリでチャットを開き、本文を貼り付けて Enter を押す。アプリ側のチャットは止めない"""
    with PASTE_LOCK:  # 同時に送ると貼り付け先が入れ替わるので1件ずつ
        copy_text(text)
        open_session(item)
        time.sleep(2.5)  # アプリがそのチャットを表示し終えるのを待つ
        if IS_MAC:
            title = DESKTOP_TITLES.get(item["id"]) or ""
            same = [k for k, v in DESKTOP_TITLES.items() if v == title]
            if not title or len(same) > 1:
                # 同じ題名のチャットが複数あると、画面の題名で見分けられないので送らない
                return "Claude アプリで開きました。同じ題名のチャットがあり見分けられないため、本文はコピーだけしています。⌘V → Return で送信してください"
            return mac_paste_send(title)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PASTE_SCRIPT],
                           capture_output=True, timeout=30, **NO_WINDOW)
    if r.returncode == 3:
        return "Claude アプリで開きました。入力欄を1つに決められなかったので、本文はコピーだけしています。Ctrl+V → Enter で送信してください"
    if r.returncode != 0:
        raise ValueError("Claude アプリを前面に出せませんでした（画面がロック中かもしれません）")
    return "Claude アプリに貼り付けて送信しました"


def mac_reload_app():
    """「止めて送る」のあと、Claude アプリの「表示 → 再読み込み」を押して、裏のやりとりを画面に出す。
    アプリが手前にあるときは、入力中かもしれないので押さない。返り値はカードに添える一言"""
    if not IS_MAC:
        return None
    if osa('tell application "System Events" to return frontmost of process "Claude"') == "true":
        return "Claude アプリが手前にあったので、画面の再読み込みはしていません。アプリで「表示 → 再読み込み」すると反映されます"
    for view, reload in (("表示", "再読み込み"), ("View", "Reload")):
        if osa(f'tell application "System Events" to tell process "Claude" to click menu item "{reload}" of menu "{view}" of menu bar 1'):
            return "Claude アプリの画面を再読み込みしました（このやりとりもアプリに表示されます）"
    return "画面の再読み込みができませんでした。アプリで「表示 → 再読み込み」すると反映されます"


def mac_after_stop_send(item):
    notes = [mac_reload_app()]
    if item.get("bridge_id"):
        # 止めると Remote Control の接続も切れ、アプリで再開しても自動ではつながり直さない。
        # 外から付け直す方法（/remote-control の自動入力）は、入力先を安定して確かめられず使えなかった
        notes.append("このチャットの Remote Control は外れました。必要ならアプリのツールバーでオンにしてください")
    return "\n".join(n for n in notes if n)


def run_job(job_id, argv, stdin, cwd, fallback=None, after=None):
    job = JOBS[job_id]
    if argv[0] in ("claude", "codex", "agy"):
        argv = [cli(argv[0]), *argv[1:]]
    try:
        # Windows の既定の文字コード (cp932) では CLI の UTF-8 出力が化けるので明示する
        p = subprocess.run(
            argv, input=stdin, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=REPLY_TIMEOUT, env=CLI_ENV, **NO_WINDOW,
        )
        job["output"] = (p.stdout or "").strip() or (p.stderr or "").strip()
        job["status"] = "done" if p.returncode == 0 else "error"
        if job["status"] == "done" and after:
            note = after()
            if note:
                job["output"] += "\n\n" + note
        # Codex アプリで開いているスレッドは CLI から書き込めない。アプリで開いて貼り付けに切り替える
        if p.returncode != 0 and fallback and "active writer" in (p.stderr or "") + (p.stdout or ""):
            job["output"], job["status"] = fallback(), "done"
    except subprocess.TimeoutExpired:
        job["status"], job["output"] = "error", "タイムアウトしました"
    except OSError as e:
        job["status"], job["output"] = "error", str(e)
    job["finished"] = now()


def start_reply(item, text, images=(), stop=False):
    after = None
    if stop:
        # 「止めて送る」: アプリ側のプロセスを止めて、CLI で同じ会話の続きを送る
        if not item.get("can_stop_send"):
            raise ValueError("このチャットは「止めて送る」に対応していません")
        if item.get("host"):
            return start_remote_reply(item, text, images, stop=True)
        stop_idle_session(item["id"])
        if item["tool"] == "claude" and IS_MAC:
            after = lambda it=item: mac_after_stop_send(it)  # noqa: E731
        item = {**item, "reply_mode": "cli"}
    elif not item.get("can_reply"):
        raise ValueError(item.get("no_reply") or "このセッションには今は返信できません。アプリで開いて返信してください")
    if item.get("reply_mode") == "bridge":
        # 別マシンで開いているセッションは claude.ai で開き、本文はこのマシンのクリップボードへ
        copy_text(with_image_paths(text, []))
        open_url(bridge_url(item))
        job_id = uuid.uuid4().hex[:12]
        paste = "Ctrl+V" if IS_WIN else "⌘V"
        JOBS[job_id] = {"id": job_id, "tool": item["tool"], "session": item["id"], "status": "done",
                        "output": f"claude.ai で開きました。本文はコピー済みなので {paste} → Enter で送信してください",
                        "started": now(), "finished": now()}
        return JOBS[job_id]
    if item.get("host"):
        return start_remote_reply(item, text, images)
    images = save_images(images)
    job_id = uuid.uuid4().hex[:12]
    if item.get("reply_mode") == "paste":
        # クリップボードは1つしか持てないので、画像はパスを本文に添える
        body = with_image_paths(text, images)
        msg = auto_paste_send(item, body) if item.get("auto_paste") else copy_and_open(item, body)
        JOBS[job_id] = {"id": job_id, "tool": item["tool"], "session": item["id"], "status": "done",
                        "output": msg, "started": now(), "finished": now()}
        return JOBS[job_id]
    argv, stdin = reply_command(item, text, images)
    fallback = (lambda: copy_and_open(item, with_image_paths(text, images))) if item["tool"] == "codex" else None
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "tool": item["tool"], "session": item["id"],
                        "status": "running", "output": "", "started": now()}
    threading.Thread(target=run_job, args=(job_id, argv, stdin, workdir(item), fallback, after), daemon=True).start()
    return JOBS[job_id]


def start_remote_reply(item, text, images, stop=False):
    """そのマシンの作業フォルダで CLI を SSH 越しに実行する。画像はそのマシンに無いので未対応"""
    host = remote_host(item)
    if host is None:
        raise ValueError("このマシンに接続できません")
    if images:
        raise ValueError("リモートのセッションには画像を添付できません")
    if host.get("ts"):
        return start_ts_reply(host, item, text, stop)
    if stop:
        raise ValueError("SSH でつないだマシンでは「止めて送る」は使えません")
    argv, stdin = reply_command(item, text)
    cwd = shlex.quote(item.get("cwd") or "~")
    command = f"cd {cwd} 2>/dev/null || cd ~; " + " ".join(shlex.quote(a) for a in argv)
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "tool": item["tool"], "session": item["id"], "host": host["name"],
                        "status": "running", "output": "", "started": now()}
    threading.Thread(target=run_job, args=(job_id, ssh_argv(host, command), stdin, str(HOME)),
                     daemon=True).start()
    return JOBS[job_id]


def start_ts_reply(host, item, text, stop=False):
    """相手のダッシュボードに返信を頼み、その実行状況をこちらのジョブに写す"""
    return relay_job(host, "/share/reply", {"tool": item["tool"], "id": item["id"], "text": text, "stop": stop},
                     item["tool"], item["id"])


def relay_job(host, path, body, tool, session):
    """相手のダッシュボードに仕事を頼み、終わるまでその状況をこちらのジョブに写す"""
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "tool": tool, "session": session, "host": host["name"],
                        "status": "running", "output": "", "started": now()}

    def relay():
        job = JOBS[job_id]
        try:
            remote = ts_post(host["ts"], path, body)
            if remote.get("error"):
                raise RuntimeError(remote["error"])
            deadline = now() + REPLY_TIMEOUT
            while remote.get("status") == "running" and now() < deadline:
                time.sleep(2)
                remote = ts_get(host["ts"], f"/share/job?id={remote['id']}")
            job["status"] = remote.get("status") if remote.get("status") != "running" else "error"
            job["output"] = remote.get("output") or ("タイムアウトしました" if job["status"] == "error" else "")
        except urllib.error.HTTPError as e:
            try:
                job["output"] = json.loads(e.read()).get("error") or str(e)
            except ValueError:
                job["output"] = str(e)
            job["status"] = "error"
        except (OSError, ValueError, RuntimeError) as e:
            job["status"], job["output"] = "error", f"{host['name']} に送れませんでした: {e}"
        job["finished"] = now()

    threading.Thread(target=relay, daemon=True).start()
    return JOBS[job_id]


# ダッシュボードから新しいチャットを始める。本文は標準入力で渡す（シェルを通さない）
NEW_CHAT = {"claude": ["claude", "-p"], "codex": ["codex", "exec", "--skip-git-repo-check", "-"]}


def start_new(tool, cwd, text):
    """そのフォルダで CLI の新しいチャットを始め、返事をジョブに入れる。チャットはそのまま一覧に出る"""
    if tool not in NEW_CHAT:
        raise ValueError("この列では新しいチャットを始められません")
    cwd = os.path.expanduser((cwd or "").strip()) or str(HOME)
    if not os.path.isdir(cwd):
        raise ValueError(f"フォルダが見つかりません: {cwd}")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "tool": tool, "session": None, "status": "running", "output": "",
                        "started": now(), "cwd": cwd}
    threading.Thread(target=run_job, args=(job_id, list(NEW_CHAT[tool]), text, cwd), daemon=True).start()
    return JOBS[job_id]


def start_new_request(data):
    """/api/new と /share/new の共通部分。host があればその端末のダッシュボードに頼む"""
    text = (data.get("text") or "").strip()
    if not text:
        raise ValueError("依頼文が空です")
    if data.get("host"):
        host = remote_host({"host": data["host"]})
        if host is None or not host.get("ts"):
            raise ValueError(f"{data['host']} に接続できません（Tailscale でつながった端末だけ使えます）")
        return relay_job(host, "/share/new", {"tool": data.get("tool"), "cwd": data.get("cwd"), "text": text},
                         data.get("tool"), None)
    return start_new(data.get("tool"), data.get("cwd"), text)


class ShareHandler(BaseHTTPRequestHandler):
    """--share のとき Tailscale アドレスで待ち受ける窓口。自分の端末からの一覧取得と返信だけ受ける"""

    def send_json(self, obj, code=200):
        Handler.send_json(self, obj, code)

    def send_shared(self, data):
        # 中身が変わっていなければ 304 だけ返す（生成時刻は比べない）。変わっていれば圧縮して送る
        body = json.dumps(data, ensure_ascii=False).encode()
        etag = '"' + hashlib.sha1(json.dumps(data.get("tools"), ensure_ascii=False, sort_keys=True)
                                  .encode()).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", etag)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        if ts_same_user(self.client_address[0]):
            return True
        self.send_error(403)
        return False

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/share/sessions":
            self.send_shared(snapshot(float(q.get("days", ["3"])[0]), with_remote=False))
        elif url.path == "/share/rclog":
            self.send_json({"events": rc_log(min(500, int(q.get("limit", ["200"])[0])))})
        elif url.path == "/share/job":
            job = JOBS.get(q.get("id", [""])[0])
            self.send_json(job or {"error": "not found"}, 200 if job else 404)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        if path not in ("/share/reply", "/share/new") or self.headers.get("X-Agent-Dashboard") != "1":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 1024 * 1024)) or b"{}")
            if path == "/share/new":
                # 頼まれた側では、さらに別の端末へは回さない
                self.send_json(start_new_request({**data, "host": None}))
                return
            item = INDEX.get((data.get("tool"), data.get("id"), None))
            if item is None:
                raise ValueError("セッションが見つかりません")
            stop = bool(data.get("stop"))
            if item.get("reply_mode") == "paste" and not stop and not item.get("auto_paste"):
                raise ValueError("このマシンのアプリで開いているため、離れた場所からは返信できません")
            text = (data.get("text") or "").strip()
            if not text:
                raise ValueError("本文が空です")
            self.send_json(start_reply(item, text, stop=stop))
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)

    def log_message(self, *args):
        pass


def start_share_server():
    ip = ts_self_ip()
    if not ip:
        print("Tailscale が見つからないか未接続のため、共有はしません", flush=True)
        return
    try:
        srv = ThreadingHTTPServer((ip, PORT), ShareHandler)
    except OSError as e:
        print(f"Tailscale アドレス {ip}:{PORT} で待ち受けできません: {e}", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"共有中: http://{ip}:{PORT}（同じ Tailscale ユーザーの端末だけ）", flush=True)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        # 他サイトからの localhost 宛てリクエストや DNS rebinding を弾く
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self.local_request():
            self.send_error(403)
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/api/sessions":
            self.send_json(snapshot(float(q.get("days", ["3"])[0]), tokens=q.get("tokens", ["1"])[0] != "0",
                                    fresh=q.get("fresh", ["0"])[0] == "1"))
            return
        if url.path == "/api/job":
            job = JOBS.get(q.get("id", [""])[0])
            self.send_json(job or {"error": "not found"}, 200 if job else 404)
            return
        if url.path == "/api/rclog":
            self.send_json({"events": rc_log_all()})
            return
        if url.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = (HERE / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # 独自ヘッダー必須にすることでブラウザの CORS プリフライトを強制し、他サイトから叩けなくする
        if not self.local_request() or self.headers.get("X-Agent-Dashboard") != "1":
            self.send_error(403)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 180 * 1024 * 1024:  # 画像5枚・書類5つ + base64 の膨らみぶん
                raise ValueError("too large")
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.send_json({"error": "bad request"}, 400)
            return
        path = urlparse(self.path).path
        if data.get("tool") == "muse" and path == "/api/open":
            # Muse は会話がローカルに無いので一覧は出せない。アプリを呼び出すだけ
            if not IS_MAC:
                open_url("https://muse.ai/")  # アプリは Mac 版のみなので Web 版を開く
                self.send_json({"ok": True})
                return
            r = subprocess.run(["open", "-a", "Muse"], capture_output=True, text=True)
            self.send_json({"ok": True} if r.returncode == 0 else {"error": "Muse アプリが見つかりません"},
                           200 if r.returncode == 0 else 400)
            return
        if path == "/api/new":
            try:
                self.send_json(start_new_request(data))
            except (ValueError, OSError) as e:
                self.send_json({"error": str(e)}, 400)
            return
        item = INDEX.get((data.get("tool"), data.get("id"), data.get("host") or None))
        if item is None:
            self.send_json({"error": "セッションが見つかりません。再読み込みしてください"}, 404)
            return
        try:
            if path == "/api/open":
                if item.get("host") and item.get("bridge_id"):
                    open_url(bridge_url(item))
                    self.send_json({"ok": True})
                    return
                if item.get("host"):
                    raise ValueError(f"{item['host']} のセッションは、そのマシンで開いてください")
                open_session(item)
                self.send_json({"ok": True})
            elif path == "/api/reply":
                text = (data.get("text") or "").strip()
                images = data.get("images") or []
                files = data.get("files") or []
                if not text and not images and not files:
                    raise ValueError("本文が空です")
                if files and item.get("host"):
                    raise ValueError("別の端末のチャットには、ファイルを添付できません（そのPCにファイルがないため）")
                # 書類はこのPCに保存し、本文にパスを添える（どの送り方でも読めるように、ここで本文に入れる）
                text = with_file_paths(text or ("この画像を見てください" if images else "このファイルを見てください"), save_files(files))
                self.send_json(start_reply(item, text, images, stop=bool(data.get("stop"))))
            else:
                self.send_error(404)
        except (ValueError, subprocess.CalledProcessError, OSError) as e:
            self.send_json({"error": str(e)}, 400)

    def log_message(self, *args):
        pass


def install_autostart(remove=False):
    """Windows: スタートアップフォルダにコンソールなしで --share 起動する .cmd を置く"""
    if not IS_WIN:
        print("自動起動の登録は Windows 用です。Mac は launchd の plist を使ってください（README 参照）")
        return
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    target = startup / "agent-dashboard.cmd"
    if remove:
        target.unlink(missing_ok=True)
        print(f"自動起動を外しました: {target}")
        return
    if FROZEN:
        cmd = f'"{sys.executable}" --share'
    else:
        # pythonw なら黒いコンソール窓が出ない
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd = f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --share'
    target.write_text(f'@echo off\r\nstart "" /b {cmd}\r\n', encoding="utf-8")
    print(f"ログイン時に自動起動するよう登録しました: {target}\n今すぐ起動するには: {cmd}")


if __name__ == "__main__":
    if "--dump" in sys.argv:
        # リモートから SSH 越しに呼ばれる。そのマシンの分だけを JSON で返す
        days = float(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 3
        print(json.dumps(snapshot(days, with_remote=False), ensure_ascii=False))
        sys.exit()
    if "--install" in sys.argv or "--uninstall" in sys.argv:
        install_autostart(remove="--uninstall" in sys.argv)
        sys.exit()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, 10048):  # 10048: Windows のポート使用中
            raise
        print(f"ポート {PORT} は使用中です（すでに起動済みなら http://localhost:{PORT} を開いてください）。"
              f"\n別ポートで起動: PORT=8766 python3 {sys.argv[0]}")
        sys.exit(1)
    print(f"Agent dashboard: http://localhost:{PORT}", flush=True)
    threading.Thread(target=rc_watch, daemon=True).start()
    if "--share" in sys.argv:
        start_share_server()
    srv.serve_forever()
