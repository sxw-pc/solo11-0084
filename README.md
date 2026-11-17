# 发布件采运台 / ReleaseHarbor

`releaseharbor` 是一个完全离线、配置驱动、行为确定的**公开数据集发布件采集与完整性闸门**。它面向本地 mock 开放数据门户，完成：

```
有限发现 -> 版本/文件元数据采集 -> Range 断点续传 -> 大小 + SHA-256 验收
        -> SQLite 幂等台账 -> JSON/Markdown/CSV/JSONL 审计输出 -> 只读复核台
        -> 已验收 CSV 的离线跨版整数流转界限（可选）
```

仓库中所有机构、数据集、版本、文件名、许可证文本、SHA-256、URL 与发布内容均为合成 fixture。运行时不访问任何外部网络目标，也不包含登录、上传、删除、远程命令、WARC、RSS、邮件、表单、站点监测或选择器回归等旁支能力。

## 快速开始

要求：Python 3.10+，仅标准库，无需 `pip install`。

```bash
# 1. 一条命令完成完整演示（启动本地 mock 门户、采集、下载、校验、入库、报告、关闭）
python -m releaseharbor demo --config config/demo.json

# 2. 一条命令启动只读复核台（只监听 127.0.0.1）
python -m releaseharbor serve --config config/demo.json

# 3. 一条命令运行测试
python -m unittest discover -s tests -v

# 4. 演示数据上的跨版区域流转界限（需先运行 demo）
python -m releaseharbor transit --config config/demo.json --query examples/transit-v3-v4.json
```

`demo` 默认会清理配置中的 `artifact_dir`、`quarantine_dir`、`report_dir` 与 SQLite 台账，以保证全新输出可复现。使用 `--keep` 保留上次结果并验证幂等跳过；使用 `--strict` 在出现隔离/失败时返回非零退出码。

复核台默认地址为 <http://127.0.0.1:18766/>。它只实现 GET/HEAD，以 SQLite `mode=ro` + `PRAGMA query_only=ON` 打开台账，不提供任何写接口或文件下载接口。

## 演示预期结果

fixture 刻意包含两类“真实流水线会遇到”的情况：

- `civic-budget` 的 `budget_notes.txt` 首次 GET 返回一次临时 503，下载器重试后成功（一次运行内 `attempts=2`）。
- `air-quality` 的 `samples.csv` 声明了错误的 SHA-256，下载完成但验收失败，被移入隔离目录并标记 `checksum_mismatch`。

固定时钟与 fixture 下的基线结果：

```text
status: partial
datasets=3 versions=8 artifacts=12 verified=11 skipped=0 quarantined=1 failed=0 blocked=0
```

`partial` 是**预期**结果：11 个发布件通过大小与摘要验收，1 个发布件被完整性闸门隔离。`nightly` 通道被通道白名单过滤，不计入 12 个发布件。`transit-counts` 额外包含 `v4`、`v4-labels` 与受控的 `flows-v3-v4` 允许流向表，供离线跨版流转查询使用。

## 命令说明

| 命令 | 作用 |
|---|---|
| `python -m releaseharbor demo --config config/demo.json` | 启动内置 mock 门户并执行完整流水线 |
| `python -m releaseharbor demo --keep` | 不清理输出，验证已验证文件的幂等跳过 |
| `python -m releaseharbor demo --strict` | 结果非 `success` 时返回 1 |
| `python -m releaseharbor collect --config ...` | 对已在配置 origin 上运行的门户执行采集 |
| `python -m releaseharbor serve --config ... [--port N]` | 启动只读复核台，端口覆盖仍只绑定 127.0.0.1 |
| `python -m releaseharbor transit --config ... --query query.json` | 离线跨版流转界限查询，打印两份业务合同 |

`collect` 适用于测试或宿主程序自行启动 `PortalServer` 的场景；普通体验使用 `demo` 即可。

## 输出

| 路径 | 内容 |
|---|---|
| `var/releaseharbor.sqlite3` | 数据集、版本、发布件、运行与审计事件台账 |
| `var/artifacts/<dataset>/<version>/<file>` | 通过验收的发布件 |
| `var/quarantine/<dataset>/<version>/<file>` | 完整性失败被隔离的发布件 |
| `reports/report.json` | 全量结构化审计报告 |
| `reports/report.md` | 面向人工复核的摘要报告 |
| `reports/manifest.csv` | 发布件级清单与校验结果 |
| `reports/audit.jsonl` | 按 `seq` 排序的审计事件流 |
| `reports/transit/transit.json` | 独立的流转界限结果（完整可行区间、见证、证据摘要） |
| `reports/transit/transit.md` | 面向复核的流转界限与逐边可行分配 |
| `var/transit-results/transit-<digest>.json` | 按输入摘要归档的流转结果；旧边版本结果可长期回读 |
| `fixtures/golden/*` | 固定演示的 golden 报告与 golden 事件流 |

SQLite 主要表：

- `runs`：每次运行的 ID、状态、配置指纹与摘要。
- `datasets` / `versions`：发现的发布主体与发布通道、版本、来源页。
- `artifacts`：发布件身份、声明大小/SHA-256、远端元数据、本地路径、隔离路径、状态与尝试次数。
- `events`：`(run_id, seq)` 唯一的审计事件序列。

发布件以 `(dataset_id, version_id, filename)` 幂等 upsert。重复运行不会产生重复发布件行；已验证文件会重新计算大小与 SHA-256，删改后会重新下载。

## 状态语义

- `verified`：下载完成，大小和 SHA-256 均匹配。
- `skipped`：上次状态为已验证/已跳过，本地文件复验通过，本次不重复下载。
- `checksum_mismatch` / `size_mismatch`：验收失败，文件进入隔离目录，不写入发布件目录。
- `failed`：远端元数据、传输或协议失败；可恢复的传输失败会按配置重试，`.part` 文件保留以支持下次 Range 续传。
- `pending` / `downloading`：台账中间状态；正常完成后不应残留。

## 发现与下载策略

发现只解析以下受限入口：

1. 首页中 `class="dataset-link"` 的链接。
2. 数据集页中 `class="version-link"` 的链接。
3. 版本页中 `class="artifact-link"` 的链接及其 `data-filename`、`data-size`、`data-sha256`、`data-media-type`。

不跟随其他链接，不递归任意站点。`max_datasets`、`max_versions`、`max_artifacts`、`max_pages` 与通道白名单共同构成发现边界。

下载器每次只发 GET/HEAD；有 `.part` 时发送 `Range: bytes=<offset>-`。只有服务端返回 206 且 `Content-Range` 起始位置匹配时才追加；服务端忽略 Range 返回 200 时从头覆盖。响应大小超过声明值或配置上限立即终止，并把超过声明大小的部分文件丢弃，避免污染后续续传。

## 离线跨版流转界限

`transit` 子命令只消费**已通过验收且磁盘复验一致**的合成 CSV，回答“旧区域到新区域的人流至少/至多多少”，并给出可独立核查的完整整数边分配见证。它只读台账与发布件，不修改任何发布件、运行或事件状态。

查询文件字段：

```json
{
  "dataset_id": "transit-counts",
  "old_version": "v3",
  "new_version": "v4",
  "old_zone": "W1",
  "new_zone": "N4",
  "old_label_version": "v3-labels",
  "new_label_version": "v4-labels",
  "edge_tables": [{"version": "flows-v3-v4", "filename": "flow_edges.csv"}]
}
```

- 客流 CSV 需 `station_code` 与 `weekday_boardings`；标签 CSV 需 `station_code` 与 `zone`；边表需 `from,to,min,max`（`min`/`max` 可省略为 0/无上界）。`SOURCE`、`SINK`（或 `@source`/`@exit`）只在边表显式声明时存在；无法解释的总量差会被拒绝，而不是被静默摊平。
- 求解在**全部站点**上同时满足整数供给、需求、边下界/上界，允许一对多拆分与多对一合并；旧/新站点按版本各自建点，不按 `station_code` 合并。
- 不可行时两份合同都返回 `unknown`，并附可核查的割/供需冲突（`cut_edges`、`new_station_shortfalls`、`old_station_shortfalls`、`unmet_demand`），绝不给出局部区域汇总。
- 重复 `station_code`、缺失/冲突区域标签、未知节点、负容量、边版本早于旧版等情况都带 `role/filename/line` 定位并拒绝完整结果。
- 任意发布件验收失败、磁盘摘要不一致或台账路径越界时，两份合同都只给 `unknown` 及原因；旧区域标签不会被套到新版上。

两份业务使用合同（对同一输入同时给出完整可行区间与两份见证）：

| 合同 | 语义 | A=7/B=3 → C=5/D=5 | 再加 `A->C` 下界 4 后 |
|---|---|---:|---:|
| `guaranteed_relocation` | 所有可行分配中东→西的**最小值**，可证明已发生的规模 | 2 | 2 |
| `maximum_exposure` | 所有可行分配中的**最大值**，用于预留服务容量 | 5 | 3 |

见证包含逐边 `flow` 及 `min/max`、逐站净流入与 `checks`；结果同时记录 `input_digest` 与每份证据的版本、大小、SHA-256。若只更换边表版本，只会产生新的界限结果：新的 `reports/transit/transit.json` 覆盖“最新”视图，而每份结果都会按 `input_digest` 归档到 `var/transit-results/`，旧结果仍可按其摘要回读。

## 安全边界

- 所有出站 URL 在**建立 socket 之前**由 `UrlPolicy` 校验：仅接受配置中的精确 origin，且 host 必须是 `127.0.0.1`、`localhost` 或 `::1`。
- 外部主机、其他端口、HTTPS、userinfo、控制字符与管理边界之外的 URL 一律拒绝；被拒绝目标写入 `target.blocked` 审计事件。
- mock 门户与复核台绑定地址都必须通过显式回环地址校验；不提供公网绑定开关。
- 只实现 GET/HEAD。POST/PUT/DELETE/PATCH/OPTIONS 返回 405。
- 发布件文件名、数据集 ID、版本 ID 都经过白名单清洗，拒绝路径分隔符、`..`、保留设备名与 Windows 尾随点/空格。
- 所有输出路径在配置加载时解析并校验位于项目根目录内；配置、fixture、目标目录之间的覆盖关系被拒绝。
- SQLite 复核台以只读 URI 打开；页面全部服务端转义，不加载外部脚本、样式或字体，并设置 CSP、nosniff 与 `Referrer-Policy: no-referrer`。

## 确定性

- 时钟可注入：`demo.json` 使用固定的 `2026-01-15T12:00:00Z`。
- 事件使用每运行单调递增的 `seq`；报告、清单和事件流均按稳定键排序。
- 全新运行下 `run_id = run-<固定时间>-<配置指纹>`，报告、SQLite 关键字段与 golden 文件可逐字节复现。
- golden 测试在等同配置的新临时根目录中运行完整流水线，并与 `fixtures/golden/report.json` 做结构比较。若固定演示端口已被占用，该测试安全跳过。

## 模块地图

```text
releaseharbor/
  config.py         严格配置/路径/输出布局校验
  security.py       精确回环 origin 与绑定地址策略
  httpclient.py     仅 GET/HEAD 的安全 HTTP 客户端
  fixture.py        合成门户 fixture 与内容生成
  portal_pages.py   确定性 HTML 页面
  portal_server.py  本地 mock 门户、HEAD、Range、临时故障
  htmlparse.py      受限 link/meta 提取
  discovery.py      有界数据集/版本/发布件发现
  downloader.py     断点续传、重试、字节上限与 .part 恢复
  verify.py         大小/SHA-256 验收与隔离移动
  ledger.py         SQLite 幂等写入与事件序列
  collector.py      单发布件元数据 -> 下载 -> 验收流程
  pipeline.py       单次运行编排与 fatal 收尾
  reports.py        JSON/Markdown/CSV/JSONL 输出
  transit.py        查询规格、区域方向与界限入口
  transit_load.py   台账读取、磁盘复验、CSV 装载
  transit_csv.py    客流/标签/边表的定位式严格解析
  transit_model.py  解析与求解共享的数据类型
  transit_flow.py   有界整数网络构建与界限入口
  flow_solver.py    下界变换 + Dinic 可行性
  flow_witness.py   残差负环优化与见证复核
  transit_contracts.py 两份业务合同与结果信封
  transit_report.py 独立流转界限报告
  console.py        只读回环复核台
  console_pages.py  无外部资源、全转义 HTML
  cli.py            demo / collect / serve / transit
  workspace.py      受保护输出目录的安全清理
tests/              62 个 unittest 用例（含流转界限合成测试）
config/demo.json    固定时钟演示配置
fixtures/portal.json 合成门户数据
fixtures/golden/    golden 报告
```

实现与测试合计约 6900 行 Python（以仓库文件 `wc -l` 计），所有文件均不超过 300 行；运行时与测试仅使用 Python 标准库。`report.json` 采用紧凑单行 JSON，便于把行数预算留给实现与测试；人类复核可阅读 `report.md`。

## 合规声明

本项目用于离线数据工程教学、测试与素材演示。所有数据均为合成内容；不面向任何真实站点，不绕过访问控制、验证码、登录或授权限制，也不包含任何真实凭据、个人信息或第三方数据。
