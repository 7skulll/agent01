"""语料主题分类与归属规则；不连接数据库。"""

from scripts import split_crud_knowledge_base as split


def test_foreign_lede_goes_international():
    text = "新华社莫斯科7月31日电 俄罗斯国防部称，乌克兰无人机袭击了莫斯科市中心建筑。"
    assert split.doc_topic("crud-0000000000.txt", text) == "国际时事"


def test_domestic_vertical_wins_over_incidental_foreign_mention():
    text = "湖北日报讯 省农业农村厅组织专家组赴荆州指导秋粮作物病虫害防治，确保粮食稳产。"
    assert split.doc_topic("crud-1111111111.txt", text) == "农业与乡村振兴"


def test_override_beats_keyword_rule():
    name = "crud-4d2f198d0000.txt"
    text = "推动各地全面取消城区常住人口300万以下的城市落户限制，放宽落户条件。"
    assert name[5:13] in split.OVERRIDES
    assert split.doc_topic(name, text) == split.OVERRIDES[name[5:13]]


def test_unmatched_document_falls_back():
    assert split.classify("正文如下，内容不含任何领域词。") == split.FALLBACK


def test_majority_prefers_earlier_topic_on_tie():
    earlier, later = split.ORDER[0], split.ORDER[-2]
    assert split.majority([earlier, later]) == earlier


def test_majority_counts_votes():
    counts = ["社会民生与文体", "社会民生与文体", "国际时事"]
    assert split.majority(counts) == "社会民生与文体"


def test_order_covers_every_topic():
    assert set(split.ORDER) == {*split.TOPICS, split.FALLBACK}
    assert len(split.ORDER) == len(set(split.ORDER))
