import os
import re
import asyncio
import tempfile
import dataclasses
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import (
    get_astrbot_builtin_plugin_path,
    get_astrbot_plugin_path,
    get_astrbot_skills_path,
    get_astrbot_system_tmp_path,
    get_astrbot_temp_path,
)
from astrbot.core.workspace import (
    default_workspace_root,
    resolve_workspace_root_for_umo,
    workspace_path_to_root,
)
import astrbot.core.message.components as Comp

# 必须在 import pillowmd 之前打补丁，使首次运行即生效、无需重启
from .pillowmd_patch import apply_patch as _apply_pillowmd_patch
from .pillowmd_patch import apply_latex_patch as _apply_pillowlatex_patch

_apply_pillowmd_patch(logger)

import pillowmd

# pillowlatex 命令补全须在 import 之后（依赖其活模块对象做 monkey-patch）
_apply_pillowlatex_patch(logger)


# md2img 文件读取功能的限制（对齐 AstrBot readfile 的本地受限权限模型）：
# 非管理员仅允许 data/skills、data/plugins/*/skills、astrbot/builtin_stars/*/skills、
# 当前会话 workspace、/tmp/.astrbot、astrbot_temp 内的 .md/.markdown/.txt 文件，
# 单文件不超过 2MB；管理员不受路径限制（含 data/koko 等已授权路径）
_MD2IMG_ALLOWED_EXTS = (".md", ".markdown", ".txt")
_MD2IMG_MAX_FILE_SIZE = 2 * 1024 * 1024
# 匹配 md2img file:路径 / md2img 文件:路径 语法，兼容中英文冒号与首尾空白/引号
_MD2IMG_FILE_ARG_RE = re.compile(r"^\s*(?:file|文件)\s*[:：]\s*(.+?)\s*$")


def _md2img_read_allowed_roots(workspace_root: Path) -> tuple[Path, ...]:
    """Return the read-allowed roots for md2img, aligned with the readfile tool.

    Mirrors the local restricted permission model of
    ``astrbot.core.tools.computer_tools.fs._read_allowed_roots``: global skills,
    plugin-provided skills, the current workspace, the AstrBot system temp dir
    and the AstrBot temp dir. Anything else (including other ``data``
    subdirectories) is rejected.

    Args:
        workspace_root: Current session/project workspace root.

    Returns:
        Tuple of resolved allowed root directories.
    """
    roots = [
        Path(get_astrbot_skills_path()).resolve(strict=False),
        Path(get_astrbot_system_tmp_path()).resolve(strict=False),
        Path(get_astrbot_temp_path()).resolve(strict=False),
        workspace_root,
    ]
    for plugins_root in (
        Path(get_astrbot_plugin_path()),
        Path(get_astrbot_builtin_plugin_path()),
    ):
        if not plugins_root.is_dir():
            continue
        roots.extend(
            (plugin_dir / "skills").resolve(strict=False)
            for plugin_dir in plugins_root.iterdir()
            if plugin_dir.is_dir() and (plugin_dir / "skills").is_dir()
        )
    return tuple(roots)


@register("astrbot_plugin_nobrowser_markdown_to_pic", "Xican", "无浏览器Markdown转图片", "1.7.0")
class MyPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.style_path = config.get("style_path", "").strip()
        # 新增 mix 模式支持：配置中已经有 "disabled" / "length" / "regex" / "mix"
        self.auto_convert_mode = config.get("auto_convert_mode", "length")
        self.md2img_len_limit = config.get("md2img_len_limit", 100)
        # md2img 文件读取的 workspace 根；留空则使用 AstrBot 当前会话/项目 workspace
        self.workspace_path = config.get("workspace_path", "").strip()
        self.regex_pattern = config.get(
            "regex_pattern",
            r"```[\s\S]*?```|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|^#{1,6}\s+.+$|^>\s+.+$|^\s*[-*+]\s+.+$|^\s*\d+\.\s+.+$|\|[^\n]*\||\[.+?\]\(.+?\)|!\[.*?\]\(.+?\)|^\s*---+\s*$|^\s*\*\*\*+\s*$"
        )
        self.extract_links_and_code = config.get("extract_links_and_code", False)
        self.extract_links = config.get("extract_links", True)
        self.extract_code_blocks = config.get("extract_code_blocks", True)
        self.extract_inline_code = config.get("extract_inline_code", False)
        self.intercept_mode = config.get("intercept_mode", "pre_send")

        self._style = None
        self._compiled_regex = None
        self._last_image_paths = []
        self._image_paths_lock = asyncio.Lock()
        self.image_cache_ttl = int(config.get("image_cache_ttl", 180))

        # 编译正则表达式
        # ✅ 这里改为：在 regex 和 mix 模式下都预编译正则
        if self.auto_convert_mode in ("regex", "mix") and self.regex_pattern:
            try:
                # 使用 MULTILINE 和 DOTALL 标志以正确处理多行文本
                self._compiled_regex = re.compile(self.regex_pattern, re.DOTALL | re.MULTILINE)
            except re.error as e:
                logger.error(f"正则表达式编译失败: {e}")
                self._compiled_regex = None

    async def initialize(self):
        """插件初始化：加载 pillowmd 样式或使用默认样式"""
        logger.info("初始化无浏览器Markdown渲染（pillowmd）...")
        await self._init_style()

    async def _init_style(self):
        """根据配置加载本地样式目录；失败则退回默认样式"""
        if self.style_path:
            if os.path.exists(self.style_path):
                try:
                    loop = asyncio.get_running_loop()
                    self._style = await loop.run_in_executor(
                        None, lambda: pillowmd.LoadMarkdownStyles(self.style_path)
                    )
                    logger.info(f"已加载自定义样式: {self.style_path}")
                except Exception as e:
                    logger.error(f"加载样式失败，将使用默认样式: {e}")
                    self._style = None
            else:
                logger.warning(f"样式路径不存在: {self.style_path}，将使用默认样式")
                self._style = None

    def _should_convert_to_image(self, text: str) -> bool:
        """
        判断是否应该转换为图片

        模式说明：
        - disabled: 不转换
        - length : 只按长度判断
        - regex  : 只按正则判断
        - mix    : 优先正则，如果不匹配再按长度判断
        """
        if self.auto_convert_mode == "disabled":
            return False

        text_len = len(text)
        len_ok = self.md2img_len_limit > 0 and text_len > self.md2img_len_limit

        if self.auto_convert_mode == "length":
            return len_ok

        if self.auto_convert_mode == "regex":
            if self._compiled_regex is None:
                return False
            return bool(self._compiled_regex.search(text))

        if self.auto_convert_mode == "mix":
            # ✅ mix 模式：优先用正则判断，其次再按长度判断
            if self._compiled_regex is not None and self._compiled_regex.search(text):
                return True
            return len_ok

        # 兜底：未知模式一律不转换
        return False

    def _extract_content_elements(self, text: str) -> dict:
        """提取文本中的链接和代码块"""
        if not self.extract_links_and_code:
            return {}

        extracted = {}

        # 提取链接
        if self.extract_links:
            # Markdown链接格式: [text](url) 和 直接链接 http(s)://...
            link_patterns = [
                r'\[([^\]]+)\]\(([^)]+)\)',  # [text](url)
                r'(?<![\[\(])(https?://[^\s\)]+)',  # 直接链接
            ]
            links = []
            for pattern in link_patterns:
                matches = re.finditer(pattern, text)
                for match in matches:
                    if len(match.groups()) == 2:  # [text](url)
                        links.append(f"{match.group(1)}: {match.group(2)}")
                    else:  # 直接链接
                        links.append(match.group(1))
            if links:
                extracted['links'] = links

        # 提取代码块
        if self.extract_code_blocks:
            code_block_pattern = r'```(?:(\w+)\n)?([\s\S]*?)```'
            code_blocks = []
            matches = re.finditer(code_block_pattern, text)
            for match in matches:
                lang = match.group(1) or "text"
                code = match.group(2).strip()
                if code:
                    code_blocks.append(f"```{lang}\n{code}\n```")
            if code_blocks:
                extracted['code_blocks'] = code_blocks

        # 提取行内代码
        if self.extract_inline_code:
            inline_code_pattern = r'`([^`\n]+)`'
            inline_codes = re.findall(inline_code_pattern, text)
            if inline_codes:
                extracted['inline_codes'] = [f"`{code}`" for code in inline_codes]

        return extracted

    async def _send_extracted_content(self, extracted: dict, event: AstrMessageEvent):
        """发送提取的内容"""
        if not extracted:
            return

        content_parts = []

        if 'links' in extracted:
            content_parts.append("🔗 链接:")
            for link in extracted['links']:
                content_parts.append(f"  {link}")

        if 'code_blocks' in extracted:
            if content_parts:
                content_parts.append("")
            content_parts.append("📝 代码块:")
            for i, code_block in enumerate(extracted['code_blocks'], 1):
                content_parts.append(f"代码块 {i}:")
                content_parts.append(code_block)
                content_parts.append("")

        if 'inline_codes' in extracted:
            if content_parts:
                content_parts.append("")
            content_parts.append("💻 行内代码:")
            content_parts.append(" ".join(extracted['inline_codes']))

        if content_parts:
            message = "\n".join(content_parts)
            await event.send(MessageChain().message(message=message))

    def _clean_markdown_text(self, text: str) -> str:
        """清理Markdown文本，使代码块更规范并去除多余空行"""
        pattern = r"(\s*)```(?:\s*\n?)([\s\S]*?)(?:\n?\s*)```(\s*)"

        def replace_match(match):
            content = match.group(2)
            return f"\n```\n{content}\n```\n"

        text = re.sub(pattern, replace_match, text, flags=re.DOTALL)

        # 归一化 \dfrac / \tfrac -> \frac：pillowlatex 不认 \dfrac/\tfrac，
        # 会原样输出成字面 "dfrac"。两者在显示效果上等价于 \frac，安全替换。
        text = re.sub(r"\\[dt]frac(?![A-Za-z])", r"\\frac", text)

        return text.strip()

    async def _render_markdown_to_image(self, text: str, render_opts: dict = None):
        """渲染Markdown为图片，优先使用自定义样式；否则使用默认渲染。

        render_opts 可选项（均为 LLM 工具可控的安全参数）：
            title(str)      标题
            autoPage(bool)  自动分页（接近黄金分割比）
            noDecoration(bool) 透明背景、无装饰
            fontSize(int)   正文字号（覆盖样式）
            xSizeMax(int)   单行元素最大宽度（覆盖样式，近似图片宽度）
        """
        cleaned = self._clean_markdown_text(text)
        opts = render_opts or {}

        # 基础样式：优先用已加载的自定义样式，否则使用 pillowmd 默认样式
        base_style = self._style if self._style is not None else pillowmd.MdStyle()

        # 按需覆盖样式字段（不改动原样式对象，生成副本）
        style_overrides = {}
        if isinstance(opts.get("fontSize"), int) and opts["fontSize"] > 0:
            style_overrides["fontSize"] = max(8, min(opts["fontSize"], 200))
        if isinstance(opts.get("xSizeMax"), int) and opts["xSizeMax"] > 0:
            style_overrides["xSizeMax"] = max(200, min(opts["xSizeMax"], 4000))

        # 仅在 base_style 为 dataclass 时才支持 dataclasses.replace 覆盖字段
        if style_overrides and dataclasses.is_dataclass(base_style):
            style = dataclasses.replace(base_style, **style_overrides)
        else:
            style = base_style

        # 渲染级参数
        render_kwargs = {}
        if isinstance(opts.get("title"), str) and opts["title"].strip():
            render_kwargs["title"] = opts["title"].strip()
        if opts.get("autoPage") is True:
            render_kwargs["autoPage"] = True
        if opts.get("noDecoration") is True:
            render_kwargs["noDecoration"] = True

        # 若加载的是旧版/自定义样式渲染器（仅支持同步 Render），则回退到 Render
        if self._style is not None and not dataclasses.is_dataclass(self._style) and hasattr(self._style, "Render"):
            loop = asyncio.get_running_loop()
            img = await loop.run_in_executor(None, lambda: self._style.Render(cleaned))
            return img

        img = await pillowmd.MdToImage(cleaned, style=style, **render_kwargs)
        return img

    async def _save_temp_image(self, img):
        """保存图片到临时文件，并返回路径"""
        loop = asyncio.get_running_loop()

        def save():
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = f.name
            try:
                # pillowmd 的 MdRenderResult 通常包含 .image (PIL Image)
                pil_image = getattr(img, "image", None)
                if pil_image is not None:
                    pil_image.save(temp_path)
                else:
                    # 如果对象本身是 PIL Image 或兼容对象
                    if hasattr(img, "save"):
                        img.save(temp_path)
                    else:
                        raise RuntimeError("无法保存图片：未知类型")
            except Exception:
                # 兜底再次尝试 PIL 接口
                try:
                    if hasattr(img, "save"):
                        img.save(temp_path)
                    else:
                        pil_image = getattr(img, "image", None)
                        if pil_image is not None:
                            pil_image.save(temp_path)
                        else:
                            raise
                except Exception:
                    raise
            return temp_path

        path = await loop.run_in_executor(None, save)
        return path

    async def _generate_and_send_image(self, text: str, event: AstrMessageEvent, is_llm_response: bool, render_opts: dict = None):
        """
        渲染并发送图片：
        - is_llm_response=True 时，直接通过 event.send 发图片消息
        - is_llm_response=False 时，通过 result 形式 yield 给上层
        - 无论哪种情况，只要开启 extract_links_and_code，就会额外发送一条“链接/代码”消息
        - render_opts 透传给渲染器，支持 LLM 工具控制标题/字号/宽度/分页/透明背景
        """
        try:
            img = await self._render_markdown_to_image(text, render_opts)
            image_path = await self._save_temp_image(img)

            # 发送图片
            if is_llm_response:
                await event.send(MessageChain().file_image(path=image_path))
            else:
                # 在指令 / 过滤器返回等场景，通过 result 形式返回
                yield event.image_result(image_path)

            # 记录路径并异步删除
            async with self._image_paths_lock:
                self._last_image_paths.append(image_path)

            async def delayed_delete(p):
                await asyncio.sleep(self.image_cache_ttl)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

            asyncio.create_task(delayed_delete(image_path))

            # 无论是 LLM 响应还是普通指令，只要开启了提取，就单独发送链接 / 代码内容
            if self.extract_links_and_code:
                extracted = self._extract_content_elements(text)
                await self._send_extracted_content(extracted, event)

        except Exception as e:
            logger.error(f"处理失败: {str(e)}")
            if is_llm_response:
                # LLM 工具路径：向上抛出，由 render_markdown_to_image 捕获并
                # 如实返回 status=error，避免渲染失败却告知 LLM 成功。
                # 用户反馈交由 LLM 依据 error 结果决定（不再发送技术错误文本）。
                raise
            else:
                # 指令 / 过滤器路径：保持原行为，回退为错误文本消息
                yield event.plain_result(f"转换失败: {str(e)}")

    @filter.on_decorating_result(priority=-9999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        pre_send 拦截模式：
        - 会对即将发送的消息链进行遍历
        - 对 Plain 文本按 _should_convert_to_image（已支持 mix）判断是否转图
        - 如果转图成功，同时按配置发送“提取的链接/代码”
        """
        if self.intercept_mode != "pre_send":
            return
        result = event.get_result()
        chain = result.chain
        new_chain = []
        temp_paths = []

        for comp in chain:
            if isinstance(comp, Plain):
                text = comp.text
                if self._should_convert_to_image(text):
                    try:
                        img = await self._render_markdown_to_image(text)
                        path = await self._save_temp_image(img)
                        new_chain.append(Comp.Image.fromFileSystem(path))
                        temp_paths.append(path)

                        # 在 pre_send 模式下也提取链接 / 代码并单独发送
                        if self.extract_links_and_code:
                            extracted = self._extract_content_elements(text)
                            await self._send_extracted_content(extracted, event)

                        continue  # 已处理
                    except Exception as e:
                        logger.error(f"Markdown 转图片失败: {e}", exc_info=True)
                        # 失败时回退到原始文本
            new_chain.append(comp)

        # 如果有任何转换发生，替换消息链
        if temp_paths:
            for p in temp_paths:
                async def delayed_cleanup(px):
                    await asyncio.sleep(self.image_cache_ttl)
                    try:
                        if os.path.exists(px):
                            os.remove(px)
                    except Exception:
                        pass

                asyncio.create_task(delayed_cleanup(p))
            async with self._image_paths_lock:
                self._last_image_paths.extend(temp_paths)
            result.chain = new_chain

    async def _resolve_workspace_root(self, event: AstrMessageEvent) -> Path:
        """Resolve the read workspace root for an event (readfile-aligned).

        A configured ``workspace_path`` wins; otherwise the AstrBot current
        session/project workspace for the event is used.

        Args:
            event: The message event.

        Returns:
            Resolved workspace root.

        Raises:
            ValueError: The configured workspace path is invalid.
        """
        configured = self.workspace_path.strip()
        if configured:
            return workspace_path_to_root(configured)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        db = None
        if self.context is not None:
            try:
                db = self.context.get_db()
            except Exception:
                db = None
        if db is not None:
            try:
                return await resolve_workspace_root_for_umo(umo, db)
            except Exception:
                pass
        return default_workspace_root(umo)

    def _is_restricted_md2img_env(self, event: AstrMessageEvent) -> bool:
        """Whether md2img file reads are path-restricted for this event.

        Mirrors the readfile tool's ``_is_restricted_env``: admins are never
        path-restricted; non-admins are restricted unless the provider setting
        ``computer_use_require_admin`` is disabled (checked via the plugin
        context when available, defaulting to restricted).

        Args:
            event: The message event.

        Returns:
            True if only the allowed read roots may be accessed.
        """
        if getattr(event, "role", "member") == "admin":
            return False
        require_admin = True
        if self.context is not None:
            try:
                cfg = self.context.get_config(umo=event.unified_msg_origin)
                require_admin = bool(
                    cfg.get("provider_settings", {}).get(
                        "computer_use_require_admin", True
                    )
                )
            except Exception:
                require_admin = True
        return require_admin

    def _read_markdown_file(
        self, file_path: str, workspace_root: Path, restricted: bool = True
    ) -> str:
        """Read a Markdown/text file with the readfile-aligned permission model.

        Relative paths are resolved against the workspace root. In a restricted
        environment (non-admin), the realpath must stay inside one of the
        allowed read roots (data/skills, data/plugins/*/skills,
        astrbot/builtin_stars/*/skills, the workspace, /tmp/.astrbot, or the
        AstrBot temp dir), which blocks ``../`` escapes and symlink escapes.
        Admins are not path-restricted (aligned with readfile), but the
        extension, size and decoding limits below still apply.

        Args:
            file_path: File path, absolute or relative to the workspace root.
            workspace_root: Root used to resolve relative paths.
            restricted: Whether the path must stay inside the allowed roots.

        Returns:
            File content as text.

        Raises:
            FileNotFoundError: The file does not exist.
            ValueError: Path escapes the allowed roots, extension is not
                allowed, file is too large, or content cannot be decoded.
            OSError: The file cannot be read.
        """
        candidate = Path(file_path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (workspace_root / candidate).resolve(strict=False)

        if restricted:
            # Security: realpath-normalized path must stay inside an allowed root.
            allowed_roots = _md2img_read_allowed_roots(workspace_root)
            if not any(
                resolved == root or resolved.is_relative_to(root)
                for root in allowed_roots
            ):
                raise ValueError(
                    f"Forbidden: path is outside the allowed read directories: {file_path}"
                )
        if resolved.suffix.lower() not in _MD2IMG_ALLOWED_EXTS:
            raise ValueError(f"Only .md/.markdown/.txt files are allowed: {file_path}")
        if not resolved.is_file():
            raise FileNotFoundError(f"File does not exist: {file_path}")
        if resolved.stat().st_size > _MD2IMG_MAX_FILE_SIZE:
            raise ValueError(f"File exceeds the 2MB limit: {file_path}")

        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # gbk fallback for legacy Chinese-encoded text files
            return resolved.read_text(encoding="gbk")

    @filter.command("md2img", priority=-999)
    async def markdown_to_image(self, event: AstrMessageEvent):
        """Markdown转图片指令"""
        message_str = event.message_str
        pattern = r'^' + re.escape('md2img')
        message_str = re.sub(pattern, '', message_str).strip()

        # 1) file:路径 / 文件:路径 语法：读取允许目录内的本地文件
        #    相对路径以当前会话 workspace 为根（对齐 AstrBot readfile 本地路径解析）
        try:
            workspace_root = await self._resolve_workspace_root(event)
        except (OSError, ValueError) as e:
            yield event.plain_result(f"读取文件失败: {e}")
            return
        restricted = self._is_restricted_md2img_env(event)
        file_arg_match = _MD2IMG_FILE_ARG_RE.match(message_str)
        if file_arg_match:
            file_path = file_arg_match.group(1).strip().strip('"\'')
            try:
                content = self._read_markdown_file(
                    file_path, workspace_root, restricted=restricted
                )
            except (OSError, ValueError) as e:
                yield event.plain_result(f"读取文件失败: {e}")
                return
            async for result in self._generate_and_send_image(content, event, False):
                yield result
            return

        # 2) 消息附带 .md/.markdown/.txt 文件
        file_comp = None
        for comp in event.get_messages():
            if not isinstance(comp, Comp.File):
                continue
            comp_name = (comp.name or "").lower()
            comp_path = Path(comp.file_ or "").suffix.lower() if comp.file_ else ""
            if comp_name.endswith(_MD2IMG_ALLOWED_EXTS) or comp_path in _MD2IMG_ALLOWED_EXTS:
                file_comp = comp
                break

        if file_comp is not None:
            try:
                file_path = await file_comp.get_file()
                if not file_path:
                    raise ValueError("cannot get file content")
                content = self._read_markdown_file(
                    file_path, workspace_root, restricted=restricted
                )
            except (OSError, ValueError) as e:
                yield event.plain_result(f"读取文件失败: {e}")
                return
            async for result in self._generate_and_send_image(content, event, False):
                yield result
            return

        # 3) 原有行为：直接渲染消息文本
        if not message_str:
            yield event.plain_result("请输入要转换的Markdown内容")
            return

        async for result in self._generate_and_send_image(message_str, event, False):
            yield result

    @filter.llm_tool(name="render_markdown_to_image")
    async def render_markdown_to_image(
        self,
        event: AstrMessageEvent,
        markdown: str = "",
        title: str = "",
        font_size: int = 0,
        width: int = 0,
        auto_page: bool = False,
        transparent_bg: bool = False,
    ) -> dict:
        """将 Markdown 文本渲染为图片并直接发送给用户。当回复包含表格、代码块、标题、列表、公式、引用等富文本排版，文本形式难以清晰展示时调用本工具。可选参数用于控制排版样式，不需要时留空即可使用默认样式。

        Args:
            markdown(string): 要渲染的完整 Markdown 文本，支持标题、列表、表格、代码块、公式等语法
            title(string): 可选，图片顶部的标题文字，留空则不显示标题
            font_size(number): 可选，正文字号，留空或 0 使用默认（默认约 25）；建议范围 8-200，越大字越大图越大
            width(number): 可选，单行内容最大宽度（像素，近似图片宽度），留空或 0 使用默认（默认约 1000）；建议范围 200-4000
            auto_page(boolean): 可选，是否自动分页排版（尽量接近黄金分割比），内容很长时可设为 true
            transparent_bg(boolean): 可选，是否使用透明背景、去除装饰，默认 false
        """
        md = (markdown or "").strip()
        if not md:
            return {"status": "error", "message": "markdown 内容不能为空。"}
        render_opts = {
            "title": title,
            "fontSize": font_size,
            "xSizeMax": width,
            "autoPage": auto_page,
            "noDecoration": transparent_bg,
        }
        try:
            # is_llm_response=True 时内部直接通过 event.send 发图，不走 yield
            async for _ in self._generate_and_send_image(md, event, True, render_opts):
                pass
            return {"status": "success", "message": "Markdown 已渲染为图片并发送给用户。"}
        except Exception as e:
            logger.error(f"Markdown 转图片失败(llm_tool): {e}", exc_info=True)
            return {"status": "error", "message": f"渲染失败: {e}"}

    async def terminate(self):
        """插件销毁：无浏览器，无需清理资源"""
        logger.info("正在销毁无浏览器Markdown渲染插件...")
        try:
            async with self._image_paths_lock:
                for p in self._last_image_paths:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
                self._last_image_paths = []
        except Exception:
            pass

    @filter.on_llm_response(priority=-999)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """
        llm 拦截模式：
        - 只针对 LLM 响应
        - 根据 _should_convert_to_image（已支持 mix）判断是否转图
        - 转图成功会阻止原始文本发送，只发图片（以及可选的链接/代码文本）
        """
        if self.intercept_mode != "llm":
            return
        try:
            rawtext = resp.result_chain.chain[0].text
        except Exception:
            return
        logger.info(f"LLM原始响应内容: {rawtext}")
        if self._should_convert_to_image(rawtext):
            logger.info("检测到相关内容内容，开始转图...")
            try:
                # 这里 _generate_and_send_image 在 is_llm_response=True 时不会 yield，
                # async for 只是一种统一调用方式
                async for _ in self._generate_and_send_image(rawtext, event, True):
                    pass
                event.stop_event()
            except Exception as e:
                logger.error(f"处理失败: {str(e)}")
                msg_chain = MessageChain().message(message=f"处理失败: {str(e)}")
                await event.send(msg_chain)
