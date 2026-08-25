# Start-stop Studio 分级备份策略

## 目标

17 GB 以上的 SQLite 数据库执行“在线复制、SQLite 完整检查、SHA-256”会耗时很久。全量备份不再作为普通代码部署、实验机下载或重新绘图的前置步骤。

## 分级规则

| 操作 | 前置保护 | 是否全量备份 |
| --- | --- | --- |
| 普通 Docker 代码更新 | 数据库只读快速一致性检查；保留旧镜像用于回滚 | 否 |
| 实验机下载、上传、计算、绘图 | 自动执行数据库元数据、表结构、外键、BLOB 长度和当前指针检查 | 否 |
| 数据库结构迁移 | 快速检查后创建并完成校验的全量备份 | 是，强制 |
| 数据库修复 | 修复前创建并完成校验的全量备份 | 是，强制 |
| 定期保护 | 独立 Docker 调度容器运行全量校验备份 | 是，不阻塞网页操作 |

快速检查不会复制数据库，也不会逐字节读取和哈希 BLOB；它不能替代结构迁移或数据库修复前的全量备份。

## 运维命令

Windows Docker Desktop：

```powershell
# 普通部署前或故障排查时的快速检查
scripts\docker-compose.ps1 check

# 结构迁移前的强制全量备份
scripts\docker-compose.ps1 backup --reason schema_migration --compact

# 数据库修复前的强制全量备份
scripts\docker-compose.ps1 backup --reason database_repair --compact

# 启动或恢复低频 Docker 调度器
scripts\configure-start-stop-backup-task.ps1 -ProjectDir C:\path\to\start-stop
```

调度容器默认每 4 周周日 03:00 运行，首次为 2026-08-30 03:00；只要 Docker Desktop 正在运行即可，不依赖 Windows 交互登录或 SSH 会话。错过 15 分钟执行窗口不会在白天补跑。备份保留清理由人工审核后执行，系统不会自动删除任何备份。

Windows 上使用独立的 Docker 卷 `ECHEM_BACKUP_STATUS_VOLUME_NAME` 保存定时任务的小型 JSON 状态文件，网站仅以只读方式挂载该卷。它与大型备份目录分开，不需要放宽备份文件的 Windows 访问权限。
