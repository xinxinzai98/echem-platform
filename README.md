# 电化学测试平台 V0.2 + V0.3 阶段 C

数据工作台版本：0.2.0-dev.3

网页 Dry-run 版本：0.3.0-dev.2

CHI 离线协议编译器版本：0.3.0-dev.1

桌面工作台外壳与 60 秒 OCP 安全门版本：0.3.0-dev.6

这是一个默认只读的本地电化学数据工作台。V0.2 提供真实数据接入和解析溯源基础；V0.3 阶段 A 增加 CHI 协议的离线校验、宏编译和静态复核；阶段 B 增加浏览器中的连续工步编辑、草稿保存、校验报告和宏预览；阶段 C 增加一套默认关闭、只允许 60 秒 OCP 的 Windows 本机安全门。公开配置不会启动仪器，平台始终不直接访问 COM3 / COM4。

## 当前能做什么

- 监听指定数据目录并建立文件索引
- 识别常见 CHI 文本导出、CorrTest `.cor` / `.z60` 文本数据
- 对 CHI `.bin` 文件只登记元数据和 SHA-256，不尝试逆向修改
- 展示 CV、LSV、EIS、OCP、CA/CP 等二维曲线
- 用统一左侧导航在数据工作台、协议编辑和阶段 C 安全门之间切换
- 保存样品编号、材料、电解液、电极面积、标签和备注
- 记录导入、更新和人工编辑审计日志
- 为每条记录保存明确的解析器标识，便于后续按仪器版本复核
- 动态标记源文件是否仍可访问；源文件移动后仍保留缓存曲线和样品信息
- 默认只监听 `127.0.0.1`，仅供 Windows 本机浏览器访问
- 在 Mac 或 Windows 上把经过校验的 JSON 协议离线编译为 CHI `.mcr`
- 对编译结果重新做文件头、ASCII、命令白名单、工步顺序和输出路径静态复核
- 在 `/protocol` 编辑 CV、OCP、LSV 和 EIS 连续工步
- 在平台自己的 SQLite 中保存尚未完成或尚未通过校验的协议草稿
- 在内存中显示规范化工步、预计时长、人工复核警告、目标文件和 CHI 宏预览
- 在 `/monitor` 只读显示 CHI 实例、可执行文件哈希、目录和活动任务预检
- 仅在未跟踪的本机配置显式启用后，创建不可变的 60 秒 OCP 运行快照
- 用参数指纹、现场确认和一次性令牌约束唯一的阶段 C 启动路径
- 观察 CHI 返回码和稳定的 `.bin/.txt` 文件，并验证扫描前后源文件未变化
- 用仓库内的 Windows 桌面启动器验证平台与 Explorer 位于同一非零会话

## 安全边界

- 不打开 COM3 / COM4
- 不启动 CHI，不执行 CHI 宏
- 不调用 CorrTest SDK
- 不向被监控目录写入任何文件
- 文件仍在写入时会暂缓导入，等待下一轮扫描
- 原始文件以 SHA-256 指纹标识，平台元数据保存在自己的 SQLite 数据库中
- 公开基础配置中的 `instrument_control_enabled` 必须为 `false`
- 控制关闭时，创建、确认和启动接口返回 404
- 启用只允许来自被 Git 忽略的 Windows 本机覆盖配置
- 阶段 C 只接受一个 60 秒 OCP，拒绝其他技术、多工步和参数指纹变化
- 启动前后都检查现有 CHI 实例，任何实例都会阻止启动
- 拒绝从 OpenSSH、Windows 服务等 Session 0 后台会话启动 CHI；实机运行必须来自当前 Windows 桌面会话
- 网页停止只记录人工停止请求，不强制结束 CHI 进程
- 网页 Dry-run 不写出 `.mcr` 文件；预览结果只保留在浏览器当前页面

## V0.3 阶段 A：Mac 离线编译

公开示例只用于展示字段格式，示例数值不是实验建议，也不能直接作为上机参数。先在 Mac 上运行：

```sh
python3 scripts/compile_chi_protocol.py \
  docs/chi-protocol.example.json \
  --output-folder D:/EchemPlatform/Runs/RUN-DEMO-001 \
  --allowed-run-root D:/EchemPlatform/Runs \
  --macro-output /tmp/RUN-DEMO-001.mcr
```

编译器会独占创建 `.mcr`，目标文件已经存在时拒绝覆盖。省略
`--macro-output` 可以只做协议校验和内存编译，不写文件。无论哪种方式，它都不会
连接 Windows、启动 CHI 或执行生成的宏。

进入任何 Windows dry-run 或真实仪器测试前，必须逐项核对电位基准、范围、扫描
方向、量程、时间、频率和保存目录，并另行获得执行确认。详细说明见
`docs/v0.3-offline-control.md`。

## V0.3 阶段 B：网页 Dry-run

启动平台后打开 `http://127.0.0.1:8787/protocol`。网页支持：

- 添加、复制、删除、启用、禁用和调整连续工步顺序
- 自动保存或手动保存一份本地 SQLite 草稿
- 仅校验，不返回宏正文
- 生成内存中的 Dry-run 宏预览，不写文件、不启动 CHI
- 显示输出文件名、静态复核结果、已知预计时长和无法精确估时的工步

公开默认草稿只展示字段格式，其中的电位、频率、扫速和时间不是实验建议。EIS
Points/Decade 仍要求在未来的 Windows 设备参数页人工复核，EIS 预计时长不会显示
虚假的精确值。阶段 B 的接口与安全边界见 `docs/v0.3-web-dry-run.md`。

## V0.3 阶段 C：60 秒 OCP 安全门

打开 `http://127.0.0.1:8787/monitor` 查看只读预检。默认部署仍锁定控制，不创建
运行目录，也不启动 CHI。只有专门的 Windows 本机 `config.local.json` 同时锁定
CHI 可执行文件 SHA-256 和已复核 OCP 参数指纹后，才可能启用运行接口。

候选协议必须只有一个 60 秒 OCP。可以先离线计算参数指纹：

```sh
python3 scripts/inspect_stage_c_protocol.py path/to/protocol.json
```

真实运行还需要现场逐项确认、手工输入运行编号和一次性令牌。详细设计、恢复语义和
实机验收门见 `docs/v0.3-stage-c-ocp-preflight.md`。

## Windows 本机使用

双击 `start-windows.cmd`。平台会在本机后台启动，并自动打开 Windows 默认浏览器。
数据工作台和协议 Dry-run 均在这台 Windows 电脑本机使用，不依赖 Mac 常驻。

停止平台时双击 `stop-windows.cmd`。

服务仍然只监听 `127.0.0.1:8787`，不需要 Mac、SSH 隧道或局域网开放端口。

阶段 C 旁路验收使用 `start-stage-c-desktop.cmd`，默认监听
`127.0.0.1:8788` 并打开锁定的 `/monitor` 页面。启动器必须由当前已登录用户在
Windows 桌面双击；从 OpenSSH、Windows Service 或 Session 0 调用时会在启动平台
前拒绝。普通双击入口还会拒绝任何已经启用仪器控制的配置，因此打开监控页本身不
构成实机授权。

停止锁定的阶段 C 旁路平台时双击 `stop-stage-c-desktop.cmd`。停止器只会结束当前
部署目录内、占用指定端口的平台 Python 进程；无法验证状态或存在活动仪器任务时
会拒绝停止，不会结束 CHI。

## 本地开发试运行

需要 Python 3.9 或更高版本，不需要安装第三方包。

```sh
python3 app.py
```

浏览器打开 `http://127.0.0.1:8787`。

协议 Dry-run 打开 `http://127.0.0.1:8787/protocol`。

只扫描一次并退出：

```sh
python3 app.py --scan-once
```

运行测试：

```sh
python3 -m unittest discover -s tests -v
```

## 配置实际数据目录

不要把实际实验路径直接写入公开仓库中的 `config.json`。复制
`config.local.example.json` 为 `config.local.json`，然后只修改本地文件中的
`watch_roots`。程序会先读取 `config.json`，再用 `config.local.json` 覆盖本机配置。

`config.local.json` 已被 Git 忽略。路径可以使用绝对路径；相对路径以平台目录为基准。

Windows 示例：

```json
{
  "watch_roots": [
    "D:\\电化学数据\\CorrTest",
    "D:\\电化学数据\\CHI"
  ]
}
```

部署到 Windows 前会生成独立的便携运行环境，避免改动系统 Python、PATH 和注册表。

## 私有数据隔离

- `config.local.json`、`private_data/`、`private_fixtures/` 和 `inventory/` 不进入 Git
- 真实实验数据默认只保留在 Windows 本机；经单独确认的最小样例只能进入 Mac 上被 Git 忽略的私有目录
- 公开测试样例必须先脱敏并经过人工确认
- CHI/CorrTest 帮助文件、SDK 和安装文件不进入公开仓库

## 私有真实样例回归

真实样例复制到 Mac 前必须先获得确认。获准后，把样例放入被 Git 忽略的
`private_fixtures/`，并复制示例清单：

```sh
mkdir -p private_fixtures/chi private_fixtures/corrtest
cp docs/private-fixture-manifest.example.json private_fixtures/manifest.local.json
python3 scripts/validate_private_fixtures.py
```

清单中的文件路径只能相对于清单目录，不能使用绝对路径或 `..`。`id` 应使用
匿名编号，不要写样品名。验证器只读文件，检查解析状态、仪器、方法、解析器、
点数和可选 SHA-256；输出不包含绝对路径、文件名或原始数据，也不会自动生成
或提交报告。

详细边界见 `SECURITY.md`，本地验收结果见 `VALIDATION.md`。
