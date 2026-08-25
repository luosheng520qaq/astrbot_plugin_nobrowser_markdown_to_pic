#!/usr/bin/env python3
"""Test script for the md2img file-reading feature (readfile-aligned).

Covers:
1. `md2img file:路径` / `md2img 文件:路径` reads a local file inside an
   allowed read root and sends the content into the render pipeline.
2. Allowed read roots (aligned with AstrBot readfile): workspace (default
   session workspace and configured workspace_path), data/skills,
   data/plugins/*/skills, the AstrBot temp dir, and /tmp/.astrbot.
3. Permission model (readfile-aligned): admins (role==admin) are not
   path-restricted (koko dir and other authorized paths readable); non-admins
   are restricted to workspace / skills / temp / /tmp/.astrbot.
4. Forbidden roots are rejected for non-admins: /AstrBot/data/koko,
   /AstrBot/data/plugins, /AstrBot/data (outside the allowed set), /etc, and
   the /AstrBot root.
5. Escalation attempts are rejected: `../` escapes, symlink escapes,
   non-allowed extensions, >2MB files, and out-of-root attachments.
6. Relative paths resolve against the current workspace root.
7. utf-8 first / gbk fallback decoding.
8. Existing plain-text rendering behavior is untouched.
9. The llm tool render_markdown_to_image reads files via file_path with the
   same permission model (admin unrestricted, non-admin restricted) and
   keeps the markdown-only behavior unchanged.

Run from the plugin directory:
    uv run python tests/test_md2img_file_reading.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
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

    def __init__(
        self,
        message_str: str,
        components: list | None = None,
        umo: str = "test_FriendMessage_2111565284",
        role: str = "member",
    ):
        self.message_str = message_str
        self._components = components or []
        self.unified_msg_origin = umo
        self.role = role
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


def make_plugin(config_overrides: dict | None = None) -> MyPlugin:
    config = {
        "style_path": "",
        "workspace_path": "",
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
    if config_overrides:
        config.update(config_overrides)
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


async def expect_tool_success(
    plugin: MyPlugin,
    event: FakeEvent,
    expected_text: str,
    label: str,
    **kwargs,
):
    """Assert the llm tool rendered expected_text into a real image."""
    captured = []
    real_render = plugin._render_markdown_to_image

    async def spy(text, render_opts=None):
        captured.append(text)
        return await real_render(text, render_opts)

    plugin._render_markdown_to_image = spy
    try:
        result = await plugin.render_markdown_to_image(event, **kwargs)
    finally:
        plugin._render_markdown_to_image = real_render

    check(
        f"{label}: tool returned success",
        result.get("status") == "success",
        f"result={result}",
    )
    check(
        f"{label}: file content entered the render pipeline",
        captured == [expected_text],
        f"captured={captured!r}",
    )


async def expect_tool_error(
    plugin: MyPlugin,
    event: FakeEvent,
    label: str,
    message_prefix: str = "读取文件失败",
    **kwargs,
):
    """Assert the llm tool returned an error without rendering."""
    captured = []
    real_render = plugin._render_markdown_to_image

    async def spy(text, render_opts=None):
        captured.append(text)
        return await real_render(text, render_opts)

    plugin._render_markdown_to_image = spy
    try:
        result = await plugin.render_markdown_to_image(event, **kwargs)
    finally:
        plugin._render_markdown_to_image = real_render

    check(
        f"{label}: tool returned error",
        result.get("status") == "error",
        f"result={result}",
    )
    check(
        f"{label}: error message matches",
        str(result.get("message", "")).startswith(message_prefix),
        f"result={result}",
    )
    check(f"{label}: nothing entered the render pipeline", len(captured) == 0, captured)


async def main() -> None:
    global PASS_COUNT, FAIL_COUNT

    suffix = uuid.uuid4().hex[:8]

    # Scratch areas, one per allowed read root.
    test_dir = os.path.join("/AstrBot", "data", "temp", f"md2img_test_{suffix}")
    skills_dir = os.path.join("/AstrBot", "data", "skills", f"md2img_test_{suffix}")
    plugin_skills_dir = os.path.join(
        "/AstrBot", "data", "plugins", f"md2img_test_{suffix}", "skills"
    )
    ws_umo = f"test_FriendMessage_md2img_{suffix}"
    ws_dir = os.path.join("/AstrBot", "data", "workspaces", ws_umo)
    cfg_ws_dir = os.path.join(
        "/AstrBot", "data", "workspaces", f"md2img_test_cfg_{suffix}"
    )
    system_tmp_dir = os.path.join(tempfile.gettempdir(), ".astrbot")

    # Forbidden scratch files inside disallowed roots (real files, so rejection
    # is proven to come from the permission model, not FileNotFoundError).
    koko_file = os.path.join("/AstrBot", "data", "koko", f"md2img_test_{suffix}.md")
    plugins_root_file = os.path.join(
        "/AstrBot", "data", "plugins", f"md2img_test_{suffix}.md"
    )
    data_root_file = os.path.join("/AstrBot", "data", f"md2img_test_{suffix}.md")

    for d in (
        test_dir,
        skills_dir,
        plugin_skills_dir,
        ws_dir,
        cfg_ws_dir,
        system_tmp_dir,
    ):
        os.makedirs(d, exist_ok=True)

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

    # Non-allowed extension inside an allowed root
    exe_file = os.path.join(test_dir, "evil.exe")
    with open(exe_file, "w", encoding="utf-8") as f:
        f.write("# not allowed")

    # Symlink escape: allowed-root .md pointing to /etc/passwd
    link_file = os.path.join(test_dir, "link.md")
    os.symlink("/etc/passwd", link_file)

    # Same content in every allowed root (workspace / skills / plugins skills /
    # system tmp) to prove each root is readable.
    skills_note = os.path.join(skills_dir, "note.md")
    plugin_skills_note = os.path.join(plugin_skills_dir, "note.md")
    ws_note = os.path.join(ws_dir, "note.md")
    cfg_ws_note = os.path.join(cfg_ws_dir, "note.md")
    system_tmp_note = os.path.join(system_tmp_dir, f"md2img_test_{suffix}.md")
    for p in (skills_note, plugin_skills_note, ws_note, cfg_ws_note, system_tmp_note):
        with open(p, "w", encoding="utf-8") as f:
            f.write(md_content)

    for p in (koko_file, plugins_root_file, data_root_file):
        with open(p, "w", encoding="utf-8") as f:
            f.write("# forbidden")

    plugin = make_plugin()
    plugin_cfg = make_plugin({"workspace_path": cfg_ws_dir})
    try:
        # ---- 1. file: absolute path (data/temp) ----
        print("== file: absolute path ==")
        ev = FakeEvent(f"md2img file:{note_md}")
        await expect_success(plugin, ev, md_content, "file: absolute")

        # ---- 2. 文件:路径 Chinese alias ----
        print("== 文件: Chinese alias ==")
        ev = FakeEvent(f"md2img 文件:{note_md}")
        await expect_success(plugin, ev, md_content, "文件: alias")

        # ---- 3. relative path resolves against the session workspace ----
        print("== file: relative path in session workspace ==")
        ev = FakeEvent("md2img file:note.md", umo=ws_umo)
        await expect_success(plugin, ev, md_content, "workspace relative")

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

        # ---- 8. allowed root: data/skills ----
        print("== allowed root: data/skills ==")
        ev = FakeEvent(f"md2img file:{skills_note}")
        await expect_success(plugin, ev, md_content, "data/skills readable")

        # ---- 9. allowed root: data/plugins/*/skills ----
        print("== allowed root: data/plugins/*/skills ==")
        ev = FakeEvent(f"md2img file:{plugin_skills_note}")
        await expect_success(plugin, ev, md_content, "plugin skills readable")

        # ---- 10. allowed root: configured workspace_path ----
        print("== allowed root: configured workspace_path ==")
        ev = FakeEvent(f"md2img file:{cfg_ws_note}")
        await expect_success(plugin_cfg, ev, md_content, "configured workspace")

        # ---- 11. allowed root: /tmp/.astrbot ----
        print("== allowed root: /tmp/.astrbot ==")
        ev = FakeEvent(f"md2img file:{system_tmp_note}")
        await expect_success(plugin, ev, md_content, "system tmp readable")

        # ---- 12. security: /etc/passwd ----
        print("== security: /etc/passwd ==")
        ev = FakeEvent("md2img file:/etc/passwd")
        await expect_rejected(plugin, ev, "file:/etc/passwd")

        # ---- 13. security: /AstrBot root ----
        print("== security: /AstrBot root ==")
        ev = FakeEvent("md2img file:/AstrBot/README.md")
        await expect_rejected(plugin, ev, "/AstrBot root")

        # ---- 14. security (non-admin): /AstrBot/data/koko ----------------
        print("== security (non-admin): /AstrBot/data/koko ==")
        ev = FakeEvent(f"md2img file:{koko_file}")
        await expect_rejected(plugin, ev, "non-admin data/koko")

        # ---- 15. admin: /AstrBot/data/koko readable -----------------------
        print("== admin: /AstrBot/data/koko readable ==")
        ev = FakeEvent(f"md2img file:{koko_file}", role="admin")
        await expect_success(plugin, ev, "# forbidden", "admin koko readable")

        # ---- 16. admin: unrestricted (data root, outside allowed_dirs) ----
        print("== admin: unrestricted path outside allowed_dirs ==")
        ev = FakeEvent(f"md2img file:{data_root_file}", role="admin")
        await expect_success(plugin, ev, "# forbidden", "admin unrestricted")

        # ---- 17. admin: extension limit still enforced --------------------
        print("== admin: extension limit still enforced ==")
        ev = FakeEvent(f"md2img file:{exe_file}", role="admin")
        await expect_rejected(plugin, ev, "admin .exe rejected")

        # ---- 17. security: /AstrBot/data/plugins root --------------------
        print("== security: /AstrBot/data/plugins root ==")
        ev = FakeEvent(f"md2img file:{plugins_root_file}")
        await expect_rejected(plugin, ev, "data/plugins root")

        # ---- 18. security: /AstrBot/data root (not in the allowed set) ----
        print("== security: /AstrBot/data root ==")
        ev = FakeEvent(f"md2img file:{data_root_file}")
        await expect_rejected(plugin, ev, "data root")

        # ---- 19. security: ../ escape from the workspace ----
        print("== security: ../ escape ==")
        ev = FakeEvent("md2img file:../../../../etc/passwd", umo=ws_umo)
        await expect_rejected(plugin, ev, "../ escape")

        # ---- 20. security: symlink escape ----
        print("== security: symlink escape ==")
        ev = FakeEvent(f"md2img file:{link_file}")
        await expect_rejected(plugin, ev, "symlink escape")

        # ---- 21. security: non-allowed extension ----
        print("== security: non-allowed extension ==")
        ev = FakeEvent(f"md2img file:{exe_file}")
        await expect_rejected(plugin, ev, ".exe rejected")

        # ---- 22. security: file over 2MB ----
        print("== security: >2MB file ==")
        ev = FakeEvent(f"md2img file:{big_file}")
        await expect_rejected(plugin, ev, ">2MB rejected")

        # ---- 23. security: attachment disguised as .md pointing outside ----
        print("== security: attachment escaping allowed roots ==")
        ev = FakeEvent("md2img", [FileComponent(name="passwd.md", file="/etc/passwd")])
        await expect_rejected(plugin, ev, "attachment escape")

        # ---- 24. llm tool: file_path absolute (data/temp) -----------------
        print("== llm tool: file_path absolute ==")
        ev = FakeEvent("llm tool")
        await expect_tool_success(
            plugin, ev, md_content, "tool file_path absolute", file_path=note_md
        )

        # ---- 25. llm tool: file_path relative (session workspace) ---------
        print("== llm tool: file_path relative ==")
        ev = FakeEvent("llm tool", umo=ws_umo)
        await expect_tool_success(
            plugin, ev, md_content, "tool file_path relative", file_path="note.md"
        )

        # ---- 26. llm tool: admin reads /AstrBot/data/koko -----------------
        print("== llm tool: admin reads data/koko ==")
        ev = FakeEvent("llm tool", role="admin")
        await expect_tool_success(
            plugin, ev, "# forbidden", "tool admin koko", file_path=koko_file
        )

        # ---- 27. llm tool: non-admin reads data/koko rejected -------------
        print("== llm tool: non-admin data/koko rejected ==")
        ev = FakeEvent("llm tool")
        await expect_tool_error(
            plugin, ev, "tool non-admin koko", file_path=koko_file
        )

        # ---- 28. llm tool: non-admin /etc/passwd rejected -----------------
        print("== llm tool: non-admin /etc/passwd rejected ==")
        ev = FakeEvent("llm tool")
        await expect_tool_error(plugin, ev, "tool non-admin /etc", file_path="/etc/passwd")

        # ---- 29. llm tool regression: markdown only (no file_path) ---------
        print("== llm tool regression: markdown only ==")
        ev = FakeEvent("llm tool")
        await expect_tool_success(
            plugin, ev, "# 标题", "tool markdown only", markdown="# 标题"
        )

        # ---- 30. llm tool regression: empty markdown and no file_path -----
        print("== llm tool regression: empty markdown ==")
        ev = FakeEvent("llm tool")
        await expect_tool_error(
            plugin,
            ev,
            "tool empty markdown",
            message_prefix="markdown 内容不能为空",
            markdown="",
        )

        # ---- 31. regression: plain text still rendered ----
        print("== regression: plain text ==")
        ev = FakeEvent("md2img # 标题")
        await expect_success(plugin, ev, "# 标题", "plain text")

        # ---- 32. regression: empty message prompt ----
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
        shutil.rmtree(skills_dir, ignore_errors=True)
        shutil.rmtree(os.path.dirname(plugin_skills_dir), ignore_errors=True)
        shutil.rmtree(ws_dir, ignore_errors=True)
        shutil.rmtree(cfg_ws_dir, ignore_errors=True)
        for p in (koko_file, plugins_root_file, data_root_file, system_tmp_note):
            try:
                os.remove(p)
            except OSError:
                pass

    print(f"\n=== {PASS_COUNT} passed, {FAIL_COUNT} failed ===")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    asyncio.run(main())
