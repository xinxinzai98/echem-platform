# Security policy and safety boundary

Echem Platform handles untrusted local files and exposes a local HTTP interface. Security reports are welcome, but public issues must not contain vulnerability details, private data, credentials, or vendor-proprietary material.

## Supported versions

| Version | Security support |
|---|---|
| Latest `0.1.x` release | Supported |
| Unreleased development branches | Best effort; reports must name the commit |
| Older tags | Not supported unless a maintainer states otherwise |

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** form:

<https://github.com/xinxinzai98/echem-platform/security/advisories/new>

Include the affected version or commit, operating system, minimal reproduction, impact, and whether a synthetic fixture can reproduce the issue. Do not attach real experimental data, local configuration, logs containing identifiers, credentials, or vendor files.

If private vulnerability reporting is unavailable, open a public issue containing only a request for a private contact channel. Do not include technical vulnerability details in that issue.

The maintainer aims to acknowledge a report within 7 days and provide a status update within 30 days. These are best-effort targets, not a service-level agreement. Please allow time for validation and coordinated disclosure before publishing details.

## Security-relevant scope

High-value reports include:

- malformed, oversized, or ambiguous files that escape parser limits or corrupt state
- path traversal, symlink escape, or writes into watched source folders
- DNS rebinding, non-loopback exposure, cross-origin mutation, or unsafe local HTTP behavior
- SQLite integrity, unauthorized metadata changes, or audit-record tampering
- command injection or unsafe behavior in Windows launcher scripts
- credentials, private paths, real data, vendor binaries, or restricted artifacts in a release
- vulnerable or unaccounted-for release dependencies

Known `0.1.x` hardening limits, including watched-root symlink handling and the absence of a multi-user authentication layer, are listed in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Documenting a limitation does not make an exploit report out of scope.

Reports about scientific interpretation should normally use the bug template unless they also create a security impact.

## 平台会写入的内容

平台只在自身 `state` 目录中写入 SQLite 数据库及其事务文件。保存内容包括：

- 原始文件路径、大小、修改时间与 SHA-256
- 解析后的降采样曲线
- 样品编号、材料、电解液、面积、标签和备注
- 导入与人工编辑审计记录

## 平台不会执行的操作

- 不创建、重命名、覆盖或删除监控目录中的文件
- 不打开 COM3、COM4 或其他串口
- 不启动、停止或控制 CHI760E、CS Studio6 或其他仪器软件
- 不执行 CHI 宏
- 不加载 CorrTest SDK
- 不监听局域网地址；程序拒绝 `0.0.0.0` 等非回环绑定
- 不上传云端，不调用外部网络服务

## 正在写入的文件

当文件修改时间距当前不足配置中的 `stable_age_seconds`，平台会暂缓读取。读取前后若大小或修改时间发生变化，同样会放弃本次导入并等待下一轮扫描。

## 文件完整性

每条记录保存完整 SHA-256。相同路径、相同哈希和相同解析器版本不会重复导入；解析器升级时会重新解析，并保留平台中的样品信息。

SHA-256 只能辅助检查文件身份，不能替代备份、访问控制、数字签名、实验记录审批或数据保留政策。

## 数据与发布安全

真实实验文件、厂商安装包、SDK、帮助文件、私有配置、日志、证书和密钥不得提交。发布前必须运行：

```sh
python3 scripts/check_public_tree.py
```

详细数据规则见 [DATA_POLICY.md](DATA_POLICY.md)，依赖范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 明确不承担的安全功能

Echem Platform 不是仪器联锁、急停系统、备份系统或监管记录系统，不能替代工作站本身的电流、电压、时间限制，也不能替代本地人工巡视。解析成功不证明实验参数、单位或科学结论正确。
