# MyQuant CSI 300

> 独立、非官方、非商业的个人研究项目。本项目与中证指数有限公司、上海证券交易所、深圳证券交易所不存在隶属、合作、授权或背书关系，也不使用中证 Logo。“沪深300”和“CSI 300”仅用于描述研究基准。

面向沪深300成分股的日频量化研究原型。已实现历史成分重建、模拟复权因子生成，以及基于自建等权基准 `MQ300.EW` 的 2024 年 V0 历史策略回测。正式沪深300指数增强和前向模拟盘仍在建设阶段。

2024 年本地行情补数已完成。年度脚本已从 6 月 28 日收盘状态独立续跑下半年，并将净值、持仓权重和调仓审计与全年回放逐日对账。腾讯行情和新浪复权因子已与 BaoStock 逐日核对，差异明细和公司行动证据保存在本地。年度净值尚未发布。

Git 只跟踪代码、测试、文档、配置示例和空白模板。原始行情、补数清单、完整成分、审计产物和本机 `.env` 留在本地；克隆仓库后需自行准备数据才能重跑年度验收。

## 快速开始

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
myquant init-db
pytest
```

将 Tushare Pro token 填入 `.env` 后，可按交易日增量同步：

```powershell
myquant sync 20260717
myquant quality-check 20260717
myquant sync-reference 20050408 20260717
myquant sync-financials 20050408 20260717
# 建议分年或分季度执行，遵循Tushare账户积分和频率限制
myquant backfill-daily 20200101 20201231
```

## 免费数据适配层

免费源不替代点时审计。安装可选依赖后，BaoStock可作为日线/季度财务主源，AKShare用于日线与资金流补充；所有响应仍会进入不可变原始归档。

```powershell
pip install -e ".[free,dev]"
myquant free-sync-daily baostock 600000.SH 2020-01-01 2020-12-31
myquant free-sync-daily akshare 000001.SZ 2020-01-01 2020-12-31
myquant free-sync-financials 600000.SH 2024 4
```

沪深300历史成分采用“官方来源链接 + 经人工复核的本地清单”导入。不要自动解析公告 PDF；创建含 `index_code,ts_code,action,effective_date,announcement_date,source_url` 的本地 CSV，再执行：

```powershell
myquant official-archive "https://example.com/official.pdf" .\data\official\source\csi300-2026-06.pdf
myquant official-import-membership .\data\official\csi300-2026-06.csv
```

中证公告详情和附件的自动采集入口当前已在代码层硬暂停；远端限制解除并重新核对适用条件前，不得通过代理轮换、验证码规避或其他方式绕过限制。显式 URL 的单份人工归档仅用于本地核验。

原始公告、附件、完整成分/调样名单、抓取日志和临时网页文件均由 `.gitignore` 排除，只保留在本机。公开仓库仅允许发布采集程序、字段结构、内容哈希和中证官方来源链接，不发布原始文件或完整名单。详细边界见 [`docs/data-publication-policy.md`](docs/data-publication-policy.md)。

免费公告来源可以辅助严格重建成分变更，但一般不提供完整历史日权重；缺少权重时，系统应将该期标记为不可用于基准权重优化，而不是伪造权重。

## 清洗与研究数据层

原始供应商记录不会被覆盖。先将旧数据迁入按供应商保留的源数据层，再生成唯一的清洗日线；默认优先级为 BaoStock、AKShare、Tushare。不同来源的收盘价超过容差会写入 `ops.data_quality_issue`。

```powershell
myquant init-db
myquant migrate-source-bars
myquant clean-data 2020-01-01 2020-12-31
myquant quality-report
myquant build-financial-snapshots 2020-01-01 2020-12-31
```

### 自由流通市值模型权重（研究代理）

当 `daily_basic` 已包含每个成分股每日的 `circ_mv`（流通市值）时，可生成按流通市值比例归一的研究权重：

```powershell
myquant build-free-float-weights 2020-01-01 2020-12-31
myquant data-audit
```

该权重的来源固定标记为 `free_float_market_cap`、方法为 `circ_mv_pro_rata_v1`；只有某日全部 300 个成分股具有当日或更早可得的正 `circ_mv` 时才会写入。它是透明的研究代理，**不等同于中证发布或许可的自由流通调整权重**，因此不能通过 `myquant certified-range` 的官方权重认证。

免费数据的优先顺序为：AKShare `stock_zh_a_daily` 的 `outstanding_share × close`，再回退到 BaoStock 的 `close × volume × 100 / turn`。后者只在 `turn > 0` 时有效，因此不会填补停牌或零成交量日。可以先用 20 只样本验证，再分片回填：

```powershell
myquant backfill-csi300-circ-mv 2005-07-01 2026-07-17 --limit 20
myquant backfill-csi300-circ-mv 2005-07-01 2026-07-17 --offset 0 --limit 100
```

回填行情时，`backfill-csi300-baostock` 以已认证的日度成分快照（而非不完整的第三方区间表）确定完整历史股票池；默认不排除任何基准成分。仅在构建实际可交易研究池时，才应显式传入 `--exclusion-file`。

### MQ300 月频等权模拟市场

不依赖官方历史权重的研究可使用独立代码 `MQ300.EW`。它采用已认证的沪深300历史成分，
月末收盘生成等权目标，下一交易日开盘生效；成分变化也在此前已知公告的前提下于生效日开盘调仓。
随后按复权收益逐日漂移。涉及停牌股票时整次调仓顺延，保留原持仓（含停牌的调出股票），
复牌后重试；停牌期间沿用前值。无法解释的缺价会阻止整个请求区间写库。

```powershell
myquant build-equal-weight-benchmark 2022-01-01 2024-12-31
```

导出净值时加 `--nav-output data/exports/mq300-nav.json`（请先创建输出目录）。
净值以历史首日开盘为 1：旧持仓先承担隔夜损益，开盘无成本调仓，再计算日内损益。
每天可用 `previous_nav + overnight_pnl + intraday_pnl = nav` 对账。
导出要求行情复权价格与归档因子一致；缺少因子或历史回放中断会拒绝导出。
归档因子的一致性检查不能代替公司行动和交易日历的历史验收。

2024 年独立验收：在本机 `.env` 配置具备交易日历、日线、复权因子和停复牌权限的
`TUSHARE_TOKEN`，从项目根目录运行 `python scripts/verify_mq300_2024.py`。
脚本使用同源行情与复权因子，归档接口响应；仅全日停牌可作为缺价时延续估值的依据。
验收通过才写入 `data/exports/mq300-2024-nav.json`，以 2024 年首个交易日开盘为 1。
上半年结束后保存持仓权重、收盘估值、现金、净值和待执行调仓状态。下半年仅使用该状态与下半年输入续跑，并与全年回放逐日核对。

仅使用本地 BaoStock 归档时，运行 `python scripts/simulate_mq300_factors_2024.py`。
脚本用 2023 年 12 月预热，按“上一有效收盘价 ÷ 当日参考昨收”累计模拟因子。
因子、异常和汇总写入 `data/exports/mq300-2024-simulated/`。缺价、零成交和冲突不推断为停牌，
后续因子链也不自动续接。完成本地公司行动复核和停牌派息预处理后，运行
`python scripts/verify_mq300_2024.py --local`，在模拟因子目录写入
`replay_comparison_2024.csv` 供研究对账。交易日按沪深交易所休市公告独立核对。

运行 `python scripts/reconcile_mq300_feeds_2024.py`，可按证券和交易日核对腾讯未复权行情、
新浪后复权因子及 BaoStock 台账。原始响应缓存在本地模拟因子目录，使用 `--offline` 可重跑。
复权以相邻有效交易日的步长和反推参考价核对，并保留两家因子的数值差异。

2024 年 12 月低波动、中期动量和短期反转使用同一套周频撮合规则和 MQ300.EW 基准。
已有本地验收输入时，运行下列命令。第一条生成基准净值，第二条使用腾讯缓存核对 BaoStock
补取的调出股行情。对照结果写入 `data/exports/mq300-2024-factor-comparison/`。

```powershell
python scripts/verify_mq300_2024.py --annual-nav
python scripts/backtest_mq300_pilot_2024.py --backfill-exits
python scripts/backtest_mq300_pilot_2024.py --compare
```

首次使用先运行 `myquant init-db` 创建新增的 `market.benchmark_rebalance` 审计表。
该表保存信号日、执行日、开盘目标权重、是否执行及顺延原因。历史首日为初始化，信号日为空。
目标不使用执行日收盘价；收盘价仅用于计算当日日终权重。为保持分段运行一致，构建会从已有成分历史起点回放。

日终权重写入 `market.index_constituent`，输出代码限定为 `MQ300.EW`、
`source=myquant_equal_weight`、`methodology=monthly_equal_weight_drift_v1`。
它是自建模拟市场，不是中证官方指数，也不能通过官方权重认证。

每日沪深300成分池需要一份“开始日前最后一个有效日”的复核基准名单（只含 `ts_code` 列）和已导入的官方调样事件：

```powershell
myquant rebuild-membership .\data\official\csi300-baseline.csv 2020-01-01 2020-12-31
```

## V0 历史回测基线

在项目根目录使用本地行情、成分、公司行动归档及腾讯和新浪缓存复跑：

```powershell
python scripts/verify_mq300_v0_2024.py
```

命令离线重建年度基准和三轮回测报告，核对 **242 个基准交易日、199 个策略交易日、43 次周频调仓**，以及五组策略的扣费后收益和成交成本。策略区间为 2024-03-11 至 2024-12-31，同期 `MQ300.EW` 收益为 +12.48%。报告及逐日净值、订单明细写入 `data/exports/`。

| 策略 | 扣费后收益 | 较 `MQ300.EW` 超额 | 成交成本 |
| --- | ---: | ---: | ---: |
| 低波动 | +21.53% | +9.05 个百分点 | 33,916.33 元 |
| 中期动量 | −0.82% | −13.30 个百分点 | 26,554.51 元 |
| 短期反转 | +14.02% | +1.53 个百分点 | 79,830.43 元 |
| 短期反转＋前 30 持仓缓冲 | +14.66% | +2.18 个百分点 | 76,579.42 元 |
| 50% 动量＋50% 低波动 | +12.06% | −0.42 个百分点 | 35,555.21 元 |

V0 使用 100 万元初始资金、20 只股票等权、98% 目标仓位和次日开盘模拟撮合。成交成本沿用历史回测口径，包含模拟佣金、税费、过户费和滑点。

## V1 逐日研究账户

V1 使用 1,000 万元虚拟资金。每周最后一个交易日收盘后取低波动、中期动量和短期反转的截面排名均值，按 80% 基准与 20% 前 50 等权分配股票仓位，目标仓位 98%。目标通过跟踪误差、行业、单股和换手检查后在下一个交易日开盘模拟成交。调出后的存量持仓继续估值并尝试退出。

```powershell
python scripts/backfill_mq300_paper_inputs.py --industry-2024
python scripts/backfill_mq300_paper_inputs.py --payments-2024
python scripts/replay_mq300_v1_2024.py
```

2024 回放使用已经验收的 MQ300.EW、BaoStock 归档及独立行情缓存。新增的 BaoStock 行业快照按查询日期向后生效。账户写入 `data/exports/mq300-v1-2024-paper/` 中的 SQLite 日账和 CSV，保留现金、持仓、信号、订单、费用及公司行动。`--through YYYY-MM-DD` 可在中间日期停止，同一输出目录再次运行会从已提交的会计日继续。改变成本或输入时用 `--output` 指定新的目录。

订单逐笔分解券商佣金、经手费、证管费、过户费、卖出印花税、半价差及冲击，其中经手费和证管费已包含在假定的万三佣金（最低 5 元）内。半价差与冲击的场景参数各为 2 和 3 个基点，以分为单位调整模拟成交价格。分红先确认应收，再按已匹配的派息日入账，缺同额证据的分红逐笔标记为除息日结算假设。送转按除权日入账。`--monthly-cost` 和 `--dividend-tax-rate` 的默认情景值均为零，后者是固定有效税率假设。

最近 120 个已收盘交易日可独立归档。结束日期按实际最后一个已收盘交易日填写：

```powershell
python scripts/backfill_mq300_paper_inputs.py --end 2026-11-17
python scripts/backfill_mq300_paper_inputs.py --recent-payments 2026-11-17
python scripts/backfill_mq300_paper_inputs.py --end 2026-11-17 --offline
```

最近窗口保存逐日供应商成分、报价、停牌状态、行业、派息送转日历、复权及涨跌停候选值和缺口清单。`coverage.json` 报告官方成分快照核对和前向放行所需的证据，`action_terms_pending.csv` 保存待复核的参考价变动。合并换股等事件也要单独验收。下载与回放产物均在本地忽略目录。

## 关键约束

- 历史仓库从2005-04-08开始；2008年开始统计绩效。
- 成分池、财务数据和公司行动均按数据可得时点使用，避免前视偏差。
- 默认持有50只股票、周频换手、年度化跟踪误差目标6%–8%。
- 回测在信号后的下一交易日开盘成交，并计入T+1、交易成本、停牌、涨跌停和参与率限制。

运行说明与数据契约见 `docs/architecture.md`；数据发布边界见 `docs/data-publication-policy.md`。

## 沪深300历史成分认证（2026-07-22）

- 已核验 2005-07-01 至 2026-06-15 的 68 个官方调整事件，共 67 个生效日、1,850 行 ADD/REMOVE 动作。
- 从 2026-07-21 官方 300 只基线反向重建至 2005-07-01，并与独立归档的 2005-07-01 官方完整 300 只基线逐代码完全一致；再正向回放至当前基线也完全闭合。
- PostgreSQL 已生成 2005-07-01 至 2026-07-17 的 5,114 个交易日日度成分快照，共 1,534,200 行；每个交易日恰好 300 只。
- 该认证仅针对历史成分身份。日度基准权重仍需单独补齐和认证，未认证权重前不得宣称指数增强回测已完成基准权重闭环。
- 2005-04-08 至 2005-06-30 虽有行情数据，但尚无独立完整成分端点，因此保持未认证。

重建与双端点验收命令：

```powershell
python scripts/build_csi300_event_table.py
python -m myquant.cli rebuild-membership-backward `
  data/reference/official_membership/csi300_baseline_20260721.csv `
  data/reference/official_membership/csi300_events_200507_202606.csv `
  2005-07-01 `
  --expected-baseline data/reference/official_membership/csi300_baseline_official_20050701.csv
python -m myquant.cli official-import-membership `
  data/reference/official_membership/csi300_events_200507_202606.csv
python -m myquant.cli rebuild-membership `
  data/reference/official_membership/csi300_baseline_official_20050701.csv `
  2005-07-01 2026-07-17
```
