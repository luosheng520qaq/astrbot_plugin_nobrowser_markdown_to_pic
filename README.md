# astrbot_plugin_nobrowser_markdown_to_pic

无浏览器的 Markdown 转图片 AstrBot 插件，使用 `pillowmd` 高效渲染 Markdown（支持 Latex），保留原有的文字获取与发送逻辑，显著降低资源占用。

作者：`Xican`

## 特性
- 基于 `pillowmd` 的纯本地渲染，无需浏览器/Chromedriver
- 支持基础 Markdown 元素与 Latex（行内/行间表达式）
- 支持在 LLM 回复过长时自动转图
- 可配置本地样式目录（未配置则使用默认样式）

## 开始使用
- 安装：`pip install pillowmd`
- 插件部署到 AstrBot 后，使用指令：`/md2img [Markdown内容]`
- 当 LLM 回复长度超过阈值（默认 100）时，自动将回复转换为图片发送

## 如何使用（pillowmd）
- 自定义样式渲染：
  - `style = pillowmd.LoadMarkdownStyles(样式路径)`
  - `img = style.Render(markdown内容)`
- 默认样式（异步）：
  - `img = await pillowmd.MdToImage(内容)`
- 默认样式（同步）：
  - `style = pillowmd.MdStyle()`
  - `img = style.Render("# Is Markdown")`

> 注：本插件会根据配置的 `style_path` 自动加载上面的自定义样式；未配置或加载失败时使用默认样式。
> 请前往 https://github.com/Monody-S/CustomMarkdownImage/tree/main/styles 下载自定义样式。

## 支持的元素（pillowmd 提供）
- 标题（#、##、###）/ 引用 / 列表（无序/有序）
- 行中代码 `` `code` `` 与代码块 ```
- 表格 |1|2| 与自动换行
- Latex 行中 `$x$` 与行间 `&& ... &&`
- 快捷图片、自定义颜色、自定义元素（可选）
> HTML 标签不支持；更多细节参考 pillowmd 文档。

## 配置项
| 配置名 | 描述 | 默认值 |
| :---: | :--- | :---: |
| `style_path` | pillowmd 样式目录（本地路径），留空则默认样式 | `""` |
| `md2img_len_limit` | LLM 输出超过该值后自动转图（<=0 禁用） | `100` |

## 使用示例
- 指令：`/md2img \n# 标题\n> 引用\n\n```python\nprint('hello')\n````
- 自动转图：当 LLM 回复长度超过 `md2img_len_limit` 时自动发送图片

## 注意事项
- 若配置了 `style_path`，请确保该路径存在并为 pillowmd 的样式目录
- 仅生成图片内容，链接不可交互
- pillowmd 的元素支持以其 README 为准

## 更新日志

### v2.0.0
- 重构为 pillowmd 渲染，移除浏览器依赖
- 新增 `style_path` 配置项，支持本地样式目录
- 保留 `md2img` 指令和 LLM 自动转图逻辑

## 特别鸣谢
- pillowmd 项目文档：https://github.com/Monody-S/CustomMarkdownImage

## 支持
- [AstrBot 帮助文档](https://astrbot.app)