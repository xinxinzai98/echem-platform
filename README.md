# Start–stop Studio

独立的电化学稳定性分析程序。默认 Docker 发布只包含数据采集、材料管理、启停/恒流分析、CV–EIS 分析和只读监控，不包含旧平台的仪器控制入口。

旧电化学平台的源码仍保留，但不进入此镜像。其历史说明见 [旧平台文档](docs/legacy-echem-platform.md) 和 [旧安全说明](docs/legacy-echem-security.md)。

## 当前工作流

1. 在服务器本机配置实验电脑的搜索位置，下载稳定文件或上传本地数据。
2. 在材料库保存完整名称、收藏、备注和入图选择。
3. 更新平台分析，选择原始数据或水位补偿。首次建立计算缓存后，未变化材料复用结果；参照模型更新会使相关补偿结果重新计算。
4. 在稳定性分析中按工步选线、高亮、缩放或检查异常。工作站与蓝博的电压标尺分开，未知工步不能混合比较。
5. PDF 按需生成，不是分析的必经步骤。材料库从已完成分析导出图集；高亮导出提供 Excel 完整记录和处理数据，以及 PDF 图。

CV–EIS 不跨测试阶段借用 EIS。模糊配对和未知在线 iR 状态须在管理端确认；未知或已在线补偿时不再次离线补偿。启停分析不进行 RHE 换算。

## 访问方式

| 入口 | 默认位置 | 权限 |
| --- | --- | --- |
| 服务器管理端 | 服务器上的 `http://127.0.0.1:18787/start-stop` | 上传、采集、配置、计算、确认 |
| 局域网查看端 | `http://<服务器局域网IP>:18788/start-stop` | 查看、选线、高亮、导出 |
| 健康检查 | 同入口的 `/healthz` | 仅表示 HTTP 服务存活 |

当前实验室主机为 192.168.110.225。Windows 部署的局域网入口按既有设置无登录，只适用于受信任的实验室网络。不要直接映射到公网。完整边界见 [SECURITY.md](SECURITY.md)。

## Docker 构建与运行

使用唯一的正式构建入口 `Dockerfile`。基础镜像固定为官方 Python 3.12.13 slim-bookworm 的 SHA-256；运行依赖版本由 `requirements.docker.txt` 固定。历史 hotfix Dockerfile 不作为新发布基础。

Windows 在手动打开 Docker Desktop 后，用现有部署的 `.env` 保留数据卷、SSH 目录、缓存目录、端口和备份配置，再更新镜像版本：

```powershell
.\scripts\docker-compose.ps1 build local
.\scripts\docker-compose.ps1 up -d --no-build
```

macOS/Linux 使用 `scripts/docker-compose.sh`。脚本不会安装或自动启动 Docker Desktop。

正式发布应从已提交源码打包，记录 commit、每个文件的 SHA-256、测试结果和最终镜像摘要。不能把未提交的临时补丁当作可复现发布。详见 [发布与回退](docs/start-stop-release.md)。

## 数据与恢复

- 原始字节、文件版本、材料配置、任务、分析来源及派生产物保存在独立 SQLite 仓库中。Windows 使用 Docker 原生 Linux 数据卷，避免把 SQLite 放在 NTFS 共享绑定目录上。
- 网页缓存可以由数据库恢复；不要把缓存当成唯一数据副本。运行状态投影有有效期，过期或缺失时显示“未提供”，不推断为零。
- 计算缓存可丢弃，按源哈希、算法和数值库版本复用；不替代原始数据或封存结果。
- 普通下载、绘图和代码更新不强制全量备份。数据库结构迁移或修复前必须完成校验备份；定期备份按现有低频策略运行。
- 历史产物清理必须先做只读保留计划。当前结果、指定回退版本和原始来源受保护；逻辑字节数不等于可直接回收空间，禁止绕过不可变引用删除。

```sh
python scripts/plan_start_stop_retention.py --database /path/to/start-stop.sqlite3
```

## 开发与验收

Python 环境安装 `requirements.docker.txt` 后运行：

```sh
python -m unittest discover -s tests -b
```

正式验收还需 Node.js 执行真实 JavaScript 行为测试，并完成浏览器的上传、配置、计算、只读读取、选线及导出流程。跳过的测试不能记为通过。

整改状态和仍待验证的项目记录在 [审计执行记录](docs/platform-audit-execution.md)。隔离浏览器样例、截图和生成的导出文件仅用于测试，不是实验数据。

## 模块边界

`start_stop_service.py` 负责固定 HTTP 路由和权限边界。`echem_platform/start_stop.py` 组合材料、查询、任务运行、发布、导出等独立模块。科学计算脚本位于 `docker/start_stop_analysis/`；原始记录统计和显示抽样分离。

监控仅反映软件、串口枚举和文件写入证据。六个活动任务槽位未绑定具体 COM 口时，不代表六台物理仪器的可靠身份映射；它不能代替现场巡视、仪器保护或急停。
