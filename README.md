# astrbot_plugin_nobrowser_markdown_to_pic

无浏览器的 Markdown 转图片 AstrBot 插件，使用 `pillowmd` 高效渲染 Markdown（支持 Latex），保留原有的文字获取与发送逻辑，显著降低资源占用。

作者：`Xican`

## 特性
- 基于 `pillowmd` 的纯本地渲染，无需浏览器/Chromedriver
- 支持基础 Markdown 元素与 Latex（行内/行间表达式）
- **多种自动转图模式**：长度判断、正则表达式匹配或完全禁用
- **智能内容提取**：可在转图后单独发送链接和代码块，便于用户复制
- 可配置本地样式目录（未配置则使用默认样式）

## 开始使用
- 安装：`pip install pillowmd`
- 插件部署到 AstrBot 后，使用指令：`/md2img [Markdown内容]`
- 根据配置的自动转图模式，LLM 回复将自动转换为图片发送

# 推荐！
自定义安装样式后，打开对应文件夹的setting.json
修改xSizeMax为1000
fontSize修改为30


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
| `auto_convert_mode` | 自动转图模式：`disabled`/`length`/`regex` | `"length"` |
| `md2img_len_limit` | 长度模式：LLM 输出超过该值后自动转图 | `100` |
| `regex_pattern` | 正则模式：匹配该正则表达式的内容将自动转图 | 默认匹配常见Markdown和LaTeX语法 |
| `extract_links_and_code` | 是否在转图后单独发送链接和代码块 | `false` |
| `extract_links` | 提取链接（需要 `extract_links_and_code` 开启） | `true` |
| `extract_code_blocks` | 提取代码块（需要 `extract_links_and_code` 开启） | `true` |
| `extract_inline_code` | 提取行内代码（需要 `extract_links_and_code` 开启） | `false` |

### 自动转图模式说明
- **disabled**: 完全禁用自动转图，只能通过 `/md2img` 指令手动转换
- **length**: 按文本长度判断，当 LLM 输出超过 `md2img_len_limit` 字符时自动转图
- **regex**: 按正则表达式判断，当 LLM 输出匹配 `regex_pattern` 时自动转图

### 内容提取功能
开启 `extract_links_and_code` 后，插件会在发送图片后额外发送一条消息，包含：
- 🔗 **链接**: 提取 Markdown 链接 `[text](url)` 和直接链接 `http://...`
- 📝 **代码块**: 提取 ``` 包围的代码块
- 💻 **行内代码**: 提取 `` ` `` 包围的行内代码（可选）

## 使用示例
- 指令：`/md2img \n# 标题\n> 引用\n\n```python\nprint('hello')\n````
- 自动转图（长度模式）：当 LLM 回复长度超过 `md2img_len_limit` 时自动发送图片
- 自动转图（正则模式）：当 LLM 回复包含代码块或数学公式时自动发送图片

## 注意事项
- 若配置了 `style_path`，请确保该路径存在并为 pillowmd 的样式目录
- 正则表达式模式需要谨慎配置，避免过于宽泛的匹配导致所有回复都转图
- 内容提取功能会增加一定的处理时间，建议根据需要开启
- 仅生成图片内容，链接不可交互
- pillowmd 的元素支持以其 README 为准

## 更新日志


### v1.1.0
 - 优化检测逻辑

### v1.0.0
- 重构为 pillowmd 渲染，移除浏览器依赖
- 新增 `style_path` 配置项，支持本地样式目录
- 保留 `md2img` 指令和 LLM 自动转图逻辑

## 特别鸣谢
- pillowmd 项目文档：https://github.com/Monody-S/CustomMarkdownImage

## 支持
- [AstrBot 帮助文档](https://astrbot.app)