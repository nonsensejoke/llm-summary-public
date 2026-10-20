# 摘要 SQLite 数据库分卷

Git 保存 `article_summaries.db`、`article_summaries_kr.db` 及可选的
`article_summaries_en.db` 的二进制分卷，
每卷最多 **48 MiB（50,331,648 字节）**。分卷本身不是 SQLite，使用前须还原。
工具仅使用 Python 标准库，不依赖摘要程序或模型 API 配置。

```text
data/
  article_summaries.db.parts/manifest.json, part-00000, ...
  article_summaries_kr.db.parts/manifest.json, part-00000, ...
  article_summaries_en.db.parts/manifest.json, part-00000, ...  # 首次英文运行后生成
  UPDATED_AT.txt
scripts/db_volumes.py
```

还原后使用对应的 `data/*.db` 工作库。
所有表、索引、记录 ID、摘要版本、请求日志及 token 统计均保留。
完整数据库、WAL、SHM、journal 和本地备份不提交 Git。分卷不使用压缩或 Git LFS，
也不改变现有 Git 历史及其体积。

## 使用

在仓库根目录执行（公开仓库也使用相同命令）：

```bash
# 新 checkout：还原中韩两库，以及存在分卷的英文库。
python3 scripts/db_volumes.py restore

# 可重复 --database，只操作指定库。
python3 scripts/db_volumes.py restore --database article_summaries.db

# 校验分卷、完整快照 SHA-256 和 SQLite quick_check；不覆盖工作库。
python3 scripts/db_volumes.py verify

# 本地摘要或翻译结束后，显式更新需要提交的分卷。
python3 scripts/db_volumes.py pack --database article_summaries.db
python3 scripts/db_volumes.py pack --database article_summaries_kr.db
python3 scripts/db_volumes.py pack --database article_summaries_en.db
```

现有中韩 CLI 和 shell 入口会自动还原缺失的工作库，保留已有本地数据库。
默认工作库及其分卷都缺失时会报错；显式指定全新自定义输出路径仍允许创建空库。
韩语翻译先读取还原后的目标库以跳过已翻译版本；`--dry-run` 在临时目录检查缺失的
目标库，不创建目标工作库、不调用模型 API。直接使用 Python 的存储类时由调用者先还原。

英文入口 `summary_ingest ingest-en` 独立读取原始论文并通过 Codex CLI 总结，
不依赖中韩摘要库。默认 `data/article_summaries_en.db` 首次运行可创建；有分卷时先还原。
英文 `--dry-run` 始终使用临时快照，不调用 Codex、不创建或修改目标工作库。
英文库未建立时，无参数的分卷操作不会要求它存在；仅有英文分卷的仓库可以独立还原。
显式 `--database article_summaries_en.db` 不会跳过缺失或损坏的英文分卷。

打包通过 SQLite backup API 生成一致性快照，包含 WAL 中已提交的记录。
本地抓取、打包和 Git 更新请串行运行。不要直接删除 WAL、SHM。

还原默认拒绝覆盖内容不同的工作库。拉取新分卷后，如确定要替换本地数据：

1. 关闭使用该数据库的所有进程和连接。
2. 先打包或备份需要保留的本地修改，让 SQLite 正常完成 checkpoint 和关闭。
3. 确认 sidecar 已由 SQLite 正常清理后，执行 `restore --force`。

`--force` 也不会绕过 WAL、SHM、journal 检查。工具无法检测所有空闲连接，
关闭连接是调用者的责任。已有完整文件与快照相同时还原可重复执行。

也可以还原到独立目录，不影响当前工作库：

```bash
python3 scripts/db_volumes.py restore --data-dir ./data --output-dir /tmp/summary-restored
```

每库先写临时文件，完整校验通过后才替换目标。缺卷、截断、SHA-256 不符、
非法清单、未知格式、目录多余文件、符号链接或 SQLite 检查失败都会阻止还原。
两库独立处理，不提供跨库事务；需要整组切换时先还原到全新的暂存目录，全部成功后发布。

## 上游文献输入

上游文献仓库同时支持完整 `.db` 和分卷格式。缺库时，CLI 使用上游附带的
`scripts/db_volumes.py` 还原；存在分卷但工具缺失、分卷损坏时会报错。
两个 shell 入口在 clone/pull 后刷新上游只读输入，已有摘要工作库保留。
运行前应关闭上游库的所有连接；不会删除遗留 sidecar 来强制覆盖。

仅刷新文献输入（本私有仓库中执行）：

```bash
python3 -m summary_ingest.database_files --literature-repo ./literature-data-public --refresh
```

直接运行 CLI 时已有输入库不会自动刷新；手动 pull 上游后应执行上述命令。

## 清单 v1

每个 `manifest.json` 包含：

| 字段 | 含义 |
| --- | --- |
| `format_version` | 整数 `1` |
| `database` | 对应的三个摘要数据库文件名之一 |
| `size_bytes`、`sha256` | 完整数据库快照大小与 SHA-256 |
| `chunk_size_bytes` | 默认 `50331648`，不超过 48 MiB |
| `parts` | 按顺序排列的 `{name, size_bytes, sha256}` |

卷名从 `part-00000` 连续编号，最后一卷可以较小，整卷边界不额外生成空卷。
清单不含生成时间，相同快照重复打包不产生变化；成功打包会移除不再使用的旧卷。

## Actions 与首次切换

现有中韩两个 workflow 共享并发组，检出最新 `main`，先还原两库和文献输入，运行任务后
仅打包对应语言的库。通过校验后提交私有分卷，再同步公开仓库。
模型任务部分失败时先保存成功记录再报告失败；还原、打包、校验或私有推送失败时停止公开发布。

发布工具只同步清单列出的分卷、`UPDATED_AT.txt`、还原工具、本文档及忽略规则，
不会复制本地压缩包和备份。首次切换无论哪个 workflow 先运行，都迁移公开仓库两库，
停止跟踪完整 `.db`；后续只更新当前语言的分卷。已有公开 README 保留。
`UPDATED_AT.txt` 原有采集指标保留，`Database volumes:` 段记录还原后的大小及卷数；
韩语流程只更新该段，不重写中文采集时间或指标。

新增英文 workflow 仅支持手动触发，并使用同一发布并发组。它只准备论文输入和英文目标库，
只打包、暂存及同步英文分卷；不要求中韩库存在，也不触发中韩整组迁移。
英文发布保留中韩已跟踪的完整库及分卷，后续中韩流程仍按原规则完成两库迁移。
元数据仅更新当前选择的库，保留其他库已有的元数据和采集指标。

私有仓库手动暂存与 workflow 使用同一工具：

```bash
python3 -m scripts.publish_db_volumes stage --database article_summaries.db
```

首次暂存前必须先打包两库，工具验证两库后才停止跟踪旧数据库。
此命令暂存分卷和元数据；代码、文档及忽略规则须随首次迁移一并提交。

## 下游网站适配

本次仅修改摘要仓库及公开发布流程，不修改 `paper_radar_web`。
公开仓库切换后，旧的“直接从 Git 提取完整摘要 `.db`”方式不能更新新数据。
网站现有更新脚本目前仅对文献库支持分卷，需要让两个摘要库也走同样的分卷提取与校验流程。

一次发布必须固定一个完整 commit SHA，从该 SHA 读取清单和全部分卷，
不能分别从不断变化的 `main` 下载。建议在独立的固定版本 checkout 中运行：

```bash
python3 /path/to/pinned-checkout/scripts/db_volumes.py restore \
  --data-dir /path/to/pinned-checkout/data --output-dir /path/to/new-release-staging
```

保留网站现有 schema、行数及摘要关联校验，全部通过后再切换 current release。
还原后的文件名及网站查询接口保持原样。历史 commit 仍可按原来的完整 `.db` 格式读取。
