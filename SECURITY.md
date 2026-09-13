# Start–stop Studio 安全边界

本文件描述独立 `start_stop_service.py` Docker 程序。旧平台的 OCP 安全门和宏编译功能不进入此镜像；历史说明保存在 [旧安全文档](docs/legacy-echem-security.md)，不能作为当前独立服务已启用的能力。

## 网络与权限

- 管理端映射到服务器回环地址，局域网端是单独的只读进程。Host、Origin 和固定路由白名单限制浏览器请求；只读入口在读取请求体之前拒绝写操作。
- 现有实验室 Windows 配置允许内网免登录访问。能访问该端口的设备可读取实验数据，这不是按用户隔离的系统。应限制防火墙来源网段，不得直接暴露公网。
- 只读查看仍允许在临时空间计算图表与生成下载文件，不允许修改源数据库、材料配置、采集位置、人工科学确认或仪器设置。
- 本机恶意进程、Docker 管理员或泄露的 SSH 密钥不在浏览器同源隔离的保护范围内。不要向局域网开放 Docker 管理接口。

## 仪器与实验电脑

程序不打开仪器串口、不发送实验控制命令、不启动 CHI 测试、不修改实验方案。工作站监控使用只读系统信息和文件增长证据；未确认的物理仪器映射必须显式说明。

采集使用固定的 SSH 工作流。蓝博提取使用实验机已安装的软件读取组件，并可在专用支持缓存/临时目录生成辅助程序和派生 CSV，但不改写实验原件。应用的“只读采集”不等于 SSH 账户在操作系统层面只有只读权限；应使用最小权限账号和受限目录。

## 原始数据与科学结论

- 原始文件按版本保存完整字节与 SHA-256。派生记录绑定来源版本，分析发布使用私有临时目录、校验清单及原子切换。
- 哈希证明内容一致性，不证明实验参数、样品身份或科学结论正确。
- 启停不换算 RHE；蓝博电压参照未确认时不与 Hg/HgO 共用纵轴。模糊 CV/EIS 配对和在线 iR 未知状态不能自动变成可定量结果。
- 完整统计必须来自全量记录。旧抽样来源只能预览，不能伪装成完整异常/循环统计或完整数据导出。
- 下载期间变化的文件不会静默覆盖稳定版本。实时预览使用只读快照，与正式入库结果区分。
- Excel 原始记录保留原值，外部文本不能变成可执行公式。导出前后复核所选来源，变化则要求重试。

## 存储、发布与回退

运行使用受限用户、只读容器文件系统、移除 Linux capabilities，并限定可写数据卷和临时空间。原始数据库、临时目录和网页缓存的容量分别检查；并发导出共享临时预算。

普通代码更新不做无条件全量备份。数据库迁移/修复前必须完成可校验备份。历史派生内容存在不可变引用，清理工具默认只读预览，不得直接 DELETE 或 VACUUM 来绕过来源保护。

正式镜像从固定官方基础摘要构建，记录源 commit、源码清单、测试和镜像摘要。保留上一镜像与原有配置供回退；数据模式变化不能仅靠换镜像回退。

## 仓库卫生与报告问题

私钥、口令、真实实验数据、SQLite 文件、未脱敏截图、临时部署目录和厂商受许可约束的二进制不能提交。部署模板可以包含必要的非秘密机器标识，但实际凭据必须独立挂载。

报告问题时提供服务版本、脱敏的任务编号、失败阶段和最小复现。不要公开原始数据库、SSH 配置或含私钥内容的日志。优先保留源文件和校验值，不要先清库或删除失败记录。

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** form:

<https://github.com/xinxinzai98/echem-platform/security/advisories/new>

Include the affected version or commit, operating system, minimal reproduction, impact, and whether a synthetic fixture can reproduce the issue. Do not attach real experimental data, local configuration, logs containing identifiers, credentials, or vendor files.

If private vulnerability reporting is unavailable, open a public issue containing only a request for a private contact channel. Do not include technical vulnerability details in that issue.

The maintainer aims to acknowledge a report within 7 days and provide a status update within 30 days. These are best-effort targets, not a service-level agreement. Please allow time for validation and coordinated disclosure before publishing details.
