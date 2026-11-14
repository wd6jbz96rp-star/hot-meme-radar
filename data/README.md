# data 目录说明

| 文件 | 作用 | 维护方式 |
|---|---|---|
| `seed_memes.json` | 种子梗表：canonical、别名、类型、出处 | 人工增删；命中即可直通候选闸门 |
| `alias_map.json` | 人工别名映射：`norm -> canonical` | 发现归并错误时手工覆盖 |
| `blocklist.json` | 新闻词、营销词、常青词、敏感词 | 按误报补充 |
| `common_words.txt` | 口语常用词，用于剔除候选 | 按误报补充 |
| `lexicon.txt` | 常用中文词表（word freq），用于剔除普通词与边界错切 | 由 jieba dict 生成，勿手改 |

## lexicon.txt 来源

```text
jieba/dict.txt (MIT License)  https://github.com/fxsjy/jieba
筛选：词频 >= 100，长度 2-6，纯 ASCII 至少 3 字符
```

重新生成：

```bash
curl -sL https://cdn.jsdelivr.net/gh/fxsjy/jieba@master/jieba/dict.txt -o /tmp/jieba.txt
python3 - <<'PY'
rows = []
for line in open('/tmp/jieba.txt', encoding='utf-8'):
    parts = line.split()
    if len(parts) < 2:
        continue
    word, freq = parts[0], parts[1]
    try:
        fq = int(freq)
    except ValueError:
        continue
    if fq < 100 or not (2 <= len(word) <= 6):
        continue
    if word.isascii() and len(word) < 3:
        continue
    rows.append((word, fq))
rows.sort(key=lambda x: -x[1])
with open('data/lexicon.txt', 'w', encoding='utf-8') as out:
    out.write('# 常用中文词表，源自 jieba dict.txt（MIT），筛选词频>=100 且长度 2-6\n')
    for word, fq in rows:
        out.write(f'{word} {fq}\n')
PY
```
