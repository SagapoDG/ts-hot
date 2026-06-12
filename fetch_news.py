#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TS·HOT 商业秘密热点 —— 资讯聚合脚本
从多个国内外信源抓取 RSS/Atom，按关键词过滤、分类、计算热度、聚类去重，
输出 news.json 供前端渲染。仅依赖 Python 标准库。
"""
import concurrent.futures
import email.utils
import gzip
import html
import http.cookiejar
import json
from collections import Counter
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = timezone(timedelta(hours=8))

OUT_FILE = Path(__file__).parent / "news.json"
ARCHIVE_FILE = Path(__file__).parent / "archive.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_AGE_DAYS = 7        # 实时/热点主列表窗口
HIGHLIGHT_DAYS = 60     # 「要闻」回顾窗口（立法与案例），档案滚动保留期
HIGHLIGHT_TOP_N = 15    # 要闻每个栏目最多条数
TIMEOUT = 15

def gnews(query, zh=True):
    q = urllib.parse.quote(query)
    if zh:
        return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

def bnews(query):
    return f"https://www.bing.com/news/search?q={urllib.parse.quote(query)}&format=rss"

def sogou(query):
    """搜狗微信搜索（type=2 为公众号文章）"""
    return f"https://weixin.sogou.com/weixin?type=2&query={urllib.parse.quote(query)}"

# 信源配置：
#   name     展示名称
#   url      RSS/Atom 地址
#   region   国内 / 国际
#   weight   信源权重（专业源高，综合源低）
#   implies  该信源天然满足的相关性维度（见下方"相关性闸门"）：
#            core=商业秘密专门领域（单独命中即收录）  topic=主题维度  legal=法律维度
SOURCES = [
    # —— 官方机构官网（无 RSS，直接解析新闻列表页；item_re 依次捕获 链接/标题/日期，
    #     official 标记使其新闻天然通过「立法监管」的官方主体闸门）——
    {"name": "最高人民法院", "type": "html", "url": "https://www.court.gov.cn/zixun.html",
     "base": "https://www.court.gov.cn", "official": True, "region": "国内", "weight": 3.0, "implies": ("legal",),
     "item_re": r'<a[^>]+href="(/zixun/xiangqing/\d+\.html)"[^>]*>(.*?)</a>\s*<i class="date">(20\d{2}-\d{2}-\d{2})</i>'},
    {"name": "最高人民检察院", "type": "html", "url": "https://www.spp.gov.cn/spp/tt/index.shtml",
     "base": "https://www.spp.gov.cn", "official": True, "region": "国内", "weight": 3.0, "implies": ("legal",),
     "item_re": r'<a href="(/spp/[^"]+\.shtml)"[^>]*>([^<]{8,90})</a>\s*<[^>]*>\s*(20\d{2}-\d{2}-\d{2})'},
    {"name": "最高法知识产权法庭", "type": "html", "url": "https://ipc.court.gov.cn/zh-cn/news/index.html",
     "base": "https://ipc.court.gov.cn", "official": True, "region": "国内", "weight": 3.0, "implies": ("legal",),
     "item_re": r'<a href="(/zh-cn/news/view-\d+\.html)"[^>]*title="([^"]{6,90})"[^>]*>.*?(20\d{2}/\d{2}/\d{2})'},
    # —— 国际专业源 ——
    # Fair Competition Law：竞业限制/商业秘密专业博客，整站在题
    {"name": "Fair Competition Law", "url": "https://www.faircompetitionlaw.com/feed/", "region": "国际", "weight": 3.0, "implies": ("core",)},
    # DOJ 新闻稿（法律维度天然满足），须命中商业秘密/经济间谍等主题词
    {"name": "DOJ 美国司法部", "url": "https://www.justice.gov/news/rss?type=press_release", "region": "国际", "weight": 2.5, "implies": ("legal",)},
    # FTC（竞业限制规则等），须命中主题词
    {"name": "FTC 美国联邦贸易委员会", "url": "https://www.ftc.gov/feeds/press-release.xml", "region": "国际", "weight": 2.5, "implies": ("legal",)},
    # Law360 知识产权频道（法律维度天然满足），泛知产新闻，须命中主题词筛出商业秘密相关
    {"name": "Law360 知识产权", "url": "https://www.law360.com/ip/rss", "region": "国际", "weight": 2.5, "implies": ("legal",)},
    # —— 微信公众号（搜狗微信搜索，type=sogou：解析结果页并还原真实文章链接）——
    {"name": "微信公众号", "type": "sogou", "url": sogou("商业秘密"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "微信公众号", "type": "sogou", "url": sogou("竞业限制"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "微信公众号", "type": "sogou", "url": sogou("侵犯商业秘密"), "region": "国内", "weight": 2.0, "implies": ()},
    # —— 国内综合源（须命中专门词，或主题+法律双维度）——
    {"name": "36氪", "url": "https://36kr.com/feed", "region": "国内", "weight": 1.5, "implies": ()},
    {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "region": "国内", "weight": 1.5, "implies": ()},
    {"name": "cnBeta", "url": "https://www.cnbeta.com.tw/backend.php", "region": "国内", "weight": 1.5, "implies": ()},
    # —— Google News 关键词检索（结果混杂，同样要过闸门）——
    {"name": "Google News", "url": gnews("商业秘密"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("侵犯商业秘密 OR 商业秘密 判决"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("竞业限制 OR 竞业禁止 OR 保密协议"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("商业秘密 刑事 OR 经济间谍 OR 商业间谍"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("反不正当竞争 OR 商业秘密 保护"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("客户名单 OR 经营秘密 纠纷"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("离职员工 泄密 OR 带走 技术"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("不正当竞争 判决 OR 反不正当竞争 处罚"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("芯片 窃密 OR 技术泄密"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("知识产权犯罪 OR 侵犯知识产权 刑事"), "region": "国内", "weight": 2.0, "implies": ()},
    # 头部知产微信公众号的网站版/转载镜像（公众号本身无公开 RSS，经由 Google News 索引其各平台分发版）
    {"name": "Google News", "url": gnews('"知产力" OR "IPRdaily" OR "知识产权那点事" OR "知产财经"'), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("trade secret lawsuit OR trade secret misappropriation", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("economic espionage OR trade secret theft", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("non-compete agreement lawsuit OR NDA dispute", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("trade secrets law OR DTSA ruling", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("corporate espionage OR industrial espionage", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("employee poaching lawsuit OR non-solicitation", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("trade secret injunction OR confidentiality breach lawsuit", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("chip technology theft OR semiconductor trade secret", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    # —— 要闻回顾专用：when:60d 拉取近两个月的立法与案例 ——
    {"name": "Google News", "url": gnews("商业秘密 判决 OR 商业秘密 案例 when:60d"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("商业秘密 立法 OR 商业秘密 司法解释 when:60d"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("竞业限制 判决 OR 不正当竞争 案例 when:60d"), "region": "国内", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("trade secret verdict OR trade secret jury when:60d", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("trade secrets legislation OR non-compete rule when:60d", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
    {"name": "Google News", "url": gnews("economic espionage charges OR trade secret indictment when:60d", zh=False), "region": "国际", "weight": 2.0, "implies": ()},
]

# —— 相关性闸门 ——
# 一条资讯必须：命中「商业秘密专门词」（CORE，单独放行），
# 或同时命中「主题维度」×「法律维度」，才会收录。
# 专业信源通过 implies 预先满足相应维度（如 Fair Competition Law 天然 core）。
CORE_RE = re.compile(
    r"商业秘密|商业机密|商业间谍|经济间谍|产业间谍|竞业限制|竞业禁止|竞业协议|技术秘密|侵犯.{0,4}秘密|"
    r"不正当竞争|客户名单|技术泄密|知识产权犯罪|"
    r"trade secrets?|misappropriation|economic espionage|industrial espionage|corporate espionage|"
    r"non-?compete|non-?solicit|restrictive covenant|\bDTSA\b|\bUTSA\b", re.I)
TOPIC_RE = re.compile(
    r"保密协议|保密义务|保密信息|泄密|窃密|窃取|盗取|经营信息|经营秘密|商业情报|跳槽|挖角|挖人|内鬼|"
    r"离职.{0,8}(带走|泄|窃)|带走.{0,8}(技术|资料|代码|图纸|客户)|源代码|源码|配方|工艺|图纸|研发数据|"
    r"知识产权.{0,4}(刑事|保护|侵权)|"
    r"\bNDA\b|confidential information|confidentiality (?:agreement|breach)|insider theft|IP theft|"
    r"technology theft|proprietary (?:information|technology|data)|poach|client list|customer list", re.I)
LEGAL_RE = re.compile(
    r"合规|监管|执法|处罚|罚款|约谈|立案|诉讼|起诉|应诉|判决|裁决|裁定|判赔|法院|法庭|检察|批捕|公诉|获刑|"
    r"立法|法案|法律|条例|办法|规定|草案|征求意见|司法解释|指南|禁令|市场监管|知识产权局|"
    r"compliance|regulat|enforc|\bfine[sd]?\b|penalt|lawsuit|litigat|\bsue[sd]?\b|court|tribunal|ruling|"
    r"judgment|verdict|settlement|class action|legislat|\bbill\b|\bact\b|statute|indict|convict|sentenc|"
    r"guidance|consultation|attorney general|injunction|sanction|criminal|prosecut|\bFBI\b|\bjury\b", re.I)
# 噪音剔除：律所榜单/评级类公关稿、综合早晚报合集（标题主体与主题无关）、
# 投资者互动问答（股民问询类 IR 内容）、企业营销软文、影视娱乐
EXCLUDE_RE = re.compile(
    r"Legal 500|Chambers (USA|Global|Asia)|ranking|recogni[sz]e[sd]?\b|(律所|律师).{0,10}(荣誉|上榜|榜单|排名|推荐)|"
    r"权威榜单|靠谱的.{0,15}律师|"
    r"8点1氪|【早知道】|播早报|氪星晚报|早报丨|晚报丨|"
    r"研究院 ?\||研究报告|行业报告|白皮书|研报|"
    r"是否.{0,20}业务往来|投资者互动|互动平台|破局者|新标杆|领跑者|"
    r"背后的商业秘密|揭开.{0,8}的秘密|"
    r"电视剧|电影|剧集|小说|综艺", re.I)


def relevant(text, implies):
    if EXCLUDE_RE.search(text):
        return False
    if "core" in implies or CORE_RE.search(text):
        return True
    has_topic = "topic" in implies or TOPIC_RE.search(text)
    has_legal = "legal" in implies or LEGAL_RE.search(text)
    return bool(has_topic and has_legal)

# 关键词词库（命中加热度）
KEYWORDS = {
    3.0: ["商业秘密", "商业机密", "侵犯商业秘密", "经济间谍", "商业间谍", "竞业限制", "竞业禁止",
          "技术秘密", "反不正当竞争法", "司法解释", "trade secret", "trade secrets",
          "misappropriation", "economic espionage", "non-compete", "noncompete", "DTSA"],
    2.0: ["保密协议", "保密义务", "保密信息", "窃密", "泄密", "判赔", "禁令", "反不正当竞争",
          "不正当竞争", "经营信息", "客户名单", "技术泄密", "产业间谍", "知识产权犯罪",
          "恶意侵权", "天价", "跳槽", "挖角", "挖人", "内鬼", "源代码", "知识产权", "刑事", "获刑", "批捕",
          "NDA", "confidential information", "confidentiality", "restrictive covenant",
          "non-solicitation", "injunction", "verdict", "jury", "espionage", "indictment",
          "convicted", "FBI"],
    1.0: ["保密", "秘密", "窃取", "盗取", "诉讼", "判决", "和解", "赔偿", "员工", "离职",
          "lawsuit", "theft", "stolen", "court", "settlement", "ruling", "criminal",
          "former employee", "proprietary", "poach"],
}

# 分类规则（按顺序匹配，先命中者归类；案件类目在立法监管之前——
# 提到判决/定罪的"某某法案"新闻是案件而非立法）
CATEGORIES = [
    ("刑事打击", r"刑事|批捕|逮捕|拘留|公诉|获刑|判刑|量刑|有期徒刑|缓刑|经济间谍|间谍罪|抓获|犯罪|涉嫌.{0,8}罪|"
                r"criminal|indict|convict|sentenc|prison|plea|espionage|\bFBI\b|prosecutor"),
    ("诉讼仲裁", r"诉讼|仲裁|判决|裁决|法院|起诉|应诉|和解|判赔|被判|赔偿|索赔|集体诉讼|诉讼时效|"
                r"class action|lawsuit|litigation|arbitration|arbitral|tribunal|court|ruling|verdict|settlement|"
                r"\bjury\b|\bsue[sd]?\b|\bsuits?\b|\bawards?\b|\balleg|federal circuit|appeals court|statute of limitations|accrual"),
    ("行政执法", r"行政处罚|市场监管|约谈|执法|查处|通报|整改|立案调查|罚款|"
                r"\bfine[sd]?\b|penalt|enforcement action|sanction|337调查|\bITC\b"),
    ("立法监管", r"法案|条例|征求意见|立法|新规|办法|司法解释|指南|标准|草案|模板|发布|发文|出台|修订|"
                r"regulation|bill\b|directive|(?-i:\bAct\b)|guidance|guideline|consultation|draft|framework|rules?\b|rulemaking|template"),
    ("竞业与保密", r"竞业限制|竞业禁止|竞业协议|保密协议|保密义务|脱密期|竞业补偿|"
                 r"non-?compete|garden leave|\bNDA\b|confidentiality agreement|restrictive covenant"),
    ("窃密泄密", r"窃取|窃密|泄密|泄露|盗取|带走.{0,8}(资料|文件|代码|图纸)|theft|st(?:eal|ole|olen)|leak|exfiltrat"),
]
DEFAULT_CATEGORY = "企业实践"

# 「立法监管」的官方主体闸门：必须出现官方机构或法律文件名，
# 排除企业内部政策、产品合规等非官方内容（acronym 部分区分大小写，防止误命中普通单词）
OFFICIAL_RE = re.compile(
    r"网信办|国家知识产权局|知识产权局|市场监管总局|市场监管|商务部|工信部|公安部|司法部|国务院|人大|"
    r"最高人民法院|最高人民检察院|最高法|最高检|检察|法院|知识产权法庭|监管机构|监管部门|"
    r"政府|部委|主管部门|当局|欧盟|欧委会|欧洲(?:委员会|议会|理事会)|议会|立法机关|国家标准|征求意见|司法解释|"
    r"(?i:European (?:Commission|Parliament|Council)|Parliament|Congress|Senate|White House|"
    r"regulator|authorit|ministry|government|federal|attorney general|lawmaker|legislature|"
    r"statute|directive|ordinance|executive order|department of (?:justice|commerce|labor))|"
    r"\bFTC\b|\bDOJ\b|\bUSPTO\b|\bUSTR\b|\bSEC\b|\bITC\b|\bAct\b")


# 观点/倡议类文章（智库分析、判例评析、呼吁立法等）不算官方动作
OPINION_RE = re.compile(r"the case for|op-ed|呼吁|观点|倡议|there (?:are|is) limits?", re.I)

# 地区标注按标题内容覆盖信源默认值：外国辖区词 → 国际，中国主体词 → 国内
FOREIGN_RE = re.compile(
    r"美国|欧盟|欧洲|英国|德国|法国|韩国|日本|印度|新加坡|加拿大|澳大利亚|俄罗斯|越南|伊朗|"
    r"弗吉尼亚|加州|加利福尼亚|得克萨斯|德州|纽约|康涅狄格|俄勒冈|马萨诸塞|"
    r"韩元|美元|欧元|FTC|FBI|DOJ|GDPR|EDPB|DTSA|联邦巡回")
CHINA_RE = re.compile(
    r"中国|我国|中央|国内|网信办|国家数据局|国家知识产权局|市场监管总局|最高人民法院|最高人民检察院|"
    r"国务院|工信部|贸仲|自贸|(?i:China|Chinese|Beijing|Shanghai)|\bCAC\b")


def detect_region(title, default):
    f, c = bool(FOREIGN_RE.search(title)), bool(CHINA_RE.search(title))
    if f and not c:
        return "国际"
    if c and not f:
        return "国内"
    return default

# 里程碑事件词：命中则要闻重要度加分（首例判决、天价判赔、法律生效、重刑等）
LANDMARK_RE = re.compile(
    r"首例|首部|首个|正式(?:施行|生效|实施|通过)|表决通过|审议通过|创纪录|史上最|天价|顶格|"
    r"最高.{0,4}(?:判赔|赔偿|罚)|亿元|亿美元|亿欧元|获刑|有期徒刑|landmark|first[- ]ever|"
    r"record (?:fine|penalty|verdict|award)|billion|historic|supreme court|milestone|jury award", re.I)


def load_archive():
    if ARCHIVE_FILE.exists():
        try:
            return json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# 实体词限额用的英文常用词表（这些词不算"实体"）
COMMON_EN = {
    "data", "breach", "class", "action", "settlement", "privacy", "lawsuit", "gdpr",
    "fine", "fines", "court", "case", "cases", "against", "after", "with", "over",
    "into", "first", "record", "report", "annual", "federal", "state", "protection",
    "personal", "information", "security", "cyber", "compliance", "regulation",
    "enforcement", "authority", "issues", "guidance", "update", "updates", "draft",
    "rules", "bill", "should", "their", "about", "million", "billion",
    "trade", "secret", "secrets", "noncompete", "compete", "former", "employee",
    "theft", "verdict", "jury", "misappropriation", "espionage", "confidential",
}


# 案例栏的"具体案件/执法信号"：排除风险预警、机制建设类非案例新闻
CASE_SIGNAL_RE = re.compile(
    r"判|诉|裁|罪|获刑|赔偿|和解|处罚|查处|罚款|通报|"
    r"verdict|jury|lawsuit|litigat|\bsue[sd]?\b|\bsuits?\b|convict|sentenc|indict|settl|"
    r"court|ruling|\bawards?\b|\bfine[sd]?\b", re.I)
# 金额也视为事件指纹：同一判赔金额的多篇报道按同案处理
AMOUNT_RE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*(?:m\b|million|billion|万元?|亿元?|亿)", re.I)


def pick_highlights(archive_items):
    """从 60 天档案中选出立法与案例两栏要闻：按重要度排序，
    近似重复标题只保留最高分一条，同一实体/同一金额（视为同案）只保留一条。"""
    groups = {"legislation": ("立法监管",), "cases": ("诉讼仲裁", "刑事打击", "行政执法")}
    out = {}
    for key, cats in groups.items():
        pool = sorted((i for i in archive_items if i["category"] in cats),
                      key=lambda x: -x.get("importance", 0))
        picked, used_entities = [], set()
        for it in pool:
            if key == "cases" and not CASE_SIGNAL_RE.search(it["title"]):
                continue  # 案例栏须有具体案件/执法信号
            toks = norm_tokens(it["title"])
            if any(jaccard(toks, norm_tokens(p["title"])) >= 0.35 for p in picked):
                continue
            entities = {t for t in toks
                        if t.isascii() and t.isalpha() and len(t) >= 4 and t not in COMMON_EN}
            entities |= {"amt" + m for m in AMOUNT_RE.findall(it["title"])}
            if entities & used_entities:
                continue  # 同一事件主体/金额只保留重要度最高的一条
            used_entities |= entities
            picked.append(it)
            if len(picked) >= HIGHLIGHT_TOP_N:
                break
        out[key] = picked
    return out


def is_official(text):
    return bool(OFFICIAL_RE.search(text))

REASONS = {
    "立法监管": "商业秘密立法/监管新动向，可能调整保护边界与合规义务，建议跟进后续落地细则。",
    "刑事打击": "侵犯商业秘密刑事案件，反映入罪门槛与量刑尺度，可用于内部威慑宣导与刑民交叉策略参考。",
    "诉讼仲裁": "商业秘密民事争议案例，秘点界定、举证、禁令与赔偿口径对类案处理有参考价值。",
    "行政执法": "行政执法动态，反映监管查处重点与尺度，可对照排查自身泄密与合规风险点。",
    "竞业与保密": "竞业限制与保密协议实务动态，直接关系人才流动管控与保密体系设计。",
    "窃密泄密": "窃密/泄密事件通常引发民刑交叉追责，可作为应急响应与保密制度复盘素材。",
    "企业实践": "企业商业秘密管理实践动态，可作为保密体系建设与对标参考。",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # 部分源无视请求头强制 gzip
        raw = gzip.decompress(raw)
    return raw


def text_of(elem, *names):
    for n in names:
        found = elem.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def strip_html(s):
    return WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(s or ""))).strip()


def parse_time(s):
    if not s:
        return None
    s = WS_RE.sub(" ", s).strip()  # 36氪等源的日期含多余空格
    try:
        return email.utils.parsedate_to_datetime(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def real_url(link):
    """Bing News 的链接是跳转链，提取真实 URL。"""
    if "bing.com" in link and "url=" in link:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        if qs.get("url"):
            return qs["url"][0]
    return link


def parse_feed(raw, source):
    """解析 RSS 2.0 或 Atom，返回 item 字典列表。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # 部分源带非法字符，做一次清洗重试
        cleaned = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw)
        root = ET.fromstring(cleaned)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    # RSS 2.0
    for it in root.iter("item"):
        title = strip_html(text_of(it, "title"))
        link = real_url(text_of(it, "link"))
        desc = strip_html(text_of(it, "description"))
        pub = parse_time(text_of(it, "pubDate") or text_of(it, "{http://purl.org/dc/elements/1.1/}date"))
        src_el = it.find("source")
        src_name = (src_el.text or "").strip() if src_el is not None and src_el.text else source["name"]
        # Google News 标题尾部带 " - 来源名"，去掉
        if title.endswith(" - " + src_name):
            title = title[: -len(" - " + src_name)].strip()
        items.append({"title": title, "link": link, "summary": desc, "time": pub, "src": src_name})
    # Atom
    for it in root.findall("atom:entry", ns):
        title = strip_html(text_of(it, "atom:title"))
        link_el = it.find("atom:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        desc = strip_html(text_of(it, "atom:summary") or text_of(it, "atom:content"))
        pub = parse_time(text_of(it, "atom:published") or text_of(it, "atom:updated"))
        items.append({"title": title, "link": link, "summary": desc, "time": pub, "src": source["name"]})
    return items


def parse_html_list(raw, source):
    """解析无 RSS 官网的新闻列表页：source["item_re"] 须依次捕获 (相对链接, 标题, 日期)。
    列表页无具体时刻，统一记为当天 12:00（北京时间）。"""
    page = raw.decode("utf-8", "ignore")
    items = []
    for m in re.finditer(source["item_re"], page, re.S):
        href, title, date = m.group(1), strip_html(m.group(2)), m.group(3)
        date = re.sub(r"[年/月.]", "-", date).rstrip("日")
        try:
            pub = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)
        except ValueError:
            continue
        items.append({"title": title, "link": urllib.parse.urljoin(source["base"], href),
                      "summary": "", "time": pub, "src": source["name"]})
    return items


SOGOU_LOCK = threading.Lock()  # 搜狗请求全局串行+限速，避免触发反爬验证


def fetch_sogou(source):
    """搜狗微信搜索：抓取公众号文章。跳转链接带会话 token 会过期，
    需带搜索会话 Cookie 请求跳转页，从 JS 片段拼出真实 mp.weixin.qq.com 地址。"""
    with SOGOU_LOCK:
        return _fetch_sogou(source)


def _fetch_sogou(source):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", UA), ("Referer", "https://weixin.sogou.com/"),
                         ("Accept-Language", "zh-CN,zh;q=0.9")]

    def get(url):
        time.sleep(1.2)
        raw = opener.open(url, timeout=TIMEOUT).read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "ignore")

    page = get(source["url"])
    if 'id="sogou_vr_' not in page:
        if "antispider" in page.lower() or "验证" in page:
            raise RuntimeError("搜狗反爬限流，本轮跳过（通常数十分钟后自动恢复）")
        return []
    cutoff = datetime.now(TZ) - timedelta(days=HIGHLIGHT_DAYS)
    items = []
    for li in re.findall(r'<li[^>]*id="sogou_vr_[^"]*".*?</li>', page, re.S):
        m_title = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        m_ts = re.search(r"timeConvert\('(\d+)'\)", li)
        if not m_title or not m_ts:
            continue
        pub = datetime.fromtimestamp(int(m_ts.group(1)), TZ)
        if pub < cutoff:
            continue  # 旧文先过滤，避免为其解析真实链接（减少请求量防限流）
        link = urllib.parse.urljoin("https://weixin.sogou.com/", html.unescape(m_title.group(1)))
        try:
            frags = re.findall(r"url \+= '([^']*)'", get(link))
            real = "".join(frags).replace("@", "")
            if real.startswith("http"):
                link = real
        except Exception:
            pass  # 还原失败则保留搜狗跳转链接
        m_acc = re.search(r'class="account"[^>]*>(.*?)</a>', li, re.S)
        m_sum = re.search(r'class="txt-info"[^>]*>(.*?)</p>', li, re.S)
        items.append({"title": strip_html(m_title.group(2)),
                      "link": link,
                      "summary": strip_html(m_sum.group(1)) if m_sum else "",
                      "time": pub,
                      "src": strip_html(m_acc.group(1)) if m_acc else source["name"]})
    return items


def keyword_score(text):
    score, hits = 0.0, []
    low = text.lower()
    for w, words in KEYWORDS.items():
        for kw in words:
            if kw.lower() in low:
                score += w
                hits.append(kw)
    return min(score, 12.0), hits


def categorize(title, summary, official=False):
    # 标题优先：标题命中的类别比摘要里顺带提到的更能代表主题
    full = title + " " + summary
    for text in (title, summary):
        for name, pattern in CATEGORIES:
            if re.search(pattern, text, re.I):
                if name == "立法监管" and ((not official and not is_official(full)) or OPINION_RE.search(title)):
                    continue  # 立法监管须有官方主体（官网信源天然满足）且非观点文章
                return name
    return DEFAULT_CATEGORY


def norm_tokens(title):
    """标题归一化为 token 集合：英文按词，中文按双字组，用于 Jaccard 去重。
    注意 \\w 含汉字，中英混排片段须再按文字系统拆分（如"事故的coupang的制裁"）。"""
    t = re.sub(r"[^\w一-鿿]+", " ", title.lower())
    tokens = set()
    for part in t.split():
        for run in re.findall(r"[0-9a-z_]+|[一-鿿]+", part):
            if run[0] >= "一":  # 汉字串 → 双字组
                if len(run) > 1:
                    tokens.update(run[i : i + 2] for i in range(len(run) - 1))
                else:
                    tokens.add(run)
            else:
                tokens.add(run)
    return tokens


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fetch_source(source):
    try:
        if source.get("type") == "sogou":
            items = fetch_sogou(source)
        elif source.get("type") == "html":
            items = parse_html_list(fetch(source["url"]), source)
        else:
            items = parse_feed(fetch(source["url"]), source)
        return source, items, None
    except Exception as e:
        return source, [], str(e)


def fetch_all():
    now = datetime.now(TZ)
    cutoff = now - timedelta(days=HIGHLIGHT_DAYS)  # 收集 60 天，主列表稍后再截 7 天
    collected, errors = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for source, items, err in pool.map(fetch_source, SOURCES):
            if err:
                errors.append(f"{source['name']}: {err}")
                continue
            for it in items:
                if not it["title"] or not it["link"]:
                    continue
                t = it["time"]
                if t is None:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t = t.astimezone(TZ)
                if t < cutoff or t > now + timedelta(hours=2):
                    continue
                text = it["title"] + " " + it["summary"]
                if not relevant(text, source["implies"]):
                    continue
                score, hits = keyword_score(text)
                summary = it["summary"]
                if jaccard(norm_tokens(summary[:80]), norm_tokens(it["title"])) > 0.7:
                    summary = ""  # 摘要与标题重复则不展示
                if len(summary) > 180:
                    summary = summary[:178].rstrip() + "…"
                collected.append({
                    "title": it["title"], "url": it["link"], "summary": summary,
                    "time": t, "source": it["src"], "feed": source["name"],
                    "region": source["region"], "weight": source["weight"],
                    "kw_score": score, "keywords": hits,
                    "official": source.get("official", False),
                })

    # —— 聚类去重：标题相似的合并为一条，来源累加（即"n 个信源"）——
    clusters = []
    for item in sorted(collected, key=lambda x: -x["kw_score"]):
        tokens = norm_tokens(item["title"])
        for c in clusters:
            if jaccard(tokens, c["_tokens"]) >= 0.55:
                if item["source"] not in c["sources"]:
                    c["sources"].append(item["source"])
                c["time"] = max(c["time"], item["time"])
                c["official"] = c.get("official", False) or item.get("official", False)
                if len(item["summary"]) > len(c["summary"]):
                    c["summary"] = item["summary"]
                break
        else:
            item["_tokens"] = tokens
            item["sources"] = [item["source"]]
            clusters.append(item)

    # —— 检索类信源限流：同一外部媒体/公众号最多 3 条，避免营销类账号刷屏 ——
    capped, per_src = [], {}
    for c in sorted(clusters, key=lambda x: -(x["kw_score"] + x["weight"])):
        if c["feed"] in ("Google News", "微信公众号"):
            n = per_src.get(c["source"], 0)
            if n >= 3:
                continue
            per_src[c["source"]] = n + 1
        capped.append(c)
    clusters = capped

    # —— 热度、分类与重要度 ——
    results = []
    for c in clusters:
        age_h = (now - c["time"]).total_seconds() / 3600
        recency = max(0.0, 4.0 - age_h / 18.0)  # 连续衰减：新发布 +4，72 小时后归零
        heat = round(c["kw_score"] + c["weight"] + recency + 2.0 * (len(c["sources"]) - 1), 1)
        category = categorize(c["title"], c["summary"], c.get("official", False))
        top_kw = sorted(set(c["keywords"]), key=lambda k: -len(k))[:3]
        # 重要度（要闻排序用）：不含时效项，立法/案例类别与里程碑事件加分
        importance = c["kw_score"] + c["weight"] + 2.0 * (len(c["sources"]) - 1)
        if category == "立法监管":
            importance += 3.0
        elif category in ("诉讼仲裁", "刑事打击", "行政执法"):
            importance += 2.5
        if LANDMARK_RE.search(c["title"]):
            importance += 3.0
        results.append({
            "title": c["title"], "url": c["url"], "summary": c["summary"],
            "time": c["time"].isoformat(), "date": c["time"].strftime("%Y-%m-%d"),
            "sources": c["sources"], "region": detect_region(c["title"], c["region"]), "category": category,
            "heat": heat, "hot": False, "tags": top_kw,
            "importance": round(importance, 1),
            "reason": REASONS[category],
        })

    # —— 档案：60 天滚动合并（跨运行累积，供「要闻」回顾）——
    archive = load_archive()
    for r in results:
        archive[r["url"]] = r
    cut60 = (now - timedelta(days=HIGHLIGHT_DAYS)).isoformat()
    archive = {u: it for u, it in archive.items() if it["time"] >= cut60}
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
    highlights = pick_highlights(list(archive.values()))

    # —— 主列表（实时/精选/全部动态）只取近 7 天 ——
    cut7 = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    items_main = [r for r in results if r["time"] >= cut7]
    for r in sorted(items_main, key=lambda x: -x["heat"])[:10]:
        r["hot"] = True  # 全局热度 Top 10 标记为热点
    items_main.sort(key=lambda x: (x["date"], x["heat"]), reverse=True)

    data = {
        "generated_at": now.isoformat(),
        "count": len(items_main),
        "highlight_count": sum(len(v) for v in highlights.values()),
        "errors": errors,
        "items": items_main,
        "highlights": highlights,
    }
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = fetch_all()
    print(f"抓取完成：{d['count']} 条资讯 → {OUT_FILE}")
    if d["errors"]:
        print("以下信源抓取失败（已跳过）：", file=sys.stderr)
        for e in d["errors"]:
            print("  -", e, file=sys.stderr)
