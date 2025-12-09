import os
import re
import asyncio
import tempfile

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain
import astrbot.core.message.components as Comp

try:
    import pillowmd
except Exception:
    pillowmd = None


@register("astrbot_plugin_nobrowser_markdown_to_pic", "Xican", "无浏览器Markdown转图片", "1.2.0")
class MyPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.style_path = config.get("style_path", "").strip()
        # 新增 mix 模式支持：配置中已经有 "disabled" / "length" / "regex" / "mix"
        self.auto_convert_mode = config.get("auto_convert_mode", "length")
        self.md2img_len_limit = config.get("md2img_len_limit", 100)
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
        if pillowmd is None:
            logger.error("pillowmd 未安装，请先执行: pip install pillowmd")
            return
        await self._init_style()

    async def _init_style(self):
        """根据配置加载本地样式目录；失败则退回默认样式"""
        if pillowmd is None:
            return
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
        return text.strip()

    async def _render_markdown_to_image(self, text: str):
        """渲染Markdown为图片，优先使用自定义样式；否则使用默认渲染"""
        if pillowmd is None:
            raise RuntimeError("pillowmd 未安装")
        cleaned = self._clean_markdown_text(text)

        if self._style is not None:
            loop = asyncio.get_running_loop()
            img = await loop.run_in_executor(None, lambda: self._style.Render(cleaned))
            return img
        else:
            img = await pillowmd.MdToImage(cleaned)
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

    async def _generate_and_send_image(self, text: str, event: AstrMessageEvent, is_llm_response: bool):
        """
        渲染并发送图片：
        - is_llm_response=True 时，直接通过 event.send 发图片消息
        - is_llm_response=False 时，通过 result 形式 yield 给上层
        - 无论哪种情况，只要开启 extract_links_and_code，就会额外发送一条“链接/代码”消息
        """
        try:
            img = await self._render_markdown_to_image(text)
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
            error_msg = f"转换失败: {str(e)}"
            if is_llm_response:
                await event.send(MessageChain().message(message=error_msg))
            else:
                yield event.plain_result(error_msg)

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

    @filter.command("md2img", priority=-999)
    async def markdown_to_image(self, event: AstrMessageEvent):
        """Markdown转图片指令"""
        message_str = event.message_str
        pattern = r'^' + re.escape('md2img')
        message_str = re.sub(pattern, '', message_str).strip()
        if not message_str:
            yield event.plain_result("请输入要转换的Markdown内容")
            return

        async for result in self._generate_and_send_image(message_str, event, False):
            yield result

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
