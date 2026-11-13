# MyQuant CSI 300

> 独立、非官方、非商业的个人研究项目。本项目与中证指数有限公司、上海证券交易所、深圳证券交易所不存在隶属、合作、授权或背书关系，也不使用中证 Logo。“沪深300”和“CSI 300”仅用于描述研究基准。

面向沪深300成分股的日频量化研究原型。目前处于数据准备和验收阶段，已实现历史成分重建、MQ300 等权基准回放及模拟复权因子生成。策略模块尚未形成经过验收的端到端回测；项目不含实盘交易或券商接入。

2024 年真实数据仍有行情和停牌证据缺口，BaoStock 参考昨收的复权口径尚未独立核验，因此没有发布年度净值。年度脚本目前只比较同一起点回放的分段输出，下半年取自再次完整回放，尚未验证独立续跑。

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
分段输出仍从同一起点回放，下半年不重新定基。

仅使用本地 BaoStock 归档时，运行 `python scripts/simulate_mq300_factors_2024.py`。
脚本用 2023 年 12 月预热，按“上一有效收盘价 ÷ 当日参考昨收”累计模拟因子。
因子、异常和汇总写入 `data/exports/mq300-2024-simulated/`。缺价、零成交和冲突不推断为停牌，
后续因子链也不自动续接。该脚本不输出净值：模拟因子须先通过独立参考价核验，
并解决缺价及停牌证据缺口，才能进入年度净值验收。

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
