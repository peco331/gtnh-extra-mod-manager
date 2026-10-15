"""从 CHANGELOG.md 取出某个版本的段落，作为 GitHub Release 正文。

用法：python scripts/release_notes.py v1.7.0 release-notes.md
找不到该版本时失败，避免发布出空白说明。
"""
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str:
    match = re.search(rf"^## {re.escape(version)}(?:（[^）]*）)?\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise SystemExit(f"CHANGELOG.md 中没有 {version} 的记录")
    return match.group(1).strip() + "\n"


if __name__ == "__main__":
    notes = section(CHANGELOG.read_text(encoding="utf-8"), sys.argv[1].lstrip("v"))
    if len(sys.argv) > 2:  # 直接写文件，避免经过 Windows 控制台编码转换
        Path(sys.argv[2]).write_text(notes, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(notes)
