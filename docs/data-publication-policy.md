# 数据采集与公开发布边界

本项目是独立、非官方、非商业的个人量化研究项目，与中证指数有限公司、上海证券交易所、深圳证券交易所不存在隶属、合作、授权或背书关系。“沪深300”和“CSI 300”仅用于描述研究基准。本项目不使用中证 Logo，也不以页面设计、文字或文件命名暗示获得中证授权。

## 自动采集状态

中证公告详情与附件的自动采集入口目前在代码层硬暂停。暂停期间：

- 不自动请求公告详情接口或批量下载附件；
- 不使用代理轮换、验证码规避、身份伪装或其他手段绕过访问限制；
- 只有在限制解除、重新核对适用条件并完成代码评审后，才可修改硬暂停常量；
- 已取得的本地材料只用于核验和个人研究，不因接口恢复而自动公开。

## 仅保存在本地的内容

以下内容不得提交到 Git 或发布到 GitHub：

- 公告正文、PDF、Excel、Word、ZIP 等原始文件和附件；
- 完整的沪深300成分名单、调入/调出名单及从正文提取的完整证券代码列表；
- 含完整名单的中间文件、验证导出、抓取日志和临时网页资源；
- 含本机路径、请求错误正文或其他运行环境信息的内部采集目录。

仓库通过 `.gitignore` 保护 `data/official/`、`data/reference/official_membership/` 中的实际数据文件、`data/logs/`、`.tmp/` 和 `tmp/`。这些文件不会由 Git/GitHub 备份，需要由使用者自行进行合规的本地备份和访问控制。

## 允许公开的内容

公开仓库只允许包含：

- 采集、校验和导入程序本身；
- 不含真实完整名单的字段结构、空白模板和测试样例；
- 用于完整性校验的 SHA-256 哈希；
- 指向中证官网的公告详情或附件 URL。

公开来源目录的建议字段结构见 [`official-source-metadata-schema.csv`](official-source-metadata-schema.csv)。该文件只有表头，不包含公告正文、附件内容或证券名单。内部目录中的路径、错误正文和提取结果不得复制到公开目录。

## 发布前检查

发布前至少执行：

```powershell
git status --short
git check-ignore -v data/official/example.pdf
git check-ignore -v data/reference/official_membership/csi300_baseline_YYYYMMDD.csv
git ls-files data/official data/reference/official_membership .tmp tmp
rg -i "csindex.*logo|中证.*logo" . --glob "!data/**" --glob "!.tmp/**" --glob "!tmp/**"
```

`git ls-files` 对私有目录应无输出；若出现实际数据文件，应停止发布并先从 Git 索引中移除。不得以删除本地原始文件来代替清理 Git 历史。
