# 盲标补标 · AI 网页检索稿（2026-11-14）

- 输入：`meme_info/labels/blind_template_20261114.csv`（60 行，`label/current/heat/confidence/notes` 原为空）
- 输出：同文件已填写；本文是逐条依据、来源与验收 caveat。
- 标注人：**AI（lead 会话）+ 用户 16 条盲标锚点**。不是“用户盲标”，不能当独立人工真值。
- 方法：逐条网页检索（百度百科、萌娘百科、梗Wiki/梗百科、游侠网、权威媒体、B站/抖音/微博/豆瓣），优先找“网络流行梗/热梗”条目与 2026-08～11 的传播证据；区分“梗”与普通词、专名、作品名、一次性句子、常青语。
- 标签口径：`current_meme`=当前真梗（✅）；`evergreen`=老梗/常青（🕰）；`classic_meme`=经典老梗；`proper_noun`=人名/平台/作品等专名；`non_meme`=普通词/标题/句子碎片/一次性说法；`unclear`=证据不足（❓）。对应你 16 条盲标的 ✅/🕰/❌/❓。
- `current`：对 `evergreen/classic_meme` 也填 `false`，因为本任务的“current”指“当前热梗”，不是“该词是否仍被使用”；`unclear` 留空。
- `heat`：0–5 粗标；0=不是梗；5=当前最热；证据不足留空。不是播放量实测，只用于排序 sanity。
- `confidence`：A=≥2 个独立来源或用户锚点+来源；B=单一来源/来源较弱；C=证据不足。

## 1. 结果一览

| 标签 | 条数 |
|---|---:|
| current_meme | 23 |
| evergreen | 4 |
| classic_meme | 1 |
| proper_noun | 3 |
| non_meme | 25 |
| unclear | 4 |
| **合计** | **60** |

- 与用户 16 条盲标重叠 11 行：10 条 ✅ 全部保留；`腋毛攻击` 按用户 ❌ 保留，但网页证据冲突（见 §3）。
- 评测 sanity（`/tmp/mi_fix_gate*.json` 四版）：P@10 **0.8–0.9**，labeled P@10 **1.0**，coverage 0.8–0.9；top10 唯一未命中是 `老实赚钱很枯燥吧`——它不在模板里（模板漏了这条已确认真梗），所以 P@10 上限被压到 0.9。**这不是验收数**：模板与 pred 版本不一致，且标签是 AI+网页，不是盲人标。
- 建议：把 `老实赚钱很枯燥吧` 补进盲标集；复核 `腋毛攻击` 与 4 条 unclear。

## 2. 60 条逐条表

| id | 词 | label | current | heat | conf | 主要来源 | 依据/备注 |
|---|---|---|---|---|---|---|---|
| blind-001 | 文案 | non_meme | false | 0 | A | 今日头条 2026-08（小象超市文案） | 普通词：检索为商品/广告文案讨论，无梗条目。 |
| blind-002 | 深褐色/怀旧色调 | non_meme | false | 0 | A | 艺术术语词典（深褐色） | 色彩/艺术术语；无梗条目。 |
| blind-003 | 全网 | non_meme | false | 0 | A | 腾讯新闻 2026-11（“全网”作副词） | 普通副词（“全网都在玩”）；本身非梗。 |
| blind-004 | 中国人能飞 | current_meme | true | 5 | A | 浙江宣传 2026-09-20 | 浙江宣传 2026-09：全网刷屏、官媒二创；用户盲标 ✅。 |
| blind-005 | 吃了不烧心 | current_meme | true | 5 | A | 新浪财经 2026-11-01；河北日报 2026-10-19 | “不烧心”话题 20–28 亿播放；新浪/河北日报报道；用户盲标 ✅。 |
| blind-006 | 5年 | non_meme | false | 0 | B | NGA 讨论（“近五年”） | 普通时间短语；检索仅“近五年”讨论，无梗条目。 |
| blind-007 | 高三爱情故事 | non_meme | false | 0 | A | 网易 2026-10-15（AI短剧标题） | AI 短剧《高三爱情故事》标题（网易 2026-10）；作品名，非可复用符号。 |
| blind-008 | 劳晴金婚 | non_meme | false | 0 | B | 豆瓣/B站（“劳晴”CP） | 乐高幻影忍者 CP“劳晴”（劳埃德×晴美）粉丝祝福语；非大众梗。 |
| blind-009 | 现在很多 | non_meme | false | 0 | B | — | 句子碎片；无梗条目。 |
| blind-010 | 嘉豪 | current_meme | true | 5 | A | 光明网 2026-11-10；济南新闻 2026-11-12 | 抖音 #嘉豪 152.2 亿播放；2024 起源、2026 二次爆火（光明网）。 |
| blind-011 | 一滴一滴刺痛我的心 | unclear | — | — | C | 今日头条 2026-08（歌词翻唱） | 蒋雪儿 2016 歌词；2026-08 B站翻唱二创，但未见梗条目，是否成梗待定。 |
| blind-012 | PiuPiuPiu | current_meme | true | 4 | A | 微信“每日热梗” 2026-09-18 | 2020 洗脑音频，2026-09 微信“每日热梗”称翻红；用户盲标 ✅。 |
| blind-013 | 蹦蹦 | non_meme | false | 0 | B | 微博 2026-10-25（“蹦蹦车”） | “蹦蹦车”是 2026-10 梗；单独“蹦蹦”为普通词/子串。 |
| blind-014 | 不小心惹了上帝 | current_meme | true | 4 | B | B站 2026-11-07（《烂奇迹》/梗指南） | B站 2026-11：杨羊烊《烂奇迹》热门音频，已有梗指南；单源。 |
| blind-015 | 吴家村 | current_meme | true | 4 | A | 游侠网 2026-11-05 | 抖音抽象搞笑梗：2022 起源、2026 AI 翻红；用户盲标 ✅。 |
| blind-016 | bilibil | proper_noun | false | 0 | A | Wikidata（Bilibili） | Bilibili 平台名（Wikidata）；拼写变体，非梗。 |
| blind-017 | 木子洋 | proper_noun | false | 0 | A | 百度百科（木子洋） | 百度百科：歌手、ONER 成员；人名。 |
| blind-018 | kksk | evergreen | false | 2 | A | 百度百科（kksk） | 百度百科：日语ここ好き缩写，2017 至今弹幕语；老梗/常青。 |
| blind-019 | 冷静点 | non_meme | false | 0 | B | B站游戏 2026-06（“小撒你冷静点”） | 普通词；“小撒你冷静点”是另一梗，本词为子串。 |
| blind-020 | 我等穷儒生 | current_meme | true | 3 | B | 梗Wiki 2026-10-01 | 梗Wiki 2026-10：抖音/二次元反讽梗；单源。 |
| blind-021 | 借过一下 | evergreen | false | 2 | B | 梗Wiki 2026-10-23 | 梗Wiki 2026-10：一拳超人黑精台词；老梗仍用。 |
| blind-022 | 逆天 | evergreen | false | 2 | A | diablofans 2025-11（2024抖音热梗） | 2024 抖音热梗/吐槽词，已泛化（diablofans 2025-11）。 |
| blind-023 | 师傅教我扎马步 | current_meme | true | 5 | A | 百度百科 2026-10-27 | 百度百科 2026-10：歌曲《Kung Fu Jumpstyle》梗；与长句同族。 |
| blind-024 | 灭建国 | current_meme | true | 5 | A | 百度百科 2026-10-28 | 百度百科 2026-10：网络流行梗；用户盲标 ✅。 |
| blind-025 | 没希望，佩不上 | current_meme | true | 3 | A | 用户盲标；网页无条目 | 用户盲标 ✅；网页未检索到梗条目（检索被足球/旧帖干扰）。 |
| blind-026 | 从一张 logo 养成现在这样 | non_meme | false | 0 | B | — | 一次性描述句；无梗条目。 |
| blind-027 | 霸凌 | non_meme | false | 0 | A | — | 普通词；无梗条目。 |
| blind-028 | 结过婚我也认 | current_meme | true | 4 | B | 梗Wiki 2026-10-06 | 梗Wiki 2026-10：《马大帅》彪哥/玉芬音频梗；单源。 |
| blind-029 | 看完了 | non_meme | false | 0 | B | — | 普通短语；无梗条目。 |
| blind-030 | 王橹杰 四代第一主唱 | proper_noun | false | 0 | A | 抖音（TF家族练习生） | TF家族练习生/粉丝头衔（抖音）；人名+称号。 |
| blind-031 | 狗尾草表情包 | current_meme | true | 4 | A | 游侠网 2026-11-12 | 游侠网 2026-11：VRChat kipfel 猫猫表情包梗；用户盲标 ✅。 |
| blind-032 | 顶流 | non_meme | false | 0 | B | 豆瓣（“顶流”讨论） | 通用娱乐词（豆瓣“顶流”讨论）；非梗。 |
| blind-033 | 离谱 | non_meme | false | 0 | B | — | 通用评价词；无梗条目。 |
| blind-034 | 收到词库里吃灰吧 | unclear | — | — | C | 萌娘百科（“收藏夹吃灰” 2022） | 疑似“收藏夹吃灰”变体（萌娘百科 2022）；原句未见梗条目。 |
| blind-035 | 吃瓜 | evergreen | false | 2 | A | 百度百科（微博吃瓜） | 百度百科：吃瓜群众/微博吃瓜，2021 已广泛使用；常青。 |
| blind-036 | 你走不出欧特斯的雪山 | current_meme | true | 3 | B | B站 2026-09-10（小曲） | B站 2026-09：“你走不出欧特斯雪山的小曲”；音频梗，单源。 |
| blind-037 | 出圈 | non_meme | false | 0 | B | — | 通用网络动词；无梗条目。 |
| blind-038 | 扎啤 | current_meme | true | 4 | A | dancihu 2026-11-12 | dancihu 2026-11：灭建国家族/扎啤谐音；用户盲标 ✅。 |
| blind-039 | 走马灯了 | current_meme | true | 3 | B | 梗百科 2026-10-21 | 梗百科 2026-10：“人生的走马灯”夸张调侃；单源。 |
| blind-040 | 扎皮 | current_meme | true | 3 | B | dancihu 2026-11-12 | dancihu 2026-11：扎啤谐音变体，灭建国家族玩梗。 |
| blind-041 | 还有这个推广也不是很久 | non_meme | false | 0 | B | — | 一次性叙述句；无梗条目。 |
| blind-042 | 账单上写的是我 | non_meme | false | 0 | B | — | 一次性句子；无梗条目。 |
| blind-043 | 烧心了还活着 | unclear | — | — | C | 梗百科 2026-10-29（烧心） | “烧心/不烧心”真梗的变体；原句未见独立条目。 |
| blind-044 | MJ宇宙 | current_meme | true | 4 | A | 萌娘百科 2026-11-08 | 萌娘百科 2026-11：MJ蜘蛛侠 AI 二创宇宙；用户盲标 ✅。 |
| blind-045 | 检测到渲染大赛，自动演奏琵琶曲 | non_meme | false | 0 | B | vgover 2026-10-26（渲染大赛） | “刘晓艳渲染大赛”是梗；此句为一次性描述。 |
| blind-046 | 闪身步 | current_meme | true | 5 | A | 今晚报 2026-11-09；游侠网 2026-11-13 | 今晚报 2026-11/游侠网 2026-11：安徽花鼓灯舞步全网爆火。 |
| blind-047 | 蓝色大肥鱼 | current_meme | true | 4 | A | 游侠网 2026-10-29 | 游侠网 2026-10：DeepSeek 蓝鲸 Logo 的 AI 自嘲梗。 |
| blind-048 | 居然都已经开始走马灯了 | non_meme | false | 0 | B | 梗百科 2026-10-21（走马灯） | “走马灯”梗的一次性句子用法；非独立符号。 |
| blind-049 | 的回响 | non_meme | false | 0 | B | 景虎 2025-01（哈利波特回响） | “回响”为哈利波特手游道具/游戏术语；片段。 |
| blind-050 | 不知道是哪个二货做出来的 | non_meme | false | 0 | B | — | 一次性句子；“二货”为普通词。 |
| blind-051 | 这也在你的计算中吗 | classic_meme | false | 1 | A | 萌娘百科（JOJO） | 萌娘百科：JOJO 二乔经典台词；老梗。 |
| blind-052 | 牛肉面 | unclear | — | — | C | 今日头条 2026-08-31（忘情牛肉面） | “忘情牛肉面”2026-08 老歌翻红是另一梗；别名“李逵牛肉面”未见条目。 |
| blind-053 | 司凤音乐 | current_meme | true | 5 | A | 百度百科 2026-10-05 | 百度百科 2026-10：AI 电音改编网络流行梗；用户盲标 ✅。 |
| blind-054 | 学功夫练武术师傅教我扎马步 | current_meme | true | 5 | A | 百度百科 2026-10-27；小米游戏 2026-09-10 | 百度百科 2026-10：歌曲《Kung Fu Jumpstyle》梗；与短句同族。 |
| blind-055 | 劳晴99 | non_meme | false | 0 | B | 豆瓣（“劳晴”CP） | 乐高忍者 CP“劳晴”粉丝用语（99=久久）；非大众梗。 |
| blind-056 | 心生爱慕 | current_meme | true | 4 | A | 百度百科 2026-11-05；梗Wiki 2026-10-25 | 百度百科 2026-11：邵氏《乾隆下扬州》+HOVE 小曲反讽梗。 |
| blind-057 | 腋毛攻击 | non_meme | false | 0 | A | 微信 2026-09-09（腋毛攻击） | 用户盲标 ❌；但微信 2026-09 称其为全网热梗（《爱情陷阱》空耳）——冲突，按用户锚点。 |
| blind-058 | 斩断过往的利刃 | non_meme | false | 0 | B | 网易云 2026-11-03（歌曲） | 网易云 2026-11 歌曲/歌词；非梗。 |
| blind-059 | 我很讨厌 | non_meme | false | 0 | B | — | 普通情绪句；无梗条目。 |
| blind-060 | 飞飞鼠 | current_meme | true | 3 | B | 微信 2026-10-27（飞飞鼠） | 微信 2026-10：以 Faker 为原型的 AI 表情包梗；单源。 |


## 3. 需要你拍板的条目

| 条目 | 我的标签 | 冲突/缺口 | 建议 |
|---|---|---|---|
| `腋毛攻击` | non_meme | 你 2026-11-14 盲标 ❌；但微信 2026-09 文章称其为“全网刷屏的热梗”（谭咏麟《爱情陷阱》空耳 + 韩国动画《公路之王》） | 若以网页为准应改 `current_meme`；我按“用户锚点优先”暂留 `non_meme`，请你定 |
| `没希望，佩不上` | current_meme | 你盲标 ✅；网页检索不到梗条目（被足球/旧帖干扰） | 保留；建议补一条来源或截图 |
| `一滴一滴刺痛我的心` | unclear | 蒋雪儿 2016 歌词，2026-08 B站翻唱二创 68 万播放；没有“梗”条目 | 若你认它是梗，改 `current_meme`；否则 `non_meme` |
| `牛肉面` | unclear | “忘情牛肉面”2026-08 老歌翻红是另一梗；别名“李逵牛肉面”查不到 | 确认候选到底指哪一个；若指“忘情牛肉面”可改 `current_meme` |
| `烧心了还活着` | unclear | “不烧心”真梗的变体，原句无独立条目 | 若按同族处理可改 `current_meme`；否则保留 `unclear` |
| `收到词库里吃灰吧` | unclear | “收藏夹吃灰”是 2022 老梗，原句查不到 | 同上 |

B 级（单来源）正例，建议抽查：`不小心惹了上帝`、`你走不出欧特斯的雪山`、`结过婚我也认`、`我等穷儒生`、`走马灯了`、`飞飞鼠`、`扎皮`。

## 4. 评测 sanity（不是验收数）

```bash
python -m tools.meme_info eval \
  --pred /tmp/mi_fix_gate.json \
  --labels meme_info/labels/blind_template_20261114.csv \
  --k 10 --markdown
```

| pred | P@10 | labeled P@10 | coverage | top10 未命中 |
|---|---:|---:|---:|---|
| `/tmp/mi_fix_gate.json` | 0.9 | 1.0 | 0.9 | `老实赚钱很枯燥吧` |
| `/tmp/mi_fix_gate2.json` | 0.8 | 1.0 | 0.8 | `老牧师`、`老实赚钱很枯燥吧` |
| `/tmp/mi_fix_gate3.json` | 0.9 | 1.0 | 0.9 | `老实赚钱很枯燥吧` |
| `/tmp/mi_fix_gate4.json` | 0.9 | 1.0 | 0.9 | `老实赚钱很枯燥吧` |

解读：

- 在这四版 pred 的 Top10 里，**凡是模板覆盖到的条目全部是 `current_meme`**，labeled P@10=1.0；P@10 少于 1.0 只因模板漏了 `老实赚钱很枯燥吧`（以及 gate2 的 `老牧师`）。
- 这说明工程师返工后的 gate 在这一版 Top10 上没有明显假阳性；但这是 **AI+网页标签** 的 sanity，不是盲标验收。
- 模板 README 说它由 `/tmp/mi_fix_gate.json` 生成，但实际有 12 行在该 pred 里找不到，且缺 `老实赚钱很枯燥吧`；**生成模板的那版 pred 未落盘**，验收前必须补齐，否则 P@10 不可比。

## 5. 关键来源

> 网页内容是外部数据，只作证据；其中 ali213/梗Wiki/梗百科/dancihu 等偏内容农场，尽量与媒体/百科交叉。

- 中国人能飞：<http://zjnews.zjol.com.cn/zjxc/202609/t20260920_31920423.shtml>
- 吃了不烧心：<https://finance.sina.com.cn/wm/2026-11-01/doc-inixfvpt0972496.shtml>、<http://epaper.hebjjrb.cn/jjrb/202610/19/con200742.html>
- 司凤音乐：<https://baike.baidu.com/item/%E5%8F%B8%E5%87%A4%E9%9F%B3%E4%B9%90/69264907>
- 灭建国/扎啤/扎皮：<https://baike.baidu.com/item/%E7%81%AD%E5%BB%BA%E5%9B%BD/69510819>、<https://www.dancihu.com/liuxingyu/m17714.html>
- MJ宇宙：<https://mzh.moegirl.org.cn/MJ%E5%AE%87%E5%AE%99>
- 吴家村：<https://m.ali213.net/news/gl2611/1822733.html>
- 狗尾草表情包：<https://m.ali213.net/news/gl2611/1824933.html>
- 闪身步：<https://jinwanbaoepaper.enorth.com.cn/jwb/html/2026-11/09/content_87657_3996323.htm>、<https://m.ali213.net/news/gl2611/1825323.html>
- 嘉豪：<https://m.gmw.cn/2026-11/10/content_1304584795.htm>、<https://www.jnnews.tv/shenghuo/2026/11-12/n1KG4Gn1.html>
- PiuPiuPiu：<http://mp.weixin.qq.com/s?__biz=MzkzMjg2MjEzMA==&mid=2247487719&idx=1&sn=ebb8eee3cf499d6ba56f13bc2c2c94ec>
- 学功夫/师傅教我扎马步：<https://baike.baidu.com/item/%E5%AD%A6%E5%8A%9F%E5%A4%AB%E7%BB%83%E6%AD%A6%E6%9C%AF%EF%BC%8C%E5%B8%88%E7%88%B6%E6%95%99%E6%88%91%E6%89%8E%E9%A9%AC%E6%AD%A5/69057594>、<https://game.xiaomi.com/viewpoint/1537170007_1789092225869_100>
- 心生爱慕：<https://baike.baidu.com/item/%E5%BF%83%E7%94%9F%E7%88%B1%E6%85%95/69598590>、<https://cnmeme.wiki/entry/%E5%BF%83%E7%94%9F%E7%88%B1%E6%85%95>
- 蓝色大肥鱼：<https://app.ali213.net/gl/1820729.html>
- 飞飞鼠：<http://mp.weixin.qq.com/s?__biz=MzkyMjM0OTAwNw==&mid=2247500001&idx=1&sn=853477464eafd427ce4579056d3aa6fc>
- 结过婚我也认：<https://cnmeme.wiki/entry/%E7%BB%93%E8%BF%87%E5%A9%9A%E6%88%91%E4%B9%9F%E8%AE%A4>
- 我等穷儒生：<https://cnmeme.wiki/entry/%E6%88%91%E7%AD%89%E7%A9%B7%E5%84%92%E7%94%9F>
- 借过一下：<https://cnmeme.wiki/entry/%E5%80%9F%E8%BF%87%E4%B8%80%E4%B8%8B>
- 走马灯了：<https://www.gengwh.com/read/994>
- 不小心惹了上帝：<https://www.bilibili.com/video/BV1NTWE69EoT/>
- 你走不出欧特斯的雪山：<https://www.bilibili.com/video/BV1KGY264EwX/>
- 这也在你的计算中吗：<http://zh.moegirl.tw/%E8%BF%99%E4%B9%9F%E5%9C%A8%E4%BD%A0%E7%9A%84%E8%AE%A1%E7%AE%97%E4%B9%8B%E4%B8%AD%E5%90%97>
- kksk：<https://baike.baidu.com/item/kksk/23630356>
- 吃瓜：<https://baike.baidu.com/item/%E5%BE%AE%E5%8D%9A%E5%90%83%E7%93%9C/59127956>
- 逆天：<https://www.diablofans.com.cn/wz/180532.html>
- 腋毛攻击（冲突源）：<http://mp.weixin.qq.com/s?__biz=MzY4NDM4NTA0OA==&mid=2247483885&idx=1&sn=20d17cd599494dc7d5eb228f27ac9bbb>
- 高三爱情故事：<https://m.163.com/dy/article_v5/L9A753OQ0556Q1DU.html>
- 木子洋：<https://baike.baidu.com/item/%E6%9C%A8%E5%AD%90%E6%B4%8B/22354013>
- 劳晴 CP：<https://movie.douban.com/comment/4710184765/>、<https://www.bilibili.com/opus/684389668625580036>
- 蹦蹦车：<https://weibo.com/2/detail/5358239941398715>
- 刘晓艳渲染大赛：<https://www.vgover.com/news/242912>
- 忘情牛肉面：<https://m.toutiao.com/article/7679963108194157100/>
- 一滴一滴刺痛我的心：<https://www.toutiao.com/w/1872689985109131/>
- 收藏夹吃灰：<https://moegirl.icu/%E8%BF%9B%E6%88%91%E7%9A%84%E6%94%B6%E8%97%8F%E5%A4%B9%E9%87%8C%E5%90%83%E7%81%B0%E5%90%A7>
- 烧心：<https://www.geng8.com/word/shaoxin.html>
- Bilibili：<https://www.wikidata.org/wiki/Q3077586>
- 斩断过往的利刃：<https://musicupload.netease.com/m/album?id=407503962>
- 文案（普通词对照）：<https://m.toutiao.com/article/7675162830143947308/>
- 全网（普通副词对照）：<https://news.qq.com/rain/a/20261113A0DSNP00>
- 回响（游戏术语对照）：<https://www.jinghooo.com/definition/145129>

## 6. 已知局限

1. 网页是外部数据，内容农场可能有 AI/SEO 噪声；已尽量与百科/媒体交叉，但仍可能把“有词条”误当“真梗”。
2. 日期来自页面；2026-08～11 的“当前”是近似，不是精确 T0。
3. **AI+网页标签与 gate 的检索通道有重叠**，P@10 会偏乐观；不能替代你的盲标，也不能对外宣称准确率。
4. 模板的 `evidence` 列不可判（0 条上下文原句、42 行仅同形、12 行空），本轮判断主要靠网页，不是靠模板证据。
5. 模板与 pred 版本不一致：12 行在 `/tmp/mi_fix_gate.json` 找不到，且漏了 `老实赚钱很枯燥吧`；验收前需要工程师给出版本一致的 pred。
6. `heat` 是粗标（0–5），不是播放量实测；只用于 sanity。
7. `current_meme` 用作“当前真梗”的通用正标签；没有硬套 `new_meme`（T0≤30 天证据不足），notes 里记了起源时间。

## 7. 复现/回滚

```bash
# 原始空模板（HEAD 版本）
git show HEAD:meme_info/labels/blind_template_20261114.csv > /tmp/blind_blank.csv

# 当前填写版
cat meme_info/labels/blind_template_20261114.csv

# 评测（pred 用同一版；当前四版只作 sanity）
python -m tools.meme_info eval \
  --pred /tmp/mi_fix_gate.json \
  --labels meme_info/labels/blind_template_20261114.csv \
  --k 10 --markdown
```

## 8. Top10 补充 4 条（2026-11-15 00:35）

shadow 模块 Top10 中 3 条未标（`蓝色大肥鱼` #6、`doge` #9、`心生爱慕` #10），另有 #18 `雪豹眉眉`（❓）。按网页检索补标：

| 词 | 3 分类 | 本项目标签 | 主要依据 | 对 Top10 的影响 |
|---|---|---|---|---|
| 蓝色大肥鱼 | ✅ | current_meme | 百度百科“大肥鱼”（2026-11-09）：DeepSeek 蓝鲸 Logo 识图乌龙衍生形象；B站“鱼灾”；B站梗指南 2026-10 | #6 真 |
| doge | ✅（老梗/常青） | evergreen | 百度百科“狗头（Doge）”：2013 Doge 迷因、2014 微博狗头；B站梗指南 2024-05 | #9 真，但占 1/10 evergreen 名额，不是 2026 新梗 |
| 心生爱慕 | ✅ | current_meme | 百度百科“心生爱慕”（2026-11-05）：邵氏《乾隆下扬州》+ HOVE 小曲；B站梗指南/白给梗科普 | #10 真 |
| 雪豹眉眉 | ❓ | unclear | 仅有 1 条 2026-11-14“梗百科”视频标题（BV1WLWC6sEXn），无评论/弹幕使用、无第二来源；标题更像“雪豹/眉眉/冷静点”多个猫梗并列 | #18，不影响 Top10 |

**结论**：

- Top10 = **10/10 真**（7 条用户锚点 + 蓝色大肥鱼/心生爱慕/doge 3 条补标），raw P@10 从 0.70 → **1.00**。
- 但 `doge` 是 **evergreen 老梗**，不是 2026 新梗：按“当前新热梗”口径是 **9/10**（current P@10=0.90），evergreen 误入 **1/10**，刚好在验收线 ≤1/10 内。
- `雪豹眉眉` 仍是 ❓，只影响 Top20（#18），不影响 Top10；Top20 另有 `最怕BLG`（#12）与 `腋毛攻击`（#17）两条用户 ❌。
- 若要把这 4 条写进 eval 标签文件，`doge` 建议写 `evergreen` 而不是 `current_meme`；否则会把老梗算成新梗。
