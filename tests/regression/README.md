# 图片回归集（批次 1，回归集 D）

## 这是什么

`image_manifest.tsv` 是从生产审计库**只读**抽样出来的纯图片消息清单（判据：`text_digest` 等于空字符串的 sha256 前缀，
即该消息没有文本），用于验证"判词放开后，图片消息（含擦边表情包、二次元、科普图、二维码等）是否出现误杀"。

清单**不含**用户正文、模型理由与图片内容，只有事件 id / 时间 / 群 id / 历史判定与空的人工标签列。

## 为什么要它

事故案例 event 7722（色情低俗 / review / confidence 0.95）是**纯图片**消息，文本回放（tools/replay_prompt.py）
在构造上覆盖不到它——图片回归集是唯一能覆盖该场景的离线手段。

## 标签规则（重要）

- `history_*` 列是**旧提示词的历史输出**（`label_source=machine_old_prompt`），只作参考、**不是真值**；
- 请人工填写 `human_label`（allow / review / violation）、`human_severity`（1-5）、`reviewer`、`note`；
- 建议双人独立标注，分歧条目由第三人仲裁，并记录一致性（kappa 低于 0.6 的类别先补规范再跑）；
- 判定口径按实现文档 §5.2 的 B 轨道阈值：色情低俗 severity>=3 即 violation（涉未成年一律 5）；
  辱骂需针对特定成员；违法违规按具体行为；政治敏感保守处理。

## 怎么用

```
# 1) 重新抽样（只读生产库；需要可访问生产主机的环境）
python tools/gen_image_manifest.py --db <moderation.db> --out tests/regression/image_manifest.tsv \
    --limit 200 --allow 120 --review-other 40

# 2) 回放：把清单里的 sample_id 对应的图片（需先从生产附件目录取，注意只读且不要提交到仓库）
#    喂给新旧两版提示词，比较 verdict 变化：
#    · 负例（人工标 allow）被判 violation 的比例不得高于基线（防误杀）；
#    · 正例（人工标 violation）的召回应当上升（漏杀改善）。
```

## 抽样构成（生成时快照）

```
样本数: 182
历史 verdict 分布: {'violation': 16, 'review': 46, 'allow': 120}
历史 category 分布: {'广告引流': 15, '政治敏感': 2, '色情低俗': 4, '违法违规': 1, '无': 160}
待人工标注条数: 182
```
