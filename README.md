# Bedrock Activity Backup

为 Minecraft Bedrock Dedicated Server（BDS）提供由玩家活动触发的在线一致性增量快照。

## 行为

- 第一位玩家进入时开始 30 分钟倒计时。
- 有玩家在线时，每 30 分钟创建一次快照。
- 普通玩家退出但仍有人在线时，不额外备份。
- 最后一位玩家退出时立即创建最终快照，并停止周期备份。
- 下一位玩家再次进入时重新开始计时。
- 仅保留最近 4 份完整、已校验的自动快照。
- 手动备份放在自动快照目录之外，不参与轮换。

监听器直接读取 BDS 写入 systemd journal 的连接/断开事件，不定期轮询玩家。只有在断开事件、启动恢复和备份完成后才通过 BDS 控制台执行 `list`，确认实际在线人数。

## 一致性与空间

快照前使用 BDS 自带的 `save hold` / `save query`，确认世界文件处于可复制状态；复制结束后无论成功或失败都执行并确认 `save resume`。文件复制使用 `rsync --link-dest`，未变化的 LevelDB 文件通过硬链接复用，因此多个还原点不等于多份完整世界体积。

## 安全边界

- 单实例文件锁避免并发备份。
- 备份、维护和人工轮换统一使用同一把非阻塞锁。
- 新快照通过归属、结构和完整 payload 哈希校验后才原子发布并参与轮换。
- 失败或未完成目录不计入保留数量。
- 磁盘可用空间低于配置阈值时拒绝新快照。
- rsync 使用 `--fsync`，发布前同步新增文件及目录元数据。
- `rename` 是提交点；提交后的 `latest` 或轮换故障只告警，不会重复生成快照。
- 默认日志不输出 Gamertag、XUID 或原始连接日志。
- BDS 必须由附带的 FIFO 包装器启动，才能安全接收管理命令。

`backup` 子命令创建的是 operator 快照，仍位于自动目录并参与 4 份轮换；升级、加模组等需要长期保留的人工里程碑必须放在自动目录之外。

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

`verify` 会重算新 schema 中完整 payload 的 SHA-256；旧 schema 仅保留只读 `verify` 兼容，不会成为链头、参与轮换、作为 `--link-dest` 或进入演练。`restore-plan` 生成绑定快照 manifest 与当前世界 `level.dat` 的摘要计划；`rehearse` 会把 payload 完整复制到 root 独占的 `/var/lib/bedrock-activity-backup/rehearsals` 并再次校验，因此会临时占用接近一个世界的额外空间。

自动快照用于世界事故回退，并不是整台 VPS 的灾难恢复包。它包含目标世界和 BDS JSON 配置，但不包含 BDS 二进制、systemd、全局包目录或异地副本。正式恢复必须遵循 [恢复原则](docs/restore.md)。

## 安装边界

`scripts/install.sh` 默认只安装并校验文件，不重启正在运行的 BDS。只有显式传入 `--restart` 才会激活控制台包装器和监听器；安装、systemd 校验或首次启动任一步失败时，脚本会恢复原集成文件及启用状态。正式安装前仍应先创建手动里程碑备份并确认所有玩家离线。

```bash
sudo scripts/install.sh --config /path/to/config.json
sudo scripts/install.sh --config /path/to/config.json --restart
```
