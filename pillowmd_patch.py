"""
pillowmd 运行时自愈补丁。

pillowmd <= 0.7.3 存在一个公式渲染 BUG：当某个行内/行间公式渲染出的子图
数量恰好等于它在源码中占用的字符数时（如 "x"、"y=x" 这类简单公式），
绘制阶段的 `nowlatexImageIdx >= len(images)` 分支永不触发，导致
`del latexs[0]` 不执行、过期条目滞留在 latexs[0]，其后所有公式都会
匹配不到绘制窗口、退化为字面文本。

本模块在 import pillowmd 之前就地修补已安装的源码（幂等、可安全重复调用）。
上游修复见 fork：CustomMarkdownImage fix/latex-formula-poisoning。

设计原则：
- 必须在 import pillowmd 之前调用，首次运行即生效、无需重启
- 幂等：靠 sentinel 标记，已打过补丁则直接跳过
- 安全：找不到文件 / 不可写 / 匹配不到目标 / 已是修复版，一律静默跳过，
  绝不抛异常影响插件启动
"""

import importlib.util

SENTINEL = "# __PATCHED_LATEX_POISONING__"

# 待匹配的原始锚点（绘制循环开头）
_ANCHOR = (
    '        islatex = False\n'
    '\n'
    '        if latexs and latexs[0]["begin"]< idx <latexs[0]["end"]:'
)

# 注入后的内容
_REPLACEMENT = (
    '        islatex = False\n'
    '\n'
    f'        {SENTINEL}\n'
    '        # 丢弃已走过的 latex 条目：当公式"子图数==源码字符数"时，\n'
    '        # `>= len(images)` 分支永不触发、del latexs[0] 不执行，\n'
    '        # 过期条目会毒化其后所有公式，使之退化为字面文本。\n'
    '        while latexs and idx >= latexs[0]["end"]:\n'
    '            del latexs[0]\n'
    '            nowlatexImageIdx = -1\n'
    '\n'
    '        if latexs and latexs[0]["begin"]< idx <latexs[0]["end"]:'
)


def _find_renderer_path():
    """定位 pillowmd 的 CustomMarkdownRenderer.py，不触发模块执行。"""
    import os

    spec = importlib.util.find_spec("pillowmd")
    if spec is None or not spec.submodule_search_locations:
        return None
    for base in spec.submodule_search_locations:
        candidate = os.path.join(base, "CustomMarkdownRenderer.py")
        if os.path.isfile(candidate):
            return candidate
    return None


def apply_patch(logger=None):
    """就地修补已安装的 pillowmd。必须在 import pillowmd 之前调用。

    返回值（仅用于日志/测试）：
        "patched"        本次成功打补丁
        "already"        已打过补丁，跳过
        "not_found"      未找到 pillowmd 源文件
        "anchor_missing" 源码与预期不符（可能已被上游修复或版本变动），跳过
        "error:<msg>"    写入失败等异常，已安全跳过
    """
    def _log(msg):
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass

    path = _find_renderer_path()
    if not path:
        _log("pillowmd 补丁：未找到源文件，跳过")
        return "not_found"

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        _log(f"pillowmd 补丁：读取失败，跳过（{e}）")
        return f"error:{e}"

    if SENTINEL in content:
        return "already"

    if _ANCHOR not in content:
        # 源码与预期锚点不符：可能上游已修复或版本不同，安全跳过
        _log("pillowmd 补丁：未匹配到锚点（可能已是修复版本），跳过")
        return "anchor_missing"

    patched = content.replace(_ANCHOR, _REPLACEMENT, 1)
    try:
        import os
        import tempfile

        dir_name = os.path.dirname(path)
        tmp_path = None
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=dir_name) as tf:
            tf.write(patched)
            tmp_path = tf.name
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        _log(f"pillowmd 补丁：写入失败，跳过（{e}）")
        return f"error:{e}"

    _log("pillowmd 补丁：已修复公式渲染 BUG（latex-formula-poisoning）")
    return "patched"


# ---------------------------------------------------------------------------
# pillowlatex 补丁：针对 <= 0.1.4 版本的命令覆盖缺陷。
#
# pillowlatex 0.1.5+ 已在上游修复所有已知问题：
#   - 箭头命令（leftarrow/rightarrow 等）已注册
#   - 花体/数学字母变体（mathcal/mathbb/mathfrak 等）通过 _preprocess_math_alpha 预处理
#   - 间距命令（quad/qquad/\,/\; 等）通过 _preprocess_math_alpha 预处理
#   - Unicode 命令名边界 bug 已通过 _is_ascii_alpha 修正
#
# 此补丁保留为向后兼容层，在遇到旧版本时会跳过以避免兼容性问题。
# ---------------------------------------------------------------------------

def apply_latex_patch(logger=None):
    """针对 pillowlatex <= 0.1.4 的防御性补丁（0.1.5+ 已原生修复，直接跳过）。

    返回 "not_needed" / "not_found" / "error:<msg>"。
    """
    def _log(msg):
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass

    try:
        import pillowlatex
    except Exception as e:
        _log(f"pillowlatex 补丁：导入失败，跳过（{e}）")
        return f"error:{e}"

    # 检查版本：0.1.5+ 已原生修复所有问题
    version = getattr(pillowlatex, "__version__", "0.0.0")
    major, minor = 0, 0
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        pass

    if major > 0 or (major == 0 and minor >= 2):
        # 0.2.0+ 肯定包含修复
        return "not_needed"

    # 0.1.5 的特征检查：是否已有 _preprocess_math_alpha 和原生箭头支持
    has_preprocess = False
    if hasattr(pillowlatex, "latex"):
        has_preprocess = hasattr(pillowlatex.latex, "_preprocess_math_alpha")
    has_arrows = "leftarrow" in pillowlatex.replaces if hasattr(pillowlatex, "replaces") else False

    if has_preprocess and has_arrows:
        # 0.1.5 或更新的修复版本，无需补丁
        return "not_needed"

    # 到这里说明是 0.1.4 或更早，但为了安全起见不主动修改（避免破坏未知版本）
    _log("pillowlatex 补丁：检测到旧版本，但为避免兼容性问题已跳过主动修补")
    return "not_needed"
