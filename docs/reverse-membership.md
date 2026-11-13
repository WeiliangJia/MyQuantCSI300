# 沪深300成分事件表与反向重建

完整事件表、成分基准和官方附件只保存在本机，并由 `.gitignore` 排除。公开仓库只保留构建程序、字段结构、校验规则和官方来源链接。

## 1. 生成统一事件表

将已人工复核的本地事件种子与已存在的官方调整清单合并：

```powershell
python scripts\build_csi300_event_table.py
```

命令逐事件输出进度，并强制校验：

- 2018-12-17 至 2026-06-15 的 18 个已确认事件日期完整；
- 每个事件的 `ADD` 与 `REMOVE` 数量相同；
- 同一事件内代码、动作唯一，调入与调出集合不重叠；
- 公告日不晚于生效日；
- 能取得本地原始附件时写入 SHA-256，否则保留官方链接并标记 `LINK_ONLY`。

默认输出：

```text
data/reference/official_membership/csi300_events_201812_202606.csv
```

## 2. 从当前300只反向重建历史基准

```powershell
myquant rebuild-membership-backward `
  data\reference\official_membership\csi300_baseline_20260721.csv `
  data\reference\official_membership\csi300_events_201812_202606.csv `
  2018-12-17 `
  --output data\reference\official_membership\csi300_baseline_rebuilt_20181217.csv
```

反向规则按生效日倒序执行：撤销原 `ADD`，恢复原 `REMOVE`。命令会在每个事件后验证成员数仍为300，并立即从目标日期正向回放全部后续事件。只有最终集合与当前官方300只逐代码完全相同，才会写出结果。

目标日期是“当日事件已经生效后的基准日”。因此目标为 `2018-12-17` 时，反向处理的是生效日在该日之后的17个事件；2018-12-17事件保留在目标基准中。

## 3. 导入数据库

闭环通过后，才导入统一事件表并用历史基准生成逐交易日快照：

```powershell
myquant official-import-membership `
  data\reference\official_membership\csi300_events_201812_202606.csv

myquant rebuild-membership `
  data\reference\official_membership\csi300_baseline_rebuilt_20181217.csv `
  2018-12-17 `
  2026-07-21
```

数据库导入和逐日快照重建是后续步骤；反向命令本身只生成本地基准文件，不修改数据库。
