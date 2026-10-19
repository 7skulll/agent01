from urllib.parse import quote

import pytest

from app.errors import ChatError
from app.rag.files import safe_filename


@pytest.mark.parametrize(
    "name,extension",
    [
        ("腾讯游戏MOBA品类：战略目标-项目组合-项目集-项目矩阵.pdf", ".pdf"),
        ("多部门职能组织下AI嵌入农业项目:迭代开发方案.docx", ".docx"),
        ("【农业项目】迭代方案；需求、范围与预算（第1版）.docx", ".docx"),
        ("Q&A：收入+成本=利润，增长率100%！[最终版]「修订」.pdf", ".pdf"),
        ("需求？为什么 & 怎么做 #1 @团队.txt", ".txt"),
        ("项目👩‍💻🚀—规划·评审 记录.md", ".md"),
        ('<报告>“项目”与"预算"|2026.xlsx', ".xlsx"),
    ],
)
def test_normal_display_names_are_preserved(name, extension):
    assert safe_filename(name) == (name, extension)
    assert safe_filename(quote(name)) == (name, extension)
    assert safe_filename(name.encode("utf-8").decode("latin-1")) == (name, extension)


@pytest.mark.parametrize(
    "name",
    ["a\x00:b.pdf", "a\n：b.docx", "a\t.txt", "a\x7f.txt", "\n报告.pdf", "中" * 84 + ".pdf"],
)
def test_invalid_names_remain_rejected(name):
    with pytest.raises(ChatError) as error:
        safe_filename(name)
    assert error.value.code == "invalid_filename"


def test_colon_does_not_bypass_extension_check():
    with pytest.raises(ChatError) as error:
        safe_filename("报告：最终.pdf.exe")
    assert error.value.code == "unsupported_file_type"
