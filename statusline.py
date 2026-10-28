#!/usr/bin/env python3
"""Claude Code のステータスライン用スクリプト。

Claude Code が渡す JSON の rate_limits（5時間枠・週の枠）をファイルに残し、
ダッシュボードが読めるようにする。表示は「モデル · 5h 12% · 週 30%」の1行。

~/.claude/settings.json:
  "statusLine": {"type": "command", "command": "python3 -I <このファイルのパス>"}
"""
import json
import os
import sys
import time
from pathlib import Path

if sys.platform == "darwin":
    CACHE = Path.home() / "Library/Caches/agent-dashboard"
elif sys.platform == "win32":
    CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agent-dashboard"
else:
    CACHE = Path.home() / ".cache/agent-dashboard"
OUT = CACHE / "claude_rate_limits.json"  # server.py の CLAUDE_LIMITS と同じ場所


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    rl = data.get("rate_limits")
    if rl:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "rate_limits": rl}))
        os.replace(tmp, OUT)  # 書きかけをダッシュボードが読まないように置き換える
    parts = [(data.get("model") or {}).get("display_name") or ""]
    for key, label in (("five_hour", "5h"), ("seven_day", "週")):
        w = (rl or {}).get(key) or {}
        if w.get("used_percentage") is not None:
            parts.append(f"{label} {round(w['used_percentage'])}%")
    print(" · ".join(p for p in parts if p))


if __name__ == "__main__":
    main()
