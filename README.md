# Bedrock Activity Backup

为 Minecraft Bedrock Dedicated Server（BDS）提供由玩家活动触发的在线一致性增量快照。

## 行为

- 第一位玩家进入时开始 30 分钟倒计时。
- 有玩家在线时，每 30 分钟创建一次快照。
- 普通玩家退出但仍有人在线时，不额外备份。
- 最后一位玩家退出时立即创建最终快照，并停止周期备份。
- 下一位玩家再次进入时重新开始计时。
- 仅保留最近 4 份 `verified` 自动快照；`pending`、`failed` 与旧版无元数据目录均不计入数量。
- 手动备份放在自动快照目录之外，不参与轮换。

监听器直接读取 BDS 写入 systemd journal 的连接/断开事件，不定期轮询玩家。只有在断开事件、启动恢复和备份完成后才通过 BDS 控制台执行 `list`，确认实际在线人数。

## 一致性与空间

快照前使用 BDS 自带的 `save hold` / `save query`，确认世界文件处于可复制状态；复制结束后无论成功或失败都执行并确认 `save resume`。文件复制使用 `rsync --link-dest`，未变化的 LevelDB 文件通过硬链接复用，因此多个还原点不等于多份完整世界体积。

## 安全边界

- 单实例文件锁避免并发备份。
- 备份、维护和人工轮换统一使用同一把非阻塞锁。
- 新快照先以 `pending` 状态保存完整清单和 SHA-256；只有隔离 BDS 实际加载世界、未出现 LevelDB repair/损坏/缺失 `.ldb` 迹象后才转为 `verified`、更新 `latest` 并参与轮换。
- 验证失败会保留为 `failed`，记录原因并在同一运行中立即重试；旧 `verified` 快照在新 `verified` 成功前绝不会因轮换删除。`failed` 快照和旧版/无归属元数据目录默认保护，不会被自动清理。
- 每次运行会在 `backups/automatic/runs/` 写入 JSON 审计记录，含保存暂停、复制、恢复、校验、验证、重试以及轮换前后目录列表。
- 磁盘可用空间低于配置阈值时拒绝新快照。
- rsync 使用 `--fsync`，发布前同步新增文件及目录元数据。
- `rename` 是提交点；提交后的 `latest` 或轮换故障只告警，不会重复生成快照。
- 默认日志不输出 Gamertag、XUID 或原始连接日志。
- BDS 必须由附带的 FIFO 包装器启动，才能安全接收管理命令。

`backup` 子命令创建的是 operator 快照，仍位于自动目录并参与 `verified` 轮换；升级、加模组等需要长期保留的人工里程碑必须放在自动目录之外。自动服务只拥有 `backups/automatic`，不会删除永久备份目录。

## 开发

项目只依赖 Python 3.10+ 标准库；集成运行时还需要 Linux、systemd、`journalctl` 和支持 `--link-dest`、`--chown` 的 rsync。

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q src tests
```

示例配置位于 `config/config.example.json`。部署前必须确认所有玩家离线，因为首次安装 FIFO 控制台包装器需要优雅重启一次 BDS。日常快照不重启服务器。

当前 `managed-v1` 集成有意固定使用 `/opt/minecraft-bedrock`、`minecraft-bedrock.service`、`/run/minecraft-bedrock` 和 `/opt/minecraft-bedrock/backups/automatic`。配置不匹配会被拒绝，避免出现“配置通过但 systemd 仍使用另一套路径”的假可配置状态。

## 校验与恢复演练

以下命令只接受仓库中的快照目录名，不接受任意路径：

```bash
sudo bedrock-activity-backup status
sudo bedrock-activity-backup verify SNAPSHOT_NAME
sudo bedrock-activity-backup restore-plan SNAPSHOT_NAME
sudo bedrock-activity-backup rehearse SNAPSHOT_NAME rehearsal-label
```

`verify` 会重算新 schema 中完整 payload 的 SHA-256 并显示快照状态。只有 `verified` 快照可用于 `restore-plan` 或 `rehearse`；旧 schema 仅保留只读 `verify` 兼容，不会成为链头、参与轮换、作为 `--link-dest` 或进入演练。自动验证会把 BDS 运行时复制到自动备份目录中的短暂隔离目录，替换其中世界后启动 BDS；该 service 使用独立网络命名空间，验证结束即删除该目录。它检测到 repair、corruption 或缺失 `.ldb` 日志即失败，而不会把“启动后自动修复”算作成功。

自动快照用于世界事故回退，并不是整台 VPS 的灾难恢复包。它包含目标世界和 BDS JSON 配置，但不包含 BDS 二进制、systemd、全局包目录或异地副本。正式恢复必须遵循 [恢复原则](docs/restore.md)。

## 安装边界

`scripts/install.sh` 默认只安装并校验文件，不重启正在运行的 BDS。只有显式传入 `--restart` 才会激活控制台包装器和监听器；安装、systemd 校验或首次启动任一步失败时，脚本会恢复原集成文件及启用状态。正式安装前仍应先创建手动里程碑备份并确认所有玩家离线。

```bash
sudo scripts/install.sh --config /path/to/config.json
sudo scripts/install.sh --config /path/to/config.json --restart
```
