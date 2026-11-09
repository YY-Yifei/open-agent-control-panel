"""Agent replies are Markdown; everything else stays text.

The renderer lives in ``src/oacp/web/md.js`` — a browser file with no build step.
So the tests import it the way the browser does: through Node. If Node is not
installed the module skips, rather than quietly pretending rendering is covered.

The security assertions here are the point of the file: a reply is untrusted
input, and whatever comes out of ``renderMarkdown`` goes straight into
``innerHTML``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "oacp" / "web"
MD = WEB / "md.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = f"""
const {{ renderMarkdown }} = await import({json.dumps(MD.as_uri())});
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const cases = JSON.parse(Buffer.concat(chunks).toString("utf8"));
process.stdout.write(JSON.stringify(cases.map((source) => renderMarkdown(source))));
"""


def render(*cases: str) -> list[str]:
    """Render each case in one Node process and return the HTML strings."""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", SCRIPT],
        input=json.dumps(list(cases)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def one(source: str) -> str:
    return render(source)[0]


# --------------------------------------------------------------- 基本语法


def test_plain_text_becomes_one_paragraph() -> None:
    assert one("hello world") == "<p>hello world</p>"
    assert one("") == ""
    assert one("   \n  ") == ""


def test_single_newlines_are_kept() -> None:
    # 模型很爱用单换行分行；段内换行必须留下，否则两句话会被粘成一行。
    assert one("line one\nline two") == "<p>line one<br>line two</p>"


def test_headings_lists_and_rules() -> None:
    assert one("# Title") == "<h1>Title</h1>"
    assert one("### Three") == "<h3>Three</h3>"
    assert one("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"
    assert one("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"
    # 从 3 开始的有序列表要保住起点，否则编号会悄悄从头数。
    assert one("3. three\n4. four") == '<ol start="3"><li>three</li><li>four</li></ol>'
    assert one("a\n\n---\n\nb") == "<p>a</p><hr><p>b</p>"


def test_quote_nests_block_content() -> None:
    assert one("> quoted\n> more") == "<blockquote><p>quoted<br>more</p></blockquote>"
    assert one("> ## h") == "<blockquote><h2>h</h2></blockquote>"


def test_inline_formatting() -> None:
    assert one("a **b** c") == "<p>a <strong>b</strong> c</p>"
    assert one("a __b__ c") == "<p>a <strong>b</strong> c</p>"
    assert one("a *b* c") == "<p>a <em>b</em> c</p>"
    assert one("~~gone~~") == "<p><del>gone</del></p>"
    assert one("use `a*b*` here") == "<p>use <code>a*b*</code> here</p>"


def test_underscores_inside_words_are_not_emphasis() -> None:
    # snake_case_name 这类标识符在 agent 输出里遍地都是，绝不能被当斜体。
    assert one("snake_case_name stays") == "<p>snake_case_name stays</p>"


def test_tables_are_rendered_with_alignment() -> None:
    html = one("| a | b |\n|---|--:|\n| 1 | 2 |")
    assert html == (
        '<table><thead><tr><th>a</th><th class="ta-right">b</th></tr></thead>'
        '<tbody><tr><td>1</td><td class="ta-right">2</td></tr></tbody></table>'
    )


def test_code_blocks_keep_their_content_verbatim() -> None:
    assert one("```py\nprint(1)\n```") == '<pre><code class="lang-py">print(1)</code></pre>'
    # 截断的输出（围栏没闭合）也要成块，而不是把剩下的 ``` 吐在气泡里。
    assert one("```py\nprint(1)") == '<pre><code class="lang-py">print(1)</code></pre>'
    # 块里的 * 与 # 是字面量。
    assert one("```\n*a* #b\n```") == "<pre><code>*a* #b</code></pre>"


# ------------------------------------------------------------------- 链接


def test_external_links_open_in_a_new_tab() -> None:
    assert one("[docs](https://example.com/a)") == (
        '<p><a href="https://example.com/a" target="_blank" rel="noopener noreferrer">docs</a></p>'
    )
    # 站内地址不开新标签，也没有 rel。
    assert one("[top](#top)") == '<p><a href="#top">top</a></p>'


def test_images_are_lazy_and_leak_no_referrer() -> None:
    assert one("![alt](https://example.com/i.png)") == (
        '<p><img src="https://example.com/i.png" alt="alt" loading="lazy" '
        'referrerpolicy="no-referrer"></p>'
    )


@pytest.mark.parametrize(
    "source",
    [
        "[x](javascript:alert(1))",
        "[x](JAVASCRIPT:alert(1))",
        "[x](java\tscript:alert(1))",
        "[x](data:text/html,<script>alert(1)</script>)",
        "[x](vbscript:msgbox(1))",
        "[x](//evil.example/phish)",
        "![x](javascript:alert(1))",
    ],
)
def test_dangerous_urls_degrade_to_text(source: str) -> None:
    html = one(source)
    assert "<a " not in html
    assert "<img " not in html
    # 内容不能丢：链接不成立，但用户还得看见对方写了什么。
    assert "x" in html


# --------------------------------------------------------------------- XSS


# 渲染器只会产出这些标签。任何别的东西出现，都意味着有一条规则把输入的
# 尖括号原样透了出去 —— 比逐个列黑名单可靠得多。
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "img", "li", "ol", "p", "pre", "strong", "table", "tbody",
    "td", "th", "thead", "tr", "ul",
}


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        "**<b>x</b>**",
        "> <script>alert(1)</script>",
        "| <script> |\n|---|\n| x |",
        "```<script>alert(1)</script>```",
        "`<script>alert(1)</script>`",
        "[<script>](https://example.com)",
    ],
)
def test_reply_html_is_never_live_markup(source: str) -> None:
    html = one(source)
    tags = set(re.findall(r"<([a-zA-Z][\w-]*)", html))
    assert tags <= ALLOWED_TAGS, f"渲染出了白名单外的标签：{sorted(tags - ALLOWED_TAGS)}"
    assert "javascript:" not in html.lower()
    # 转义后的形态还在：内容可见，只是不再是指令。
    assert "&lt;" in html


# ------------------------------------------------------------ 前端接线回归


def test_only_agent_replies_are_rendered_as_markdown() -> None:
    """用户自己的输入必须留在纯文本。

    否则人打一句「# 标题」会莫名其妙变成大标题，而错误文案本来就是服务端
    拼好的整句，过一遍 Markdown 只会改坏它。
    """
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'import { renderMarkdown } from "./md.js";' in app
    assert 'role === "agent" ? renderMarkdown(message.text) : esc(message.text)' in app
    assert app.count("renderMarkdown(") == 1
