"""按主题把 CRUD-RAG 语料拆成多个知识库；默认只预演，加 --apply 才写入。

背景：`CRUD-RAG-60-v1` 混装了 60 道题的配套新闻与 100 篇无关事件新闻，跨领域检索时
精度和可解释性都很差。本脚本按新闻领域把 220 篇文档分到多个知识库，并同步重建测评集。

主题判定有两条规则：

1. 关键词规则：只看首段（标题与导语通常都在这里），按命中不同词的数量取最高分。
2. 显式覆盖：`OVERRIDES` 记录人工复核过的例外，键是文件名前 8 位十六进制。

案例的主题取它所有证据文档的多数票，文档的归属再取引用它的案例主题的多数票。这样
每道题的证据文档一定与其案例同库；未被任何案例引用的干扰文档按自身主题归属。

写入顺序保证可恢复：先复制原始文件，再在一个事务里改 `files`/`chunks`/`chunk_terms`
的 `kb_id` 与 `files.storage_path`，最后尽力删除旧路径。旧知识库和旧测评集只做归档，
不删除记录，历史评测仍可按 ID 读取。

用法：

  uv run python -m scripts.split_crud_knowledge_base plan
  uv run python -m scripts.split_crud_knowledge_base apply --apply
  uv run python -m scripts.split_crud_knowledge_base verify
"""

import argparse
import asyncio
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.config import Settings
from app.database import Database
from app.evaluations import digest

SOURCE_KB = "CRUD-RAG-60-v1"
PREFIX = "CRUD-"
DEVELOPMENT_SET = "CRUD-Development-v1"
ACCEPTANCE_SET = "CRUD-Acceptance-v1"

# 顺序即平票优先级：越靠前越具体，越靠后越宽泛。
TOPICS = {
    "国际时事": [
        "美国", "俄罗斯", "乌克兰", "日本", "韩国", "朝鲜", "欧盟", "联合国", "北约",
        "以色列", "巴勒斯坦", "伊朗", "印度", "德国", "法国", "英国", "澳大利亚",
        "新西兰", "加拿大", "巴西", "阿根廷", "南非", "尼日尔", "埃及", "柬埔寨",
        "泰国", "越南", "菲律宾", "新加坡", "马来西亚", "哈萨克斯坦", "乌干达",
        "古巴", "墨西哥", "危地马拉", "西班牙", "瑞典", "丹麦", "土耳其", "塞浦路斯",
        "巴基斯坦", "阿联酋", "尼泊尔", "缅甸", "白宫", "国会", "首相", "总统",
        "外长", "使馆", "大使", "西共体", "非盟",
    ],
    "金融与产业经济": [
        "中国人民银行", "证监会", "证券", "股票", "港股", "国债", "银行", "央行",
        "金融", "基金", "上市", "财报", "营收", "净利润", "人民币", "资本市场",
        "交易所", "私募", "保险", "民营经济", "民营企业", "营商环境", "减税",
        "降费", "油价", "成品油", "外贸", "出口", "进口", "消费", "制造业",
        "轻工业", "汽车", "房地产", "楼市", "城中村", "落户", "进博会", "班列",
        "航空", "客座", "电商", "夜经济", "消费券", "家电", "光伏", "运营商",
        "派息", "经济", "展会", "博览会", "民航", "铁路", "高铁", "城际",
        "动车", "列车", "盾构", "基建", "隧道", "桥梁", "公路", "港口",
        "地铁", "客流", "运量", "航线",
    ],
    "政务与法治": [
        "省委", "市委", "书记", "省长", "部长", "国务院", "政治局", "人大", "政协",
        "纪委", "监委", "党组", "党建", "主题教育", "习近平", "部署", "改革", "审批",
        "监管", "执法", "法院", "检察", "公安", "司法", "案件", "判决", "纠纷",
        "侵权", "知识产权", "普法", "督查", "问责", "通报", "排查", "整治",
        "安全生产", "应急", "消防", "事故", "隐患", "诈骗", "犯罪", "户口", "户籍",
        "版权", "维权",
    ],
    "生态环保与水利": [
        "生态", "环保", "污染", "排污", "治污", "污水", "水质", "碳排放", "碳中和",
        "节能", "能耗", "水利", "河湖", "流域", "水源", "防汛", "防洪", "洪灾",
        "水网", "节水", "气候", "变暖", "热浪", "干旱", "湿地", "垃圾", "大气",
        "核污水", "核污染", "治水", "台风", "暴雨", "降雨", "水位",
    ],
    "科技与数字产业": [
        "人工智能", "AI", "大模型", "算力", "芯片", "半导体", "元宇宙", "数字化",
        "5G", "智能手机", "云计算", "机器人", "航天", "卫星", "火箭", "核电",
        "信通院", "光纤", "传感", "网络安全", "网信", "数字经济", "游戏市场", "互联网",
        "推特", "马斯克", "社交平台",
    ],
    "农业与乡村振兴": [
        "农业", "农村", "农户", "农民", "秋粮", "粮食", "作物", "玉米", "水稻",
        "小麦", "病虫害", "植保", "种植", "养殖", "畜牧", "农技", "丰收", "乡村振兴",
        "耕地", "农田", "农资", "渔业", "渔民", "农产品", "田间", "苗情",
    ],
    "社会民生与文体": [
        "教育", "学校", "学生", "教师", "高校", "研学", "夏令营", "培训", "医院",
        "医疗", "医生", "健康", "养生", "中医", "疫苗", "养老", "社保", "医保",
        "文化", "非遗", "博物馆", "展览", "电影节", "演出", "体育", "比赛", "赛事",
        "大运会", "篮球", "足球", "运动员", "旅游", "游客", "景区", "文旅", "青少年",
        "儿童", "救援", "志愿", "华侨", "侨界", "华人", "青年", "公益", "刺绣",
        "杂技", "晚会", "歌手", "音乐", "电影", "食品安全", "抽检", "宠物", "托运",
    ],
}
FALLBACK = "其他"
ORDER = [*TOPICS, FALLBACK]
DOMESTIC = [
    "我国", "中国", "全国", "国务院", "省委", "市委", "县委", "湖北省", "北京", "上海",
    "广州", "深圳", "武汉", "湖北", "江苏", "浙江", "山东", "河南", "河北", "湖南",
    "四川", "安徽", "福建", "江西", "广西", "贵州", "云南", "甘肃", "陕西", "山西",
    "辽宁", "吉林", "黑龙江", "内蒙古", "新疆", "西藏", "宁夏", "青海", "海南",
    "重庆", "天津", "成都", "杭州", "南京", "西安", "郑州", "长沙",
]
# 人工复核过的例外：键为文件名前 8 位十六进制。
OVERRIDES = {
    "4d2f198d": "政务与法治",        # 城区落户限制，属户籍政策而非产业经济
    "b2f113dc": "国际时事",          # 苏丹冲突导致断水
    "3e457f8d": "金融与产业经济",    # 成品油调价
    "e35a1de6": "国际时事",          # 中资企业在柬埔寨
    "2d0d9d07": "国际时事",          # 一带一路企业与人才
    "cf89be9e": "政务与法治",        # 反诈提醒
    "6c59a99f": "生态环保与水利",    # 引江济淮航运
    "e661cc5f": "国际时事",          # 中埃外长通话
    "e08a5770": "国际时事",          # 日本外相行程
    "1e278c37": "生态环保与水利",    # 阿根廷高温与气候变化
    "2b2a1dab": "生态环保与水利",    # 布宜诺斯艾利斯高温
    "5175464e": "生态环保与水利",    # 极端热浪归因研究
    "be52b04f": "金融与产业经济",    # 与重庆至日本航线同一篇新闻的残段
    "87abf86e": "政务与法治",        # 湖北长江经济带部署
    "6672bf5b": "社会民生与文体",    # 支教队员
}


def classify(text):
    lede = text[:300]
    foreign = sum(1 for word in TOPICS["国际时事"] if word in lede)
    if foreign and not any(word in lede for word in DOMESTIC):
        return "国际时事"
    scores = {
        topic: sum(1 for word in words if word in lede) for topic, words in TOPICS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "国际时事" if foreign else FALLBACK
    for topic in ORDER:
        if scores.get(topic) == best:
            return topic


def doc_topic(name, text):
    prefix = name.split("-", 1)[1][:8] if name.startswith("crud-") else ""
    if prefix in OVERRIDES:
        return OVERRIDES[prefix]
    return classify(text)


def majority(values):
    counts = Counter(values)
    return max(counts.items(), key=lambda item: (item[1], -ORDER.index(item[0])))[0]


def short(text, limit=48):
    return re.sub(r"\s+", " ", text)[:limit]


async def load_source(database):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT * FROM knowledge_bases WHERE name=%s", (SOURCE_KB,)
        )
        kb = await cursor.fetchone()
        if kb is None:
            raise SystemExit(f"knowledge base not found: {SOURCE_KB}")
        cursor = await database.connection.execute(
            "SELECT f.file_id,f.name,f.extension,f.storage_path,f.content_hash,"
            "string_agg(c.content,' ' ORDER BY c.chunk_index) AS text "
            "FROM files f JOIN chunks c ON c.file_id=f.file_id "
            "WHERE f.kb_id=%s GROUP BY 1,2,3,4,5 ORDER BY f.name",
            (kb["kb_id"],),
        )
        files = await cursor.fetchall()
        cursor = await database.connection.execute(
            "SELECT * FROM evaluation_case_sets WHERE kb_id=%s AND archived=FALSE "
            "ORDER BY name",
            (kb["kb_id"],),
        )
        sets = await cursor.fetchall()
    return kb, files, sets


def build_plan(kb, files, sets):
    topics = {row["name"]: doc_topic(row["name"], row["text"] or "") for row in files}
    name_by_id = {row["file_id"]: row["name"] for row in files}

    cases, case_topics = [], {}
    for case_set in sets:
        for case in case_set["cases"]:
            missing = [fid for fid in case["relevant_file_ids"] if fid not in name_by_id]
            if missing:
                raise SystemExit(f"案例证据不在源库：{case['query'][:30]} → {missing}")
            topics_hit = [topics[name_by_id[fid]] for fid in case["relevant_file_ids"]]
            case_topics[id(case)] = majority(topics_hit) if topics_hit else FALLBACK
            cases.append((case_set, case))

    uses = defaultdict(list)
    for case_set, case in cases:
        for fid in case["relevant_file_ids"]:
            uses[fid].append(case_topics[id(case)])
    final = {}
    for row in files:
        voted = uses[row["file_id"]]
        final[row["name"]] = majority(voted) if voted else topics[row["name"]]

    for case_set, case in cases:
        kinds = {final[name_by_id[fid]] for fid in case["relevant_file_ids"]}
        if len(kinds) > 1:
            raise SystemExit(f"案例证据跨库：{case['query'][:30]} → {sorted(kinds)}")
    return topics, final, cases, case_topics


def report(kb, files, sets, topics, final, cases, case_topics):
    by_kb = defaultdict(list)
    for row in files:
        by_kb[final[row["name"]]].append(row)
    print(f"源知识库：{kb['name']}  {kb['kb_id']}  文件 {len(files)}  案例集 {len(sets)}")
    print("\n目标知识库：")
    for topic in ORDER:
        members = by_kb.get(topic, [])
        if not members:
            continue
        case_count = sum(1 for _, case in cases if case_topics[id(case)] == topic)
        print(f"  {PREFIX}{topic:<10} 文件 {len(members):>3}  案例 {case_count:>2}")
    changed = sum(1 for row in files if topics[row["name"]] != final[row["name"]])
    print(f"\n主题被案例归并修正的文档：{changed}")
    for row in files:
        if topics[row["name"]] != final[row["name"]]:
            print(
                f"  {row['name'][5:13]}  规则={topics[row['name']]} → 入库={final[row['name']]}"
                f"  {short(row['text'] or '')}"
            )


async def copy_object(storage_root: Path, old: str, new: str):
    source = storage_root / old
    target = storage_root / new
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_bytes, await asyncio.to_thread(source.read_bytes))
    return target


async def apply(settings, kb, files, sets, topics, final, cases, case_topics):
    database = Database(settings)
    storage_root = settings.file_storage_path
    wanted = [topic for topic in ORDER if any(final[row["name"]] == topic for row in files)]
    created = {}
    stamp = datetime.now(timezone.utc).isoformat()
    copied = []
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT name FROM knowledge_bases WHERE name=ANY(%s)",
                ([PREFIX + topic for topic in wanted],),
            )
            clash = [row["name"] for row in await cursor.fetchall()]
        if clash:
            raise SystemExit(f"目标知识库已存在，先人工确认：{clash}")

        for topic in wanted:
            kb_id = str(uuid4())
            created[topic] = kb_id
            async with database.transaction():
                await database.connection.execute(
                    "INSERT INTO knowledge_bases(kb_id,name,created_at) VALUES(%s,%s,%s)",
                    (kb_id, PREFIX + topic, stamp),
                )

        # 先复制原始文件：崩溃时最多留下无引用的副本，由孤儿扫描回收，不会丢失原文。
        for row in files:
            target = created[final[row["name"]]]
            key = f"{target}/{row['file_id']}{row['extension']}"
            await copy_object(storage_root, row["storage_path"], key)
            copied.append((key, row["storage_path"], target, row["file_id"]))

        async with database.transaction():
            for key, old, target, file_id in copied:
                await database.connection.execute(
                    "UPDATE files SET kb_id=%s,storage_path=%s,updated_at=%s WHERE file_id=%s",
                    (target, key, stamp, file_id),
                )
                await database.connection.execute(
                    "UPDATE chunks SET kb_id=%s WHERE file_id=%s", (target, file_id)
                )
                await database.connection.execute(
                    "UPDATE chunk_terms SET kb_id=%s WHERE file_id=%s", (target, file_id)
                )

        async with database.read():
            cursor = await database.connection.execute(
                "SELECT kb_id,file_id,content_hash FROM files WHERE kb_id=ANY(%s)",
                (list(created.values()),),
            )
            corpus = defaultdict(list)
            for row in await cursor.fetchall():
                corpus[row["kb_id"]].append(
                    {"file_id": row["file_id"], "content_hash": row["content_hash"]}
                )

        bundles = defaultdict(lambda: {"development": [], "acceptance": []})
        for case_set, case in cases:
            bundles[case_topics[id(case)]][case_set["split"]].append(case)
        saved = []
        async with database.transaction():
            for topic, groups in bundles.items():
                kb_id = created[topic]
                for split, name, status, frozen in (
                    ("development", DEVELOPMENT_SET, "draft", False),
                    ("acceptance", ACCEPTANCE_SET, "verified", True),
                ):
                    cases_json = groups[split]
                    if not cases_json:
                        continue
                    files_json = sorted(corpus[kb_id], key=lambda item: item["file_id"])
                    fingerprint = digest(
                        {
                            "cases": cases_json,
                            "corpus": files_json,
                            "split": split,
                            "review_status": status,
                            "frozen": frozen,
                        }
                    )
                    case_set_id = str(uuid4())
                    tags = ["crud-rag", split, "split-v1"]
                    if split == "development":
                        tags.append(topic)
                    await database.connection.execute(
                        "INSERT INTO evaluation_case_sets(case_set_id,kb_id,name,version,tags,"
                        "cases,corpus,fingerprint,created_at,split,review_status,frozen,archived) "
                        "VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)",
                        (
                            case_set_id,
                            kb_id,
                            name,
                            Jsonb(tags),
                            Jsonb(cases_json),
                            Jsonb(files_json),
                            fingerprint,
                            stamp,
                            split,
                            status,
                            frozen,
                        ),
                    )
                    saved.append((name, len(cases_json), len(files_json)))
            await database.connection.execute(
                "UPDATE evaluation_case_sets SET archived=TRUE WHERE kb_id=%s AND archived=FALSE",
                (kb["kb_id"],),
            )
            await database.connection.execute(
                "UPDATE knowledge_bases SET archived=TRUE WHERE kb_id=%s", (kb["kb_id"],)
            )

        for key, old, _, _ in copied:
            try:
                (storage_root / old).unlink(missing_ok=True)
            except OSError:
                print(f"  旧文件删除失败，可稍后由孤儿扫描回收：{old}")

    for topic in wanted:
        print(f"  {PREFIX}{topic}  {created[topic]}")
    for name, case_count, file_count in saved:
        print(f"  案例集 {name}: {case_count} 题 / 语料 {file_count} 篇")
    print(f"源知识库已归档：{kb['name']}")


async def verify(settings):
    database = Database(settings)
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT k.name,f.file_id FROM files f JOIN knowledge_bases k USING(kb_id)"
            )
            owner = {row["file_id"]: row["name"] for row in await cursor.fetchall()}
            cursor = await database.connection.execute(
                "SELECT k.name AS kb_name,c.name AS set_name,c.split,c.frozen,c.cases "
                "FROM evaluation_case_sets c "
                "JOIN knowledge_bases k USING(kb_id) WHERE c.archived=FALSE AND k.name LIKE %s",
                (PREFIX + "%",),
            )
            rows = await cursor.fetchall()
    problems = 0
    for row in rows:
        outside = [
            fid for case in row["cases"] for fid in case["relevant_file_ids"]
            if owner.get(fid) != row["kb_name"]
        ]
        status = "ok" if not outside else f"{len(outside)} 条证据不在本库"
        problems += bool(outside)
        print(
            f"  {row['kb_name']:<20} {row['set_name']:<26} {row['split']:<11} "
            f"frozen={row['frozen']!s:<5} {len(row['cases']):>2} 题  {status}"
        )
    print("校验通过" if not problems else f"存在 {problems} 个问题")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "apply", "verify"], nargs="?", default="plan")
    parser.add_argument("--apply", action="store_true", help="plan 时等价于 apply")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "verify":
        asyncio.run(verify(settings))
        return

    async def run():
        database = Database(settings)
        async with database.pool:
            kb, files, sets = await load_source(database)
        topics, final, cases, case_topics = build_plan(kb, files, sets)
        report(kb, files, sets, topics, final, cases, case_topics)
        if args.command == "apply" or args.apply:
            print()
            await apply(settings, kb, files, sets, topics, final, cases, case_topics)
        else:
            print("\n预演完成；加 --apply 写入。")

    asyncio.run(run())


if __name__ == "__main__":
    main()
