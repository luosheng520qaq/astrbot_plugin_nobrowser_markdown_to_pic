#!/usr/bin/env python3
"""Test script for the md2img file-reading feature.

Covers:
1. `md2img file:路径` / `md2img 文件:路径` reads a local file inside the data
   dir and sends the content into the render pipeline.
2. A .md/.markdown/.txt file attached in the message is read and rendered.
3. Escalation attempts are rejected: /etc/passwd, `../` escapes, symlink
   escapes, non-allowed extensions, >2MB files, and out-of-data attachments.
4. utf-8 first / gbk fallback decoding.
5. Existing plain-text rendering behavior is untouched.

Run from the plugin directory:
    python3 tests/test_md2img_file_reading.py
"""

import asyncio
import os
import shutil
import sys
import uuid

# ruff: noqa: E402, I001  # imports below must follow the env bootstrap above

# AstrBot resolves its data dir from $ASTRBOT_ROOT (or cwd). Point it at the
# real /AstrBot so the plugin's data root is exactly /AstrBot/data.
os.environ["ASTRBOT_ROOT"] = "/AstrBot"

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from astrbot.core.message.components import File as FileComponent

from astrbot_plugin_nobrowser_markdown_to_pic.main import MyPlugin

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  [PASS] {name}")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {name} {detail}")


class FakeEvent:
    """Minimal AstrMessageEvent stub to drive the md2img handler."""

    def __init__(self, message_str: str, components: list | None = None):
        self.message_str = message_str
        self._components = components or []
        self.results = []  # (kind, payload), kind in {"image", "text"}

    def get_messages(self):
        return self._components

    def image_result(self, url_or_path: str):
        self.results.append(("image", url_or_path))
        return url_or_path

    def plain_result(self, text: str):
        self.results.append(("text", text))
        return text

    async def send(self, chain):  # pragma: no cover - only used by extract mode
        pass


def make_plugin() -> MyPlugin:
    config = {
        "style_path": "",
        "auto_convert_mode": "disabled",
        "md2img_len_limit": 100,
        "regex_pattern": "",
        "extract_links_and_code": False,
        "extract_links": True,
        "extract_code_blocks": True,
        "extract_inline_code": False,
        "intercept_mode": "disabled",
        "image_cache_ttl": 180,
    }
    return MyPlugin(None, config)


async def run_handler(plugin: MyPlugin, event: FakeEvent) -> list:
    """Collect all yields of the md2img handler."""
    results = []
    async for r in plugin.markdown_to_image(event):
        results.append(r)
    return results


async def expect_success(
    plugin: MyPlugin, event: FakeEvent, expected_text: str, label: str
):
    """Assert the handler rendered expected_text into a real image."""
    captured = []
    real_render = plugin._render_markdown_to_image

    async def spy(text, render_opts=None):
        captured.append(text)
        return await real_render(text, render_opts)

    plugin._render_markdown_to_image = spy
    try:
        await run_handler(plugin, event)
    finally:
        plugin._render_markdown_to_image = real_render

    images = [p for kind, p in event.results if kind == "image"]
    texts = [t for kind, t in event.results if kind == "text"]
    check(f"{label}: one image result", len(images) == 1, f"results={event.results}")
    if images:
        img_path = images[0]
        is_png = (
            os.path.isfile(img_path)
            and open(img_path, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
        )
        check(f"{label}: produced a valid PNG file", is_png, img_path)
    check(
        f"{label}: content entered the render pipeline",
        captured == [expected_text],
        f"captured={captured!r}",
    )
    check(f"{label}: no error text result", len(texts) == 0, f"texts={texts}")


async def expect_rejected(plugin: MyPlugin, event: FakeEvent, label: str):
    """Assert the handler rejected the request with a readable error text."""
    await run_handler(plugin, event)
    texts = [t for kind, t in event.results if kind == "text"]
    images = [p for kind, p in event.results if kind == "image"]
    check(
        f"{label}: rejected with an error message",
        len(texts) == 1 and texts[0].startswith("读取文件失败"),
        f"results={event.results}",
    )
    check(f"{label}: no image was produced", len(images) == 0, f"images={images}")


async def main() -> None:
    global PASS_COUNT, FAIL_COUNT

    # Sandbox: a scratch dir under /AstrBot/data (inside the allowed root).
    test_dir = os.path.join(
        "/AstrBot", "data", "temp", f"md2img_test_{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(test_dir, exist_ok=True)

    md_content = (
        "# 测试文件\n\n- 列表项一\n- 列表项二\n\n```python\nprint('hello')\n```"
    )
    note_md = os.path.join(test_dir, "note.md")
    with open(note_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    note_txt = os.path.join(test_dir, "note.txt")
    with open(note_txt, "w", encoding="utf-8") as f:
        f.write("纯文本内容：txt 附件")

    # gbk-encoded file (utf-8 decoding must fail, then gbk fallback kicks in)
    note_gbk = os.path.join(test_dir, "note_gbk.md")
    with open(note_gbk, "wb") as f:
        f.write("GBK编码的中文内容".encode("gbk"))

    # Oversized file (>2MB)
    big_file = os.path.join(test_dir, "big.md")
    with open(big_file, "wb") as f:
        f.write(b"# big\n" + b"x" * (2 * 1024 * 1024))

    # Non-allowed extension inside the data dir
    exe_file = os.path.join(test_dir, "evil.exe")
    with open(exe_file, "w", encoding="utf-8") as f:
        f.write("# not allowed")

    # Symlink escape: data-internal .md pointing to /etc/passwd
    link_file = os.path.join(test_dir, "link.md")
    os.symlink("/etc/passwd", link_file)

    plugin = make_plugin()
    try:
        # ---- 1. file:路径 absolute ----
        print("== file: absolute path ==")
        ev = FakeEvent(f"md2img file:{note_md}")
        await expect_success(plugin, ev, md_content, "file: absolute")

        # ---- 2. 文件:路径 Chinese alias ----
        print("== 文件: Chinese alias ==")
        ev = FakeEvent(f"md2img 文件:{note_md}")
        await expect_success(plugin, ev, md_content, "文件: alias")

        # ---- 3. relative path (resolved against the data dir) ----
        print("== file: relative path ==")
        rel = os.path.relpath(note_md, "/AstrBot/data")
        ev = FakeEvent(f"md2img file:{rel}")
        await expect_success(plugin, ev, md_content, "file: relative")

        # ---- 4. file: path with surrounding spaces/quotes ----
        print("== file: quoted path ==")
        ev = FakeEvent(f'md2img file:"{note_md}"')
        await expect_success(plugin, ev, md_content, "file: quoted")

        # ---- 5. attached file (.md) ----
        print("== attached .md file ==")
        ev = FakeEvent("md2img", [FileComponent(name="note.md", file=note_md)])
        await expect_success(plugin, ev, md_content, "attachment .md")

        # ---- 6. attached file (.txt) ----
        print("== attached .txt file ==")
        ev = FakeEvent("md2img", [FileComponent(name="note.txt", file=note_txt)])
        await expect_success(plugin, ev, "纯文本内容：txt 附件", "attachment .txt")

        # ---- 7. gbk fallback ----
        print("== gbk decoding fallback ==")
        ev = FakeEvent(f"md2img file:{note_gbk}")
        await expect_success(plugin, ev, "GBK编码的中文内容", "gbk fallback")

        # ---- 8. security: /etc/passwd ----
        print("== security: /etc/passwd ==")
        ev = FakeEvent("md2img file:/etc/passwd")
        await expect_rejected(plugin, ev, "file:/etc/passwd")

        # ---- 9. security: ../ escape from the data dir ----
        print("== security: ../ escape ==")
        ev = FakeEvent("md2img file:../../../../etc/passwd")
        await expect_rejected(plugin, ev, "../ escape")

        # ---- 10. security: symlink escape ----
        print("== security: symlink escape ==")
        ev = FakeEvent(f"md2img file:{link_file}")
        await expect_rejected(plugin, ev, "symlink escape")

        # ---- 11. security: non-allowed extension ----
        print("== security: non-allowed extension ==")
        ev = FakeEvent(f"md2img file:{exe_file}")
        await expect_rejected(plugin, ev, ".exe rejected")

        # ---- 12. security: file over 2MB ----
        print("== security: >2MB file ==")
        ev = FakeEvent(f"md2img file:{big_file}")
        await expect_rejected(plugin, ev, ">2MB rejected")

        # ---- 13. security: attachment disguised as .md pointing outside ----
        print("== security: attachment escaping data dir ==")
        ev = FakeEvent("md2img", [FileComponent(name="passwd.md", file="/etc/passwd")])
        await expect_rejected(plugin, ev, "attachment escape")

        # ---- 14. regression: plain text still rendered ----
        print("== regression: plain text ==")
        ev = FakeEvent("md2img # 标题")
        await expect_success(plugin, ev, "# 标题", "plain text")

        # ---- 15. regression: empty message prompt ----
        print("== regression: empty message ==")
        ev = FakeEvent("md2img")
        await run_handler(plugin, ev)
        texts = [t for kind, t in ev.results if kind == "text"]
        check(
            "empty message prompts for content",
            len(texts) == 1 and texts[0] == "请输入要转换的Markdown内容",
            f"results={ev.results}",
        )

    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    print(f"\n=== {PASS_COUNT} passed, {FAIL_COUNT} failed ===")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    asyncio.run(main())
