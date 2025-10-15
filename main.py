import os
import re
import asyncio
import tempfile

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse

try:
    import pillowmd
except Exception:
    pillowmd = None


@register("astrbot_plugin_nobrowser_markdown_to_pic", "Xican", "无浏览器Markdown转图片", "1.0.0")
class MyPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.style_path = config.get("style_path", "").strip()
        self.md2img_len_limit = config.get("md2img_len_limit", 100)

        self._style = None

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
        try:
            img = await self._render_markdown_to_image(text)
            image_path = await self._save_temp_image(img)

            if is_llm_response:
                await event.send(MessageChain().file_image(path=image_path))
            else:
                yield event.image_result(image_path)

            async def delayed_delete(p):
                await asyncio.sleep(10)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            asyncio.create_task(delayed_delete(image_path))

        except Exception as e:
            logger.error(f"处理失败: {str(e)}")
            error_msg = f"转换失败: {str(e)}"
            if is_llm_response:
                await event.send(MessageChain().message(message=error_msg))
            else:
                yield event.plain_result(error_msg)

    @filter.command("md2img")
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

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后超出阈值自动转图"""
        rawtext = resp.result_chain.chain[0].text
        if len(rawtext) > self.md2img_len_limit and self.md2img_len_limit > 0:
            try:
                async for _ in self._generate_and_send_image(rawtext, event, True):
                    pass
                event.stop_event()
            except Exception as e:
                logger.error(f"处理失败: {str(e)}")
                msg_chain = MessageChain().message(message=f"处理失败: {str(e)}")
                await event.send(msg_chain)
                
        else:
            pass