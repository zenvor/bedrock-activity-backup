# 恢复工作流

自动快照位于 `backup_root/snapshots/<时间-标识>/payload`。任何正式恢复都必须在玩家全部离线、BDS 优雅停止且进程完全退出后进行。禁止覆盖运行中的 LevelDB，也禁止只复制单个 `.ldb` 文件拼接世界。

## 1. 只读校验

先查看状态并对目标快照重新计算 SHA-256。新 schema 会覆盖完整 payload；旧 schema 只能执行只读 `verify`，不能直接生成恢复计划或进入演练，需先另行保留并转制为当前 schema：

```bash
sudo bedrock-activity-backup status
sudo bedrock-activity-backup verify SNAPSHOT_NAME
sudo bedrock-activity-backup restore-plan SNAPSHOT_NAME
```

`restore-plan` 输出的 `plan_sha256` 同时绑定快照 manifest、当前世界 `level.dat` 指纹和恢复安全门。若正式操作前任一指纹变化，必须重新生成并审阅计划。

## 2. 隔离恢复演练

确认至少还有一个世界大小的可用空间，再执行：

```bash
sudo bedrock-activity-backup rehearse SNAPSHOT_NAME before-real-restore
```

程序会在 root 独占的 `/var/lib/bedrock-activity-backup/rehearsals/before-real-restore` 创建全新目录，复制 payload，逐项比较源快照校验值，并验证 `db/CURRENT` 指向的 MANIFEST。目标目录已存在时命令会拒绝执行，失败时只清理本次新建的演练目录。

## 3. 正式离线恢复门槛

只有演练通过后才进入正式恢复：

1. 再次确认在线人数为 0。
2. 优雅停止 `minecraft-bedrock.service`，确认 service inactive 且没有 `bedrock_server` 进程。
3. 对当前正式世界创建一份新的离线里程碑副本，验证其 `level.dat`、`db/CURRENT` 和 MANIFEST。
4. 将目标快照的世界复制到正式世界旁边的全新暂存目录；不要直接写入现有世界。
5. 对暂存目录重新计算 restore plan 中的关键哈希，并检查世界名、包绑定和 `minecraft:minecraft` 所有权。
6. 将当前世界改名为带时间戳的 rollback 目录，再把暂存世界原子改名为正式世界。
7. 启动 BDS，检查 `Server started`、Level Name、Opening level、完整 Pack Stack、UDP 19132/19133 和错误日志。
8. 由玩家实机核验背包、箱子、等级、位置、模组进度和被恢复的建筑或物品。

不要在实机验收前删除 rollback 世界、离线里程碑或演练报告。

## 4. 恢复范围

自动 payload 包含目标世界、`server.properties`、`allowlist.json`、`permissions.json` 和 `valid_known_packs.json`。世界恢复通常只替换世界目录；是否恢复 JSON 配置必须在计划中单独决定。

它不包含 BDS 二进制、systemd unit、全局 `behavior_packs`/`resource_packs`、代理服务或云主机配置。VPS、磁盘或整个安装目录损坏时，必须依赖云盘快照、异地副本和版本化部署文件。
