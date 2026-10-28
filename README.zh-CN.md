# catalog-sync

[English](README.md) | 简体中文

**把外部平台的数据，接入你自己的业务系统。**

在取得授权的前提下，采集外部数据、清洗并统一字段，再导入自己的数据库或业务平台。
来源更新后，按同一套规则再次运行。

面向需要把 SaaS 或企业数据服务接入自有平台的技术团队。第一版从商品／物料和供应商记录开始：
启动 Docker 后，一条命令完成合成数据的采集、清洗和导入，并打开结果报告；体验后再适配自己的来源和目标。

**[运行示例](#运行合成数据示例)** · [看一条数据如何落地](#一条数据从来源到目标) ·
[接入自己的服务](#接入自己的服务) · [在 Docker 中运行](docs/deploy.md)

> **v0.1.0rc1 · 候选版本。** 包含框架、合成数据源、离线回放和参考 HTTP 接收端。
> 接入真实服务需要安装或编写 Python 连接器。已验证范围与限制见[验收说明](docs/acceptance.md)。

## 为什么需要它

能访问外部服务，只是开始。自己的业务系统还需要统一格式的记录、可以重复执行的导入，以及明确的数据更新规则。

| 团队遇到的问题 | catalog-sync 提供的机制 | 对接入工作的价值 |
|---|---|---|
| 来源字段与自己的平台不一致 | 显式字段映射、单位解析、按来源划分的编码表 | 把整理规则写成可以重复执行的配置。 |
| 采集中途失败 | 保存进度、支持续传 | 从已完成的工作继续。 |
| 采完才发现清洗规则要改 | 保留原始响应、支持离线重新清洗 | 不必为了改映射再采集一遍。 |
| 再次导入时可能重复或覆盖平台内容 | 记录标识、更新策略和字段归属 | 重复导入时保留平台自己维护的字段。 |
| 一批数据没有完整处理 | 条目失败、解析问题和审计记录 | 找到下次运行前需要处理的对象。 |

完整链路：**获授权的服务 → 连接器 → 原始缓存 → 清洗、字段统一与空值处理 → 标准记录 → 自有数据库或接收端**。

```
Authorized SaaS / enterprise service
               ↓
        Source connector
               ↓
           Raw cache  ← replay when mappings change
               ↓
     Clean · map · handle empty values
               ↓
       Normalized records
               ↓
   Your database / application receiver
```

你配置来源、字段规则和接收目标，框架负责串起整个过程。[数据契约](docs/data-contract.md)明确了空值、失败和更新的含义。

## 一条数据，从来源到目标

下面使用仓库已有的[契约示例](examples/contract/materials/cases/mat-0002.json)，全部是**合成数据**。
同一个来源标识贯穿采集、清洗和导入。

| 字段 | 来源响应 | 标准记录 | 导入结果 |
|---|---|---|---|
| 标识 | `id: "mat-0002"` | `source_id: "mat-0002"` | SQLite 和参考接收端保留相同来源标识 |
| 克重 | `weight: "250 gsm"` | `weight_gsm: 250.0` | 以克／平方米表示的数值 |
| 幅宽 | 来源没有提供 | `width_cm: null` | 明确保留为空 |
| 价格 | `price: "38.87"`, `currency: "USD"` | `price: {amount: "38.87", currency: "USD"}` | 金额与币种一起保留 |

**缺失值不会被猜出来。** 映射可以依次尝试多个来源字段；没有可用值时，按字段类型保留为空。
无法解析且没有可用后备值时，在 `issues` 中留下问题记录。目标字段配置 `keep_existing` 后，
可以在新值为空时保留原有值。

<a id="60-秒体验"></a>

## 运行合成数据示例


### Docker 一条命令体验

安装并启动 Docker，下载或克隆本仓库后，在仓库根目录执行下面的命令。
支持 macOS、Linux 的 POSIX shell；Windows 使用已接通 Docker 的 WSL。
电脑上不需要安装 Python，也不需要真实服务账号或手动填写演示配置。

```console
$ sh tools/demo.sh
```

首次运行会构建镜像并下载依赖。随后在禁止访问外网的隔离容器内启动模拟来源和参考接收端，
完成采集、清洗、导入 SQLite 与接收端，再重复导入验证结果。

**你会看到：** 一份本地 HTML 报告，展示来源原值、清洗后的字段、实际导入记录，以及验证数量：
**24 条物料、9 条供应商、接收端 24 条对应物料**。程序会对照标准记录核验两个物料目标端，
重复导入后内容也必须保持不变，否则命令报错退出。支持桌面打开命令时会自动打开报告，终端始终打印报告路径。

**清理：** 无论成功还是失败，启动脚本退出时都会清理本次临时容器和数据卷。
仅保留报告目录与缓存镜像 `catalog-sync:demo`。看完后可以删除终端显示的报告目录；
运行 `docker image rm catalog-sync:demo` 可删除缓存镜像。原始缓存和模拟登录状态不会导出到电脑上。

如需指定一个空的报告目录，设置 `DEMO_OUTPUT_DIR`；设置 `DEMO_OPEN_REPORT=0` 可关闭自动打开。
已有内容的输出目录会被拒绝，不会覆盖。另有可选的 [Compose 部署示例](docs/deploy.md)，
用于独立 Postgres 服务及定时运行。

<details>
<summary>其他方式：逐步运行命令行示例</summary>

### 逐步运行 CLI

**准备条件：** Python 3.11–3.13、`uv` 和本仓库的一份全新检出。在仓库根目录打开终端，
确保端口 `8765`、`8780` 空闲。下方可选的查询命令需要 `sqlite3` 命令行工具。

示例启动本地模拟 SaaS 和参考 HTTP 接收端，采集 24 条物料、9 条供应商记录，清洗字段，
再将物料同时导入 SQLite 和参考接收端。无需真实服务账号，示例配置中已填好演示用声明。

下面会把 `catalog-sync.example.toml` 复制为 `catalog-sync.toml`，请使用还没有自定义配置的全新检出。
需要隔离演示数据时，先将 `CATALOG_SYNC_HOME` 设置为云同步目录之外的一个新本地目录。
命令最后会停止两个演示服务。`$` 是终端提示符，复制命令时不包含它。

<!-- ci:run -->
```console
$ uv sync
$ cp catalog-sync.example.toml catalog-sync.toml
$ uv run catalog-sync mock serve --detach --pid-file mock.pid
$ uv run catalog-sync mock platform serve --detach --pid-file platform.pid --db out/platform.db
$ uv run catalog-sync login mock --form
$ uv run catalog-sync pull mock-materials
$ uv run catalog-sync normalize mock-materials --out out/materials.jsonl
$ uv run catalog-sync load --sink mock-materials-db --input out/materials.jsonl
$ PLATFORM_TOKEN=demo-platform-token uv run catalog-sync load --sink mock-platform --input out/materials.jsonl
$ uv run catalog-sync run mock-suppliers
$ uv run catalog-sync audit show
$ uv run catalog-sync mock stop --pid-file platform.pid
$ uv run catalog-sync mock stop --pid-file mock.pid
```

<details>
<summary>展开每条命令的作用</summary>

各步骤做了什么：

1. `mock serve --detach` 在后台启动这个服务，监听就绪后才返回；`mock stop` 结束它。它的数据由种子生成，全是假的：
   文件夹树里的 24 个面料（materials）和 9 个供应商（suppliers）。
2. `login mock --form` 把演示账号提交到服务的登录接口，并把它返回的 bearer 令牌存进工作区，只有你自己可读。
   全程不需要浏览器，所以 CI 也能跑。
3. `pull` 遍历文件夹树，取回每个面料的详情记录和它的两张图片，并保留原始响应。`normalize` 离线地、用连接器装在自己包里的映射把它们变成内置 `material`
   schema 的记录：不需要 `--mapping`，也不需要源码树，所以这个代码块从安装好的 wheel 里同样能跑。`load` 把记录写进 SQLite（`{workspace}/out/mock.db`：以 `{workspace}/` 开头的目标端路径会按工作区解析，见下文）。
4. `mock platform serve --detach` 启动了参考平台，它把数据存进 `out/platform.db`（普通路径，相对于你运行命令的目录）。`load --sink mock-platform` 通过 `http` 目标端
   把同样的记录以 JSON 批次送给它（见 [HTTP 目标端](#你的平台http-目标端)）。令牌从目标端所指的环境变量读取；
   接收端没有被给定令牌时，接受的就是演示令牌。它的表以实体命名（`material`），不同于 SQLite 目标端的默认表名（`materials`）。
5. `run mock-suppliers` 用一条命令完成第二种实体的同样三步；它的 schema 不同（`supplier`），分页方式不同（游标），也没有图片。
6. `audit show` 列出做过的事：只有计数和 id，没有内容，也没有令牌。

</details>

**查看导入结果。** 演示服务停止后，仍可查询数据库：

```console
$ WORKSPACE="${CATALOG_SYNC_HOME:-$HOME/.local/share/catalog-sync}"
$ sqlite3 -header "$WORKSPACE/out/mock.db" "select source_id, code, name, weight_gsm from materials limit 3"
$ sqlite3 -header "$WORKSPACE/out/mock.db" "select source_id, name, country, rating from suppliers limit 3"
$ sqlite3 -header out/platform.db "select source_id, code, name, weight_gsm from material limit 3"
```


**第一次试用的完成标志：** 本地数据库里有 24 条物料、9 条供应商记录；参考接收端里有相同的 24 条物料。
选一个 `source_id`，对照两边的映射字段。这是默认合成数据集的预期数量，不是性能测试结果。

想验证重复导入，可以再次执行同一条 `load --sink mock-materials-db` 命令，导入同一份标准记录文件，
再查询数据库：物料数量应仍为 24。中断续传和平台字段保护的复现步骤见[验收场景](docs/acceptance.md)。

供应商的邮箱地址在它的映射里标了 `sensitive`，所以不会出现在数据库里：目标端会丢弃敏感字段，除非明确要求哈希或保留。

状态（缓存、状态库、凭证、审计日志）默认放在 `~/.local/share/catalog-sync`，除非你设置 `CATALOG_SYNC_HOME` 或传 `--workspace`。
它特意放在仓库之外：缓存里是别人的数据。

文件目标端也写在那里。以 `{workspace}/` 开头的目标端 `path` 会按当前使用的工作区解析，所以一次运行产生的记录不会落进检出目录；
其他相对路径照旧相对于配置文件解析。演示里只有两个文件是普通路径，因为它们是命令行参数而不是目标端：`out/materials.jsonl`
（`normalize --out` 的输出文件）和 `out/platform.db`（参考平台自己的数据库）。两者只装 Mock 的虚构数据，而且 `out/` 已被 git 忽略。
对真实数据源，也请把 `--out` 指到工作区里的路径。

**永远不要把目标端指向云同步文件夹（iCloud/Dropbox/OneDrive）。** 这些记录是别人的数据，同步客户端会把它们复制到你没有选择的服务器上，
还可能在 SQLite 文件写到一半时复制它。

### 离线：回放已保存的快照

仓库还自带一份合成快照（`examples/synthetic`，由 `tools/make_synthetic.py` 用明显虚构的数据生成）。`replay` 数据源用它代替真实 API，
所以连本地服务都不需要。下面这个代码块同样会在每次改动时由测试套件原样执行。与上面的代码块不同，它需要仓库的检出：快照和 `demo` 配置档所指的映射都是仓库里的文件，不在安装好的包里。

<!-- ci:run -->
```console
$ uv run catalog-sync run demo
$ uv run catalog-sync runs list
$ uv run catalog-sync cache purge --dry-run
```

`run demo` 会拉取快照，用 Mock 物料映射规范化，再写入三个目标端：`{workspace}/out/catalog.db`（SQLite）、`{workspace}/out/catalog.jsonl` 和 `{workspace}/out/catalog.csv`。
`runs list` 列出工作区里的各次运行，`cache purge --dry-run` 显示保留策略会删除什么（现在还没有要删的）。

```console
$ WORKSPACE="${CATALOG_SYNC_HOME:-$HOME/.local/share/catalog-sync}"
$ sqlite3 -header "$WORKSPACE/out/catalog.db" "select source_id, code, name, weight_gsm from materials limit 3"
```

也可以一步一步地跑管道：

```console
$ uv run catalog-sync pull demo                      # raw responses into the workspace cache
$ uv run catalog-sync normalize mock.materials --run latest --out records.jsonl
$ uv run catalog-sync load --sink local --input records.jsonl --dry-run
$ uv run catalog-sync load --sink local --input records.jsonl
```

</details>

<a id="快速开始"></a>

## 接入自己的服务

合成数据示例跑通后，再确定自己的系统需要适配哪几部分：

| 接入步骤 | 你需要提供的内容 | 指南 |
|---|---|---|
| 来源 | 访问授权，以及适配来源接口的已安装或自定义 Python 连接器 | [连接器指南](docs/connector-guide.md) |
| 清洗 | 字段映射、编码表和空值处理策略 | [数据契约](docs/data-contract.md) |
| 目标 | 已配置的数据库目标端、兼容 HTTP 接收端或自定义适配器 | [平台契约](docs/platform-contract.md) |
| 运行 | 命令行运行，或携带配置的 Docker 部署 | [部署指南](docs/deploy.md) |

已有连接器时，`init` 会引导你填写授权、来源参数、登录方式、试采集和接收目标。
它不会自动为尚未支持的服务生成接入能力。请在与示例分开的检出／配置中运行：

```console
$ uv sync
$ uv run catalog-sync init
```

它依次做这些事：

1. **选连接器。** 列出已安装的所有连接器，每个一行说明；连接器自己的设置（比如服务的地址）按它的声明来问。
2. **逐项填写合规声明**，每一项都附说明：数据归谁、凭什么授权读取（`own_account`、`customer_authorized`、`contract`、`public_data`）
   以及授权记录在哪、你读过的条款、你会遵守的请求频率、原始缓存最长保留多久。每个回答都用闸门同样的规则校验。
   条款审阅日期**只有在你确认读过条款之后才会默认为今天**：`init` 不会替你选日期，没读过就在发出任何请求之前停下。
3. **登录**，用连接器提供的方式之一：HTTP 登录（`--form`）、浏览器（`--browser`，需要人输入时开可见窗口）、或粘贴 cURL 命令。
   账号来自环境变量，从不写进文件。
4. **写文件之前先看一眼。** `inspect` 数据源，拉一个小**样本**（默认 5 条），并以表格展示**规范化**后的结果，令牌已遮蔽，`sensitive` 字段不显示。
5. **选择记录去哪**：SQLite、Postgres、通过 HTTP 送到你的平台（见[平台契约](docs/platform-contract.md)）、JSONL 或 CSV。
   它从不向你要密钥：需要令牌或连接串的地方，你只填存放它的**环境变量名**。
6. **最后写出 `catalog-sync.toml`**，并打印要执行的命令。已有同名文件时，没有 `--force` 就不会覆盖；文件里绝不会写入任何密钥。

登录、样本拉取和规范化各自会在审计日志里留下原有的记录，`init` 再加一条自己的。

`init` 做不到的事，先说清楚，免得意外：

- **它只问连接器标为必填、或几乎人人都要设置的项**（比如服务的地址）。其余设置一律保持默认值，事后在文件里改。
- **没有声明设置的连接器，会被如实标出**，`init` 也就没有什么可问：它的 `[sources.NAME]` 表得手写
  （[示例配置](catalog-sync.example.toml)给出了格式；连接器怎样声明设置，见[连接器指南](docs/connector-guide.md)）。
- **`--non-interactive` 要求每个回答都给齐。** 没有回答又没有默认值的问题会报错并指出键名。合规声明的回答刻意不设默认值：
  它们是只有你才能作出的事实陈述。
- **样本拉取是一次真实的运行。** 它向真实的服务发真实的请求，写入原始缓存和审计日志，所以合规声明排在它前面。
- **它打印出 `run` 命令就结束。** 它不做任何定时安排：把那条命令交给 cron、systemd 定时器或你自己的任务调度去调用，是你的事（[docs/deploy.md](docs/deploy.md) 说明了怎么做，附带容器镜像和 compose 栈）。
- **它无法判断你的声明是否属实**，只能检查它是否完整、前后一致（[这之后留给你的是什么](docs/compliance.md)）。

脚本或 CI 里，把回答写进文件，一个问题都不问：`catalog-sync init --answers answers.toml --non-interactive`。
[`examples/init/mock-materials.toml`](examples/init/mock-materials.toml) 列出了所有的键；它把内置的 Mock SaaS 配好，
目标端是一个 SQLite 文件和参考平台，测试套件在每次改动时都会运行它。

无需真实账号即可先运行[合成数据示例](#运行合成数据示例)。

## 支持范围

| 范围 | 第一版的边界 |
|---|---|
| 实体 | 物料和供应商；其他业务实体需要扩展。 |
| 来源 | 内置合成数据源和离线回放。真实 SaaS 或企业服务需要兼容连接器与访问授权。 |
| 目标 | JSONL、CSV、SQLite、Postgres、HTTP。HTTP 已验证仓内参考接收端；CSV 有契约测试，未覆盖完整验收场景。 |
| 缺失值 | 按显式规则使用后备字段或保留为空，不推测数据。 |
| 访问方式 | 不支持通用 HTML 抓取、生成 JavaScript 请求签名、验证码或无人处理的二次验证。 |
| 发布证据 | 原冻结候选的本地 18 项验收通过；公开候选的 GitHub CI 待运行。见[已测环境与版本说明](docs/acceptance.md)。 |

Docker 打包运行环境。来源权限、连接器适配和接收端兼容性，仍需按你的系统配置和验证。

## 反馈试用情况

请说明来源类型、目标类型、执行到哪一步、遇到的第一个阻塞，以及使用的版本和环境。
提供合成示例或脱敏后的字段结构即可，不要上传凭证、私有地址或真实业务记录。
仓库公开后，可通过 Issues 提交。

继续接入时可查阅：[配置参考](#配置参考) · [命令参考](#命令参考) ·
[自定义来源与目标](#编写自己的数据源或目标端) · [贡献指南](CONTRIBUTING.md)。

## 工作方式

```
 SaaS tool                  catalog-sync                                        your platform
 ---------    +-----------------------------------------------------+    -------------------
              |  compliance gate: declaration valid? rate ceiling?  |
   HTTP  ---> |  credential fresh?  (all checked before any call)   |
              |                                                     |
              |  collect         clean + normalize          load    |
              |  pull  ------>  raw cache  ------>  mapping  ---> sinks ---> SQLite, Postgres,
              |  (resumable)    (kept, offline)     + code tables   |        JSONL, CSV, HTTP, or yours
              |                                                     |
              |  audit log: one line per login, pull, normalize,    |
              |  load, run and purge. Counts and ids, never content.|
              +-----------------------------------------------------+
```

1. **采集**（`pull`）。*数据源*（source）插件列出所有条目，取回每个条目的详情记录和图片；原始响应按运行（run）分目录保存在工作区里，
   每个文件都有校验和。被中断或失败的拉取用 `--resume` 继续；再次拉取只会取回有变化的部分。HTTP 请求按你声明的上限限速
   （每分钟上限，以滑动窗口强制执行），遇到 5xx 和 429 时指数退避重试，其他 4xx 不重试。
2. **清洗与规范化**（`normalize`）。只读缓存，所以可以离线运行，改了映射马上就能重跑。一份 YAML *映射*（mapping）逐字段写明：
   由哪些原始值供给、怎么解析、是否 `sensitive`（敏感）。列表行和详情记录并排读取，从不合并；编码表按数据源区分，从不混用。
3. **入库**（`load`）。*目标端*（sink）负责写入记录（文件、数据库，或通过 HTTP 送到你的平台）。只写映射声明“拥有”的字段；`NULL` 就写成 `NULL`，除非该字段标了
   `keep_existing`；从不删除行（源端不再返回的行只做报告）；写入前先备份；`--dry-run` 只展示将会发生什么，不写任何东西。
   SQL 目标端使用 `INSERT ... ON CONFLICT DO UPDATE`，所以带级联删除的子表行在刷新后依然存在。
4. **贯穿全程**：**合规闸门**在没有有效声明时拒绝开始拉取，也拒绝会发出请求的登录；**审计日志**记录做过什么。

输出契约（实体与 schema 版本、规范化文件、`(source, source_id)` 键、来源（provenance）、字段表、空值规则、问题代码（issue codes）、敏感字段策略、失败处理、拥有的字段与 `keep_existing`、备份与审计）见 [docs/data-contract.md](docs/data-contract.md)。
[examples/contract/](examples/contract/) 里有一份生成出来的并排运行结果，覆盖物料（materials）和供应商（suppliers），从 Mock SaaS 经 SQLite 一路到参考平台，并由一个测试与代码核对。

## 连接器

| 连接器 | 读什么 | 合规声明块 | 状态 |
|---|---|---|---|
| `replay` | 一个已保存的快照目录：合成演示数据，或你之前抓取的运行结果。 | 不需要（只读本地文件）；仍会写审计日志。 | CI 中有测试。 |
| `mock.materials`、`mock.suppliers` | 内置的 Mock SaaS：登录后面是虚构数据，有页码分页和游标分页、文件夹树、图片和常见的陷阱。 | **必填**（示例配置已为演示填好）：Mock 是联网数据源，会发出真实的 HTTP 请求，只是发往 localhost。 | CI 中有端到端测试。 |

其他服务的连接器由开发者写一次，其余人无需写任何代码：`catalog-sync new-connector` 会生成一个自带测试且开箱即绿的连接器，
[docs/connector-guide.md](docs/connector-guide.md) 带你从浏览器的 Network 面板一路走到第一次真机拉取
（见[编写自己的数据源或目标端](#编写自己的数据源或目标端)）。其他平台的连接器可以作为独立的包发布，见 [docs/connector-guide.md](docs/connector-guide.md)。

### 内置的 Mock SaaS

`catalog-sync mock serve` 会在 `127.0.0.1:8765` 启动一个小型的假服务。它只用标准库和 Pillow，并且随包一起发布，所以 `pip install` 之后就有。
它的数据由种子（`--seed`）生成，明显是虚构的。有了它，任何人都可以在没有账号、没有法律顾虑的前提下试用、演示和测试这个工具，
连接器的端到端测试跑的也是它。

| 实体 | 分页方式 | 形态 |
|---|---|---|
| `materials` | 页码，带总数 | 一棵文件夹树。列表行有图片、价格为 0；详情记录（嵌套两层，`data.data`）有价格、克重、成分和第二张图片，而 `thumbs` 为空：两者互不包含。 |
| `suppliers` | 游标，没有总数 | 一个平铺的列表；没有详情记录，也没有图片。 |

登录可以走 HTML 表单（`/login`）或 `POST /api/login`。两者都会给出一个 bearer 令牌（带签名的 JWT，除非用 `--token-ttl` 设置，否则有效期一周）
和一个 cookie 会话，API 两者都接受。登录方式有：`login mock --form`（一次普通的 HTTP 请求；CI 用的就是它）、
`login mock --browser [--headed]`（应用页面像单页应用那样发送令牌）和 `login mock --paste-curl -`。账号取自 `MOCK_SAAS_USER` 和
`MOCK_SAAS_PASSWORD`；两者都没设置时，用的是公开的演示账号 `demo` / `demo-password`，它什么也保护不了，并且只要还是这个密码，
服务端就拒绝监听 loopback 以外的地址。

各种陷阱，每个对应 `mock serve` 的一个参数，也都是真实服务会做的事：

| 参数 | 服务端的行为 | 工具的行为 |
|---|---|---|
| `--rate-limit COUNT/SECONDS` | 超过上限就返回带 `Retry-After` 的 429。 | 按要求等待，然后继续。 |
| `--error-every N` | 每第 N 个数据请求返回 503。 | 指数退避后重试。 |
| `--gone-every N` | 每第 N 个物料的详情返回 404。 | 该条标记失败，其余照常继续。 |
| `--expire-after N` | 登录后只能发 N 个数据请求，之后返回 401，直到再次登录。 | 立即停止，退出码 2。与 `--token-ttl` 效果相同，但发生在运行中途，不依赖时钟。 |
| `--token-ttl SECONDS` | 令牌和会话失效：401。 | 立即停止，退出码 2，并说明如何重新登录。401 从不重试。 |
| `--signed-urls SECONDS` | 图片链接带有会过期的签名：403。 | 该图片失败并写明原因；本次运行的其余部分继续，签名也从不落盘。 |

数据*形态*上的陷阱始终开着，因为数据本来就长这样：列表里价格为 0、详情里 `thumbs` 为空、双层信封、克重为 0 表示“没填”，
以及任何编码表里都查不到的成分代码。

```console
$ uv run catalog-sync mock serve --rate-limit 8/5 --error-every 9 --signed-urls 120
```

## 配置参考

一个 TOML 文件：工作目录里的 `catalog-sync.toml`，或用 `--config FILE` 指定。里面的相对路径相对于该文件解析。
[`catalog-sync.example.toml`](catalog-sync.example.toml) 是一份完整、带注释的起点。配置里从不存放凭证。

```toml
[workspace]
home = "~/.local/share/catalog-sync"    # cache, state database, credentials, audit log

[sources.my-source]                      # a named instance of a source plugin
plugin = "mock.materials"
credential = "mock"
# ...the plugin's own options...

[sources.my-source.compliance]           # required for any source that contacts a service
data_owner = "Example Co"
authorization = "own_account"
authorization_ref = "wiki/data-access/12"
tos_url = "https://vendor.example/terms"
tos_reviewed_on = 2026-10-26
max_requests_per_minute = 120
retention_days = 90

[sinks.warehouse]                        # a named instance of a sink kind
kind = "postgres"
dsn_env = "WAREHOUSE_DSN"

[sinks.local]
kind = "sqlite"
path = "{workspace}/out/catalog.db"      # in the workspace, not in the checkout
table = "materials"

[profiles.weekly]                        # what `catalog-sync run weekly` does
source = "my-source"
# mapping = "my-mapping.yaml"            # optional: left out, the connector's own packaged mapping is used
sinks = ["warehouse", "local"]
incremental = true
max_failed_ratio = 0.10                  # more failed items than this share of those fetched: the run fails
```

工作区目录按以下顺序确定：`--workspace`，`$CATALOG_SYNC_HOME`，`[workspace] home`，最后是 `~/.local/share/catalog-sync`。

配置档的 `mapping` 是可选的。省略时，`run` 使用该配置档数据源的连接器装在自己包里的映射（`init` 试跑用的就是它）。`catalog-sync normalize SOURCE` 在省略 `--mapping` 时也是如此；SOURCE 可以是连接器名（`mock.materials`），也可以是 `[sources.*]` 条目，此时取其 `plugin`。不是连接器的数据源（`replay` 可以回放任何快照）不自带映射：配置档或命令必须指明一个，否则以退出码 1 停下并说明。你指明的映射永远优先。

### 合规声明块

| 键 | 含义 |
|---|---|
| `data_owner` | 这些数据归哪个组织所有。 |
| `authorization` | `own_account`（自有账号）、`customer_authorized`（客户授权）、`contract`（合同约定）或 `public_data`（公开数据）。 |
| `authorization_ref` | 授权依据记录在哪里：工单、wiki 页面、合同编号。只是指针，不是内容本身。 |
| `tos_url` | 你审阅过的服务条款。 |
| `tos_reviewed_on` | 你审阅的日期。超过 `tos_review_max_age_days`（默认 180 天）就会打印警告。 |
| `max_requests_per_minute` | 对该数据源每个请求（含重试）强制执行的上限。`request_gap_s` 小于 60 除以该值时会被拒绝。 |
| `retention_days` | 原始缓存最多可保留多久。`run` 在缓存超期时告警；`cache purge` 负责执行。 |

声明块不完整或不合法时，`pull`、`inspect` 和 `run` 会在读取任何凭证、发出任何请求之前就以退出码 4 终止。
`login --form` 和 `login --browser` 也一样，因为登录要提交密码、并自己发出请求：它们需要一个 `credential` 正是所登录凭证的数据源的声明块
（有多个时，其中最低的 `max_requests_per_minute` 决定登录的节奏），没有任何数据源使用的凭证会被拒绝。
`login --paste-curl` 不发送任何东西，不需要声明块。
工具只检查声明块存在且自洽，无法检查它是否属实。工具强制了什么、哪些仍然是你的责任，见 [docs/compliance.md](docs/compliance.md)。

### 目标端（Sinks）

所有目标端都接受 `sensitive_fields = "drop" | "hash" | "keep"`，用来处理映射里标了 `sensitive: true` 的字段。
`drop`（默认）完全不写；`hash` 写入加盐的 sha256（在同一个工作区内保持稳定）；`keep` 原样写入，必须显式选择。

| 类型 | 选项 | 说明 |
|---|---|---|
| `jsonl` | `path` | 每行一个 JSON 对象，按 `(source, source_id)` 合并；原子重写；写入期间加锁。 |
| `csv` | `path` | 同样的合并方式。表格软件会当作公式执行的文本（单元格以 `=`、`+`、`-`、`@`、制表符或回车开头）会在前面加一个撇号写入；读取时还原。数字不受影响。 |
| `sqlite` | `path`、`table`（默认 `materials`） | `INSERT ... ON CONFLICT DO UPDATE`；只增不减的列；用在线备份 API 备份，备份在本次入库自己的写事务里进行。写入同一个文件的多次入库轮流进行：后来的最多等 5 秒，等前一个提交，之后以清楚的错误停止。 |
| `postgres` | `dsn_env`、`table`、`schema` | 需要 extra：`pip install 'catalog-sync[postgres]'`。连接串从 `dsn_env` 指定的环境变量读取。列类型随字段类型而定（`text`、`bigint`、`double precision`、`jsonb`、`timestamptz`）。备份是 `<table>__backup_<timestamp>`，只包含本次入库将要改动的那些行在入库之前的版本；它是单次入库的安全网，不能替代 `pg_dump`。dry run 在一个会被回滚的事务里执行。入库时对每张表加一把咨询锁（advisory lock），所以写入同一张表的两次入库轮流进行（后来的会一直等，没有超时）；共用一张表的多个 profile 应该都写明 `schema`，这样它们锁的是同一张表。 |
| `http` | `url`、`auth_header_env`、`auth_header`、`auth_scheme`、`batch_size`、`max_requests_per_minute`、`timeout_s`、`allow_plain_http` | 把 JSON 批次 POST 给你运行的平台（见[你的平台](#你的平台http-目标端)）。不做备份：那是平台的职责，入库报告会写明这一点。dry run 只构造请求，不发送。 |

平面文件目标端（`jsonl`、`csv`）在入库期间对 `<file>.lock` 加独占锁，第二个入库进程会被立即拒绝，并给出清楚的提示。

`sqlite`、`jsonl` 或 `csv` 目标端的 `path` 相对于配置文件解析；以 `{workspace}/` 开头的路径则按当前使用的工作区解析
（`--workspace`，然后 `$CATALOG_SYNC_HOME`，然后 `[workspace] home`，最后 `~/.local/share/catalog-sync`：与状态库的顺序相同）。
请用它，让输出留在检出目录之外；`catalog-sync init` 默认就提供它。单独的 `{workspace}`，或斜杠后什么都没有，是配置错误，
不会被当成同名文件夹。**永远不要把目标端指向云同步文件夹（iCloud/Dropbox/OneDrive）。**

### 你的平台（HTTP 目标端）

`http` 目标端把记录分批交给你运行的平台，每批是一次 JSON 的 `POST`：

```toml
[sinks.platform]
kind = "http"
url = "https://platform.example/api/ingest"
auth_header_env = "PLATFORM_TOKEN"       # the NAME of the variable that holds the token
batch_size = 100
```

一次请求只携带映射“拥有”的字段、其中“为空时保留已有值”的字段清单、schema 的名称和版本，以及 run id；每条记录带一个幂等键，
每次请求带一个 `Idempotency-Key` 头（请求体的 sha256），所以重试或重跑绝不会被应用两次。5xx 和 429 带退避重试，
401 或 403 以退出码 2 终止运行，其他 4xx 会连同平台自己解释的开头一段（已遮蔽密钥）一起报告。令牌从 `auth_header_env`
所指的环境变量读取；配置里直接写令牌会被拒绝。普通 `http://` 只允许用于回环地址。重定向不会被跟随，因为令牌会随之去到新地址：目标端会停下，并提示把最终地址用作 `url`。

你的平台要做什么（只写“拥有”的字段、从不删除、幂等地应答）写在
[docs/platform-contract.md](docs/platform-contract.md) 里，附带确切的请求与应答。`catalog-sync mock platform serve`
是实现了它的参考接收端，检查其他所有目标端的同一套契约测试也对着它运行。远端目标端无法备份不在自己手里的数据：恢复是平台的职责。

### 映射（Mappings）

映射是一份 YAML，里面是它所**拥有**的每个记录字段各一条规则；入库时绝不会碰映射没有列出的字段。

```yaml
version: 1
source: mock.materials
schema: material                          # what kind of record this fills; material if left out
fields:
  name:
    from: [detail.name, list.name]        # the first non-empty value wins
  weight_gsm:
    from: [detail.weight]
    parse: gsm
    zero_is_missing: true                 # a zero from the service means "not filled in"
  composition:
    from: [detail.composition]
    parse: composition
    scope: mock.saas/materials            # the code table; scopes are never merged
  folder_paths:
    from: [context.folder_paths]
    on_null: keep_existing                # an empty value keeps what the sink already holds
  supplier:
    from: [detail.some_field]
    sensitive: true                       # sinks drop it unless told otherwise
```

取值来源有三处：`list.<path>`（原样的列表行）、`detail.<path>`（详情记录）和 `context.<path>`（遍历过程中得知的信息，比如文件夹路径）。
解析器：`text`、`list`、`split`、`gsm`、`width_cm`、`number`、`integer`、`timestamp`、`money`、`composition`、`raw`。映射能填的字段，就是它的 schema 所声明的字段
（`material` 的是：`code`、`name`、`category`、`supplier`、`composition_raw`、`composition`、`weight_gsm`、`width_cm`、`price`、
`use_cases`、`tags`、`folder_paths`、`images`），其余任何内容用 `extra.<namespace>.<name>`。每条记录还带有 `issues`（无法理解的内容，例如 `weight_gsm:source_zero` 或
`composition:unknown_code:X`）、来源版本、指向原始响应的引用，以及抓取时间。图片带有从缓存文件里读出的宽和高。

### 实体 schema

记录长什么样，由它的实体 schema 决定，所以同一条管道既能装面料库，也能装供应商列表，或者你自己定义的类型。schema 是一份 YAML 文件：

```yaml
version: 1
entity: supplier
schema_version: 1                       # written into every record; bump it when a field changes meaning
key: [source, source_id]                # sinks key every row this way
fields:
  name: {kind: text}
  rating: {kind: real}
  lead_time_days: {kind: integer}
  certifications: {kind: json, of: texts}
  listed_at: {kind: timestamp}
```

`kind` 是目标端存储该字段的方式：`text`、`integer`、`real`、`timestamp`（带偏移量的 ISO 8601 字符串）或 `json`。`json` 字段用 `of`
说明里面装的是什么：`texts`（字符串列表）、`money`、`composition`、`images` 或 `object`。字段可以指定一个解析器，作为规则没写 `parse` 时的默认值
（`parse: gsm`）。每条记录还固定带有 `source`、`source_id`、`extra` 和来源信息字段，所以 schema 不能再声明这些名字。

内置两个 schema：`material`（内置；映射没写 `schema` 时就是它）和 `supplier`（由 mock 连接器注册）。映射用 `schema: supplier`，或用一个紧挨着它的
schema 文件路径，来指明要填哪一个；插件把自己的 schema 注册到 `catalog_sync.schemas` entry point 组。映射里出现 schema 没有声明的字段，
在读取映射时就会被拒绝，此时还没碰任何数据。规范化后的文件自带它的 schema，所以 `load` 不需要别的东西。

## 命令参考

```
catalog-sync [--config FILE] [--workspace DIR] COMMAND

init [--answers FILE.toml] [--non-interactive] [--force]
login CREDENTIAL [--paste-curl FILE|-] [--form] [--browser [--headed]] [--force]
auth status CREDENTIAL
inspect SOURCE [--folder ID] [--option KEY=VALUE]
pull SOURCE [--stage list|detail|assets|all] [--resume RUN] [--max-items N] [--full] [--option KEY=VALUE]
normalize SOURCE --mapping FILE --out FILE.jsonl [--run RUN|latest]
load --sink NAME --input FILE.jsonl [--dry-run]
run PROFILE [--max-failed-ratio RATIO]
schedule PROFILE --every DURATION [--jitter DURATION] [--max-consecutive-failures N] [--max-failed-ratio RATIO] [--once]
health [--max-age DURATION|Nx-interval] [--profile PROFILE]
audit show [--last N] [--json]
runs list [--source SOURCE]
runs show RUN
cache purge [--older-than 30d] [--source SOURCE] [--include-running] [--dry-run]
mock serve [--host HOST] [--port N] [--seed N] [--materials N] [--suppliers N] [--token-ttl SECONDS] [--rate-limit COUNT/SECONDS] [--error-every N] [--gone-every N] [--expire-after N] [--signed-urls SECONDS] [--pid-file FILE] [--detach] [--verbose]
mock stop --pid-file FILE
mock platform serve [--host HOST] [--port N] [--db PATH] [--token-env NAME] [--pid-file FILE] [--detach] [--verbose]
new-connector NAME [--dir PATH] [--auth bearer|cookie|api_key|form] [--pagination page|token|offset] [--catalog-sync-source auto|pypi|path]
```

| 退出码 | 含义 |
|---|---|
| 0 | 成功。 |
| 1 | 失败：意外错误（其 traceback 会被记录，且已脱敏），或者一次 `run` 中失败的条目超过所取条目的 `--max-failed-ratio`（已到达的数据仍会被规范化并入库）。 |
| 2 | 凭证缺失、已过期或无法获得。错误信息会说明如何续期。 |
| 3 | 失败熔断器触发（连续多个条目失败，或一次运行中对同一个列表取回的页数超过 `max_pages`）。用 `--resume` 继续。 |
| 4 | 数据源的合规声明缺失或不合法，或者 `login --form` / `login --browser` 找不到使用该凭证的数据源的有效声明。没有发出任何请求。 |
| 5 | 带条目失败完成：运行结束了，但有些条目失败。`pull` 只要有条目失败就以 5 退出。`run` 在失败条目不超过它尝试取回的条目的 `--max-failed-ratio`（默认 `0.10`，或该 profile 的 `max_failed_ratio`）时以 5 退出，超过则以 1 退出。因未变化而跳过的条目不计入，在两个阶段都失败的条目只算一次。 |
| 130 | 被中断。 |

`cache purge` 会删除超出保留期的运行的原始响应：保留期取各数据源声明的值，或用 `--older-than`（以天为单位）指定。
仍有未变化条目依赖的运行**绝不会**被删除；它会被列为“保留”，并给出依赖它的条目数，而 `pull --full` 能把这些条目转移到新的运行里，
旧的运行随后就可以清理。每次清理都会写入审计日志。

`schedule` 就是定时执行的 `run`：前台运行，直到收到 SIGTERM 或 SIGINT（`--every 30m`、`6h`、`1d`；`--jitter`
会在每次间隔上再加一段不超过该值的随机时间；间隔从一轮结束时开始算）。每轮写一行 `schedule` 审计记录；如果同一 profile
的另一个调度器已持有工作区里的 `schedule-PROFILE.lock`，它会拒绝启动。每轮的结果对循环的影响：

| 这一轮 | 循环 |
|---|---|
| 成功 | 等待 `--every`，再来一轮。 |
| 结束时有条目失败，但不超过所取条目的 `--max-failed-ratio`（`run` 退出码 5） | 算成功，在输出和审计记录里标为**降级**（outcome 为 `ok_with_failures`）：不退避，连续失败的计数重新开始。 |
| 凭证已过期或缺失（退出码 2） | 每一轮都会说明，并继续循环：只有在宿主机上重新登录才能解决，且不计入失败。 |
| 合规声明缺失或不合法（退出码 4） | 以退出码 4 停止。不可重试。 |
| 其他情况，包括失败条目超过 `--max-failed-ratio` | 先等 1 分钟，再 2、4……分钟，最长不超过 `--every`；连续失败 `--max-consecutive-failures` 次（默认 5）后以退出码 1 退出，让守护进程明显地重启它。 |

收到信号后，进行中的这一轮会跑完（puller 没有可以中途取消的点），然后以 0 退出；再收到一次信号则当场中断这一轮（退出码 130），
之后可以用 `pull --resume` 继续该次运行。`--once` 只跑一轮，并以这一轮的退出码退出（降级的一轮是 5）。每轮开始前，如果凭证的 `renew_before_days`
比 `--every` 短，会给出警告。

`health` 在最近一次成功的 `schedule` 不早于 `--max-age` 时退出 0，否则退出 1。降级的一轮也算成功，此时 `health` 会打印
`healthy, degraded: ...`，方便人看出来。`--max-age` 是形如 `90m` 的时长，或调度器所用间隔的倍数：默认的 `2x-interval` 是两倍间隔
再加上最长的抖动（审计记录里写着间隔和 `jitter_seconds`，所以 `health` 不需要参数）。它只读审计日志，适合用作 Docker 的 `HEALTHCHECK`。

仓库附带 `Dockerfile` 和一套 compose 栈（调度器、Postgres，以及用来试跑的 Mock SaaS 和参考平台）：登录由一次性容器完成，
重启和停止的处理对应上面的退出码，另有一个 CI 任务把它从头到尾跑一遍。在 `deploy/.env` 里为 `deploy/.env.example`
列出的变量填好值之后：

```console
$ docker compose -f deploy/compose.yaml up -d mock-saas mock-platform postgres
$ docker compose -f deploy/compose.yaml run --rm login
$ docker compose -f deploy/compose.yaml up -d
```

[docs/deploy.md](docs/deploy.md) 接着讲怎样把它指向真实的服务和你自己的平台、凭证续期、备份、升级、改用 systemd 运行，以及安全注意事项。

## 编写自己的数据源或目标端

插件通过 Python entry point 发现，所以可以放在另一个包里，公开或私有都行。核心代码从不 import 任何插件。

### 一个下午写好一个连接器

如果某个服务的网页应用是从 JSON 接口取数据的，而且你自己的登录就能调用这些接口，就不必从下面的接口定义写起。
`new-connector` 会生成一个包，它对着一个虚构的服务就已经能跑通；下面这段每次变更都会由测试套件实际运行：

<!-- ci:run -->
```console
$ uv run catalog-sync new-connector acme --dir out/connectors/acme
$ uv run pytest -q -c out/connectors/acme/pyproject.toml --rootdir out/connectors/acme out/connectors/acme/tests
```

生成的包自带 `pyproject.toml` 和 entry point、配置项、API 调用、凭证类型（`--auth bearer|cookie|api_key|form`）、
支持页码、下一页令牌或 offset 分页的数据源（`--pagination`）、schema 与映射、带虚构数据的假服务、
对着假服务把连接器跑完拉取与规范化的测试，以及一个合规声明块——占位符没替换之前，合规闸门会拒绝它。
所有需要你改的地方都标了 `TODO(connector)`。可复用的积木是公开的，在 `catalog_sync.sdk`（选项、连接、分页、文件夹遍历、图片、凭证）
和 `catalog_sync.testing`（共用一个时钟的假服务、以数据集为准绳的比对）里。
[连接器指南](docs/connector-guide.md)讲完整条路：在浏览器开发者工具里找接口、选认证方式、写合成夹具（绝不用抓来的真实响应）、
填合规声明块，以及第一次真机 `inspect` 和 `pull`。它也如实说明 SDK 做不到什么：由 JavaScript 计算请求签名、
只有 HTML 的页面、验证码。

### 数据源（Source）

```python
# my_package/source.py
from collections.abc import Iterator, Mapping
from pathlib import Path

from catalog_sync.models import JsonObject
from catalog_sync.sources.base import AssetRef, ListedItem
from catalog_sync.state import Checkpoint


class MySource:
    name = "my.source"

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> "MySource":
        # `options` is the [sources.NAME] table. For a source that contacts a service it also
        # holds max_requests_per_minute (from the compliance block), credential (a handle), ...
        return cls()

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        # Yield every item. Record your place with state.set_frontier() so that a run can resume.
        raise NotImplementedError

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        raise NotImplementedError

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        raise NotImplementedError

    def fetch_asset(self, ref: AssetRef) -> bytes:
        raise NotImplementedError
```

```toml
# my_package's pyproject.toml
[project.entry-points."catalog_sync.sources"]
"my.source" = "my_package.source:MySource.from_options"
```

请求请用 `catalog_sync.http.HttpClient` 加 `RateLimiter(options["max_requests_per_minute"])`（`catalog_sync.sdk.Connection` 已把两者接好），这样限速、退避和声明的上限在所有地方都一致；
单个条目的问题抛 `ItemFailed`，凭证被拒抛 `AuthExpired`，响应结构不对抛 `SourceShapeError`。只读本地文件的数据源还可以注册到
`catalog_sync.local_sources`，从而免于填写合规声明块。可选：实现 `inspect(folder)` 以支持 `inspect` 命令；
编码表注册到 `catalog_sync.code_tables`，实体 schema 注册到 `catalog_sync.schemas`，登录方式注册到 `catalog_sync.credentials`
（提供 `form_login` 就成了无需浏览器的普通 HTTP 登录）。`catalog_sync.plugins.mock` 是这一切的一个小而完整的示例。连接器必须遵守的规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

连接器的设置（键、类型、默认值、一行说明）只声明一次，写成 `SettingSpec`，放在读取它们的代码旁边，并连同一行摘要、凭证类型和映射文件，
作为 `ConnectorInfo` 注册到 `catalog_sync.connectors` entry point 组。`catalog-sync init` 就是靠这份声明在不了解连接器代码的情况下把它配置好的；
`new-connector` 会生成这样一份声明，它的测试会拿声明去核对配置类。做法见[连接器指南](docs/connector-guide.md)。

### 目标端（Sink）

目标端实现 `open()`、`backup()`、`upsert(batch, *, dry_run=False)` 和 `close()`（见 `catalog_sync.sinks.base`），
对一行数据的含义一无所知，并注册到 `catalog_sync.sinks`。检查内置目标端的同一套契约测试，也提供给你的目标端使用：

```python
import pytest

from catalog_sync.testing.sink_contract import CONTRACT_CHECKS, SinkHarness, shape_of


def make_harness(workdir):  # build your sink in `workdir`, and say how to read its rows back
    sink = MySink(workdir / "store")
    # shape=shape_of(schema) runs the same checks on every column of one of your entity schemas
    return SinkHarness(sink, read_rows=..., read_backup=...)


@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_my_sink_honours_the_contract(check, tmp_path):
    check(make_harness, tmp_path)
```

契约：只写“拥有”的字段；`keep_existing_if_null` 保留已存的值；从不删除或整行替换；每次入库之前都先 `backup()`，且备份里是入库前的行；
dry run 报告入库会做什么，但什么都不写。

自己不存储数据的目标端（HTTP 目标端）无法保留备份，也无法预测 dry run 的结果，它如实声明而不是假装：
`backup_supported = False` 与 `dry_run_predicts_counts = False`。它的备份回执带一条说明，写明恢复是谁的职责；
dry run 不报告计数，而在报告的备注里解释原因；契约测试会检查这个声明，正如检查其他目标端的行数据一样。

## 负责任地使用

- 只导出你有权访问的数据，并且只用于你有权使用的目的。
- 阅读你所拉取的服务的服务条款，并记下阅读的日期。遵守它的访问频率限制：声明的上限是上限，不是目标值。
- 如实填写合规声明块。工具无法判断你是否如实。
- 工具不做任何规避：不带反检测参数，不轮换 user-agent 或 IP，遇到 429 退避，不破解验证码。需要真人的登录由真人完成。
- 请求会表明自己是谁发的。默认的 `User-Agent` 是 `catalog-sync/<version> (+project URL)`：工具及其版本，绝不是某个人或某个账号。
  连接器只有在 [docs/compliance.md](docs/compliance.md#user-agent-policy) 列出例外及其理由时，才可以改发浏览器的 User-Agent
  （连接器包在自己的 README 里列出它的例外）。`login --browser` 使用的浏览器保留它自己的 user-agent。
- 重定向不会被盲目跟随。HTTP 客户端从不让库自动跟随重定向，POST 也绝不会被重新发送（登录表单不能把密码带到响应所指的任意地方）。
  图片下载最多手动跟随 5 次重定向：每一跳都按连接器的 URL 规则检查，计入请求上限，绝不从 https 降级到 http，
  一旦换到另一个源（origin）就丢掉 `Authorization` 和 `Cookie`。`https_only()` 规则会拒绝 `localhost` 以及私有、回环和链路本地地址，
  除非连接器有意放行。
- 缓存里是第三方数据，常常受版权保护。它放在你的工作区里，位于本仓库之外，绝不能提交或公开。请设置保留期并运行 `cache purge`。
- 凭证来自环境变量或工作区里权限为 `0600` 的文件。它们不会被写入日志，工具也不会解析 `.env` 文件。
- 本项目是独立的，与任何一个连接器所对接的厂商都没有关联，也未获其认可或支持；相关产品名称均为其各自所有者的商标。

## 安全

凭证不会出现在日志、错误行、快照或审计日志里：代码本身就被写成不往里放，脱敏层会拦住漏网的内容，还有一个测试把金丝雀令牌（canary token）
灌过每一条路径来验证。请私下报告漏洞，见 [SECURITY.md](SECURITY.md)。本仓库公开之前，整个历史都会被扫描，查找机密词条
（[docs/publishing.md](docs/publishing.md)）。

## 开发

```console
$ uv sync
$ uv run ruff check . && uv run ruff format --check .
$ uv run mypy --strict src
$ uv run python tools/check_nesting.py src tools
$ uv run pytest -q
```

测试在禁用套接字的条件下运行，只使用合成数据；启动 Mock SaaS 的测试只允许访问 `127.0.0.1`，别的一概不通。CI 工作流配置为在 Python 3.11、3.12 和 3.13 上运行测试，
用 `mypy --strict` 做类型检查，用 `postgres:16` 服务容器测试 Postgres 目标端，并且把上面各个演示里的命令原样执行一遍。规则见 [CONTRIBUTING.md](CONTRIBUTING.md)，其中包括：每一道新增的检查，都要有一个能让它失败的测试。

从干净安装出发的验收运行（构建软件包、装进全新环境，再用装好的程序跑完断点续拉、重跑、部分失败、敏感值和 Postgres 等场景）见 [docs/acceptance.md](docs/acceptance.md)，其中有一次完整的运行记录和它所覆盖的范围。

在 macOS 上，Python 3.13+ 会静默忽略带有“隐藏”文件标志的 `.pth` 文件，而一些桌面工具会给 `.venv` 这类点号目录里的所有内容打上该标志。
如果这时 `catalog-sync` 报 `ModuleNotFoundError: catalog_sync`，请运行 `chflags -R nohidden .venv`，或者用 `uv sync --no-editable` 安装一份普通副本。
测试套件不受影响：pytest 会自己把 `src` 加进路径。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
