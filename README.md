# TS·HOT — 商业秘密热点

聚焦**商业秘密**领域的国内外资讯聚合页面，与同目录的 DC·HOT（数据合规与争议解决）同构，逻辑参照 aihot.virxact.com：
多信源 RSS 聚合 → 相关性闸门 → 自动分类 → 相似标题聚类去重（"n 个信源"）→ 热度排名（随时间消退）→ 卡片式榜单（实时/精选/要闻/全部动态，含分类筛选、国内/国际筛选、搜索、按日期分组、入选理由）。

纯 Python 标准库 + 单文件前端，无任何第三方依赖。

## 快速开始

```bash
cd ts-hot
python3 app.py          # 默认 http://localhost:8766 ，首次启动自动抓取（与 DC·HOT 的 8765 可并行运行）
python3 app.py 9001     # 指定端口
```

页面默认打开「实时」标签页；右上角「↻ 刷新」触发服务端重新抓取。

也可以只生成数据不开服务：`python3 fetch_news.py`

## 核心规则（与 DC·HOT 一致的机制，换了领域配置）

**相关性闸门**：命中商业秘密专门词（`CORE_RE`：商业秘密/竞业限制/经济间谍/trade secret/misappropriation…）单独放行；否则须同时命中「主题维度」（`TOPIC_RE`：保密协议/泄密/窃密/NDA…）×「法律维度」（`LEGAL_RE`：诉讼/判决/立法/刑事…）。剔除律所榜单公关稿、早晚报合集、投资者互动问答、营销软文、影视娱乐（`EXCLUDE_RE`），Google News 同一外部媒体限 3 条。

**分类**（七类，标题优先匹配）：立法监管（仅官方部门，`OFFICIAL_RE` 闸门 + `OPINION_RE` 排除观点文）/ 刑事打击 / 诉讼仲裁 / 行政执法 / 竞业与保密 / 窃密泄密 / 企业实践（默认）。

**实时更新**：服务端每 10 分钟（`app.py` 的 `REFRESH_MINUTES`）自动重抓；页面每 60 秒轮询，新条目打 NEW 标；「实时」标签页完整展示近 48 小时全部资讯，统一按北京时间倒序（前端强制 Asia/Shanghai）。

**要闻回顾**：近两个月（`HIGHLIGHT_DAYS=60`）重要动态两栏 ——「重要立法与监管」（立法监管）与「重要案例与执法」（诉讼仲裁/刑事打击/行政执法）。重要度 = 关键词分 + 信源权重 + 类别加成 + 里程碑加成（`LANDMARK_RE`：首例/天价判赔/获刑/record verdict…）+ 多信源加成；近似重复只留最高分一条、同一实体最多 2 条；数据存 60 天滚动档案 `archive.json`，配 `when:60d` 查询补历史。

**热度公式**：`关键词分(≤12) + 信源权重 + 时效加成(连续衰减，72h 归零) + 2×(信源数-1)`，全局 Top 10 标 🔥。

## 信源

- **国际**：Fair Competition Law（竞业/商业秘密专业博客，整站在题）、DOJ 新闻稿、FTC 新闻稿（均按主题词过滤），及 Google News 英文检索（trade secret lawsuit / economic espionage / non-compete / DTSA）
- **国内**：36氪、Solidot、cnBeta（关键词过滤），及 Google News 中文检索（商业秘密 / 侵犯商业秘密判决 / 竞业限制 / 刑事·经济间谍 / 反不正当竞争）
- 实测不可用已排除：Seyfarth Trading Secrets、Crowell Trade Secrets Trends（403 WAF）、IPWatchdog（超时）、natlawreview（404）

## 自定义与部署

同 DC·HOT：信源/词库/分类都在 `fetch_news.py` 顶部；静态托管时 `index.html` 回退读同目录 `news.json`（配 cron 定时跑 `fetch_news.py`）。本页面仅供学习研究参考，不构成法律意见。
