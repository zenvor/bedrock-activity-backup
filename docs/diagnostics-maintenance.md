# 诊断空间维护

`runs/failed/<snapshot>/` 只保存失败候选的诊断材料，不能作为恢复点。它不受 `max_failed_snapshots` 的完整失败样本保留数量控制。连续失败、同轮立即重试和后续定时重试都会产生新诊断；运行审计 JSON 也会持续增加。程序不自动删除这些历史证据，维护是部署后的必要运维工作。

至少每周检查以下目录；失败持续时每天检查。`runs/failed/` 达到 256 MiB，或可用空间接近最近运行记录中的 `space_budget.required_free_bytes`，应立即审查原因和空间。不要只看服务是否 active，也不要仅把 `min_free_bytes` 当作足够启动备份的总空间。

```sh
sudo du -sh /opt/minecraft-bedrock/backups/automatic/runs/failed /opt/minecraft-bedrock/backups/automatic/runs
df -h /opt/minecraft-bedrock/backups/automatic
```

该目录尚未创建时，`du` 对它报告不存在是正常的。建议保留最近 7 天诊断和最新失败记录；未审查事故的材料继续保留。空间告警发生时，即使材料不足 7 天也应立即介入调查、扩容或经明确审查后处理，不能等待定期清理来解决当前不足。

清理前应阅读选定记录的 manifest、私有 validation.log 与相关运行 JSON，确认不再需要用于调查；需要长期保留的材料应先存到经授权且空间足够的归档位置。私有日志可能包含世界、包和路径信息，不要直接粘贴到公开讨论。不要清理 `snapshots/` 中的失败样本、pending 或可靠快照来代替诊断维护，也不要涉及手动备份。

以下为人工清理命令模板，默认不删除任何内容。只有管理员明确选定后，才把目录名逐项填入 `names`。它使用与备份程序相同的仓库锁，只处理超过 7 天、带本工具归属且标为 failed 的独立诊断目录，拒绝含 payload、额外文件或子目录的目标，并保留最新诊断。锁忙时应稍后再执行，不能绕过锁。

```sh
sudo python3 - <<'PY'
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/bedrock-activity-backup/current/src")
from bedrock_activity_backup.config import Config
from bedrock_activity_backup.snapshot import repository_lock

names = []
if not names:
    raise SystemExit("未指定诊断目录，未删除任何内容")
config = Config.load(Path("/etc/bedrock-activity-backup/config.json"))
with repository_lock(config):
    root = config.backup_root / "runs" / "failed"
    if root.is_symlink():
        raise SystemExit("诊断根目录是符号链接，拒绝处理")
    directories = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    newest = max(directories, key=lambda p: p.stat().st_mtime)
    selected = []
    allowed = {"manifest.json", ".owner.json", "validation.log", "rsync-stats.txt"}
    for name in names:
        if not re.fullmatch(r"\d{8}T\d{6}Z-[a-f0-9]{8}", name):
            raise SystemExit("诊断名称无效")
        path = root / name
        if path.is_symlink() or not path.is_dir() or path == newest:
            raise SystemExit("目标不是可清理的旧诊断目录")
        if time.time() - path.stat().st_mtime < 7 * 86400:
            raise SystemExit("目标不足 7 天")
        if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in path.iterdir()):
            raise SystemExit("目标包含非诊断内容")
        owner = json.loads((path / ".owner.json").read_text())
        manifest = json.loads((path / "manifest.json").read_text())
        if owner.get("tool") != "bedrock-activity-backup" or manifest.get("snapshot_state") != "failed":
            raise SystemExit("目标归属或失败状态不符")
        selected.append(path)
    for path in selected:
        shutil.rmtree(path)
        print("已删除诊断目录", path.name)
PY
```

清理不改变任何快照状态、latest 指针或配置。再次查看空间和备份状态，确认后续重试能通过完整空间预算；若仍不足，应处理实际空间来源或扩容。
