# 电化学测试平台 V0.2 + V0.3 阶段 A

数据工作台版本：0.2.0-dev.3

CHI 离线协议编译器版本：0.3.0-dev.1

这是一个默认只读的本地电化学数据工作台。V0.2 提供真实数据接入和解析溯源基础；V0.3 阶段 A 增加 CHI 协议的离线校验、宏编译和静态复核。它不会连接串口、启动仪器软件、执行宏或修改原始测试文件。

## 当前能做什么

- 监听指定数据目录并建立文件索引
- 识别常见 CHI 文本导出、CorrTest `.cor` / `.z60` 文本数据
- 对 CHI `.bin` 文件只登记元数据和 SHA-256，不尝试逆向修改
- 展示 CV、LSV、EIS、OCP、CA/CP 等二维曲线
- 保存样品编号、材料、电解液、电极面积、标签和备注
- 记录导入、更新和人工编辑审计日志
- 为每条记录保存明确的解析器标识，便于后续按仪器版本复核
- 动态标记源文件是否仍可访问；源文件移动后仍保留缓存曲线和样品信息
- 默认只监听 `127.0.0.1`，仅供 Windows 本机浏览器访问
- 在 Mac 或 Windows 上把经过校验的 JSON 协议离线编译为 CHI `.mcr`
- 对编译结果重新做文件头、ASCII、命令白名单、工步顺序和输出路径静态复核

## 安全边界

- 不打开 COM3 / COM4
- 不启动 CHI，不执行 CHI 宏
- 不调用 CorrTest SDK
- 不向被监控目录写入任何文件
- 文件仍在写入时会暂缓导入，等待下一轮扫描
- 原始文件以 SHA-256 指纹标识，平台元数据保存在自己的 SQLite 数据库中
- `instrument_control_enabled` 被强制锁定为 `false`

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

## Windows 本机使用

双击 `start-windows.cmd`。平台会在本机后台启动，并自动打开 Windows 默认浏览器。

停止平台时双击 `stop-windows.cmd`。

服务仍然只监听 `127.0.0.1:8787`，不需要 Mac、SSH 隧道或局域网开放端口。

## 本地开发试运行

需要 Python 3.9 或更高版本，不需要安装第三方包。

```sh
python3 app.py
```

浏览器打开 `http://127.0.0.1:8787`。

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
