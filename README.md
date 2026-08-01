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
- 新快照通过结构校验后才原子发布并参与轮换。
- 失败或未完成目录不计入保留数量。
- 磁盘可用空间低于配置阈值时拒绝新快照。
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

## 安装边界

`scripts/install.sh` 默认只安装并校验文件，不重启正在运行的 BDS。只有显式传入 `--restart` 才会激活控制台包装器和监听器；安装、systemd 校验或首次启动任一步失败时，脚本会恢复原集成文件及启用状态。正式安装前仍应先创建手动里程碑备份并确认所有玩家离线。

```bash
sudo scripts/install.sh --config /path/to/config.json
sudo scripts/install.sh --config /path/to/config.json --restart
```
