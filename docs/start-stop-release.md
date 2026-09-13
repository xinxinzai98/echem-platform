# 独立分析程序发布与回退

正式入口是根目录 `Dockerfile`。历史 `Dockerfile.*-hotfix` 仅用于理解旧发布，不可作为新版本的基础镜像。

## 源码基线

发布前完成 Python、真实 JavaScript 和浏览器验收。只提交与发布有关的源码、测试和文档；实验数据、数据库、私钥、截图、生成的 Excel/PDF 和镜像归档不进入 Git。

旧平台兼容适配器仍在仓库中，保持回归测试可重现，但 `.dockerignore` 和 Dockerfile 的允许列表禁止把旧仪器控制入口打入独立镜像。

```sh
python scripts/package_start_stop_release.py --version 0.7.0-dev.1 --output output/releases
```

打包器从指定 commit 读取内容，不复制未提交的工作树。运行时文件未提交则拒绝打包。归档附带 `RELEASE-MANIFEST.json`，记录版本、commit、文件清单和 SHA-256；同时输出归档自身校验值。

## 构建与验证

1. 在新的临时目录解包已核验的归档。使用唯一正式 Dockerfile，传入 manifest 中的版本和 commit 作为 `APP_VERSION`、`VCS_REF`，`BUILD_STATE=committed-source`。
2. 构建后记录镜像 ID；若推送到获准的镜像仓库，另记录 registry manifest digest。不要把 image ID 与 registry digest 混称。
3. 先启动隔离验证容器，不挂载生产数据库或 SSH 密钥。检查 `/healthz`、所有静态资源和只读写入拒绝，再用合成数据库走完整流程。
4. 确认生产没有正在采集、计算或导出的任务。保留原 `.env`、原镜像标签、镜像 ID、数据卷名及当前分析代号。
5. 仅改变镜像字段和对应版本/commit；保留端口、数据卷、SSH 挂载、缓存与备份位置。切换后核对管理端与 LAN 的版本、配置修订、数据时间、封存代号和实际下载。

普通代码更新不执行无条件全量备份。若本次涉及数据库结构或内容修复，则必须先完成并验证备份，不得因为耗时跳过。

工作区 `/app/scratch` 使用独立磁盘卷 `start-stop-analysis-scratch`，可通过 `ECHEM_SCRATCH_VOLUME_NAME` 指定名称；不再使用 4 GiB tmpfs 限额，也不以提高容器内存上限替代磁盘容量。该卷只保存可重建工作缓存，不属于原始数据库或备份。

## 离线基础镜像

主机无法直接连接 Docker Hub 时，可在可联网端下载官方 OCI 组件，逐项核对固定摘要后导入。多架构 index 的目标平台必须连同对应 attestation 一起保存，否则 Docker 可能报缺少内容。

本项目固定 Python 3.12.13 slim-bookworm index：

`sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`

linux/amd64 manifest：

`sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af`

目标 Docker 支持 containerd image store 和 `docker image load --platform linux/amd64` 时，导入后还必须用完整 `python:3.12.13-slim-bookworm@sha256:...` 引用执行 `docker image inspect`。仅看到一个可变 tag 不足以证明固定基础摘要可用。

## 回退与首次更新

- 代码回退使用保存的旧镜像和原部署配置，不删除数据库或当前数据卷。
- 新代码首次分析需要建立计算缓存；以后按源文件和算法/运行时版本复用。界面可先读取已发布旧分析，不强迫生成 PDF。
- 旧分析缺少新导出元数据或使用了不同计算代码时，按需 PDF 导出会明确提示先更新分析，不偷偷执行全量重算。
- 历史分析产物的删除属于独立归档决策。只读保留计划不能当作已释放空间，也不能替代备份。
