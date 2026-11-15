# 交接演练证据目录（handover-evidence）

本目录是 `docs/handover-runbook.md` 的实测支撑材料，全部在仓库外的临时目录产生，
**未修改任何 `reel_loom/` 生产代码**。

## 文件说明

- `drill-timeline.log`：完整演练时间线（热备份 → SIGKILL 中断 → 全量恢复 → 重试交付 → 部分复制反例 → SIGINT 正常关停对照）。
- `drill-results.json`：结构化结果，含恢复计数、重试/验规/下载/印本/血缘、部分复制失败体、关停退出码与票终态。
- `supplemental-probes.json`：同尺寸字节损坏下 reconcile 与 verify 的差异；实例锁拒绝第二进程。
- `reingest-remediation.json`：来源 blob 丢失后，用相同原始字节重新入库恢复、且不产生重复行。
- `layout-*.json`：各阶段数据目录布局、各表计数、票状态、blob 大小与 sha256、scratch 残留。
- `server-*.log`：各服务实例真实 HTTP 访问日志。
- `scripts/drill.py`：备份恢复演练驱动（经 HTTP 操作真实服务 + 文件级备份 + 信号注入）。
- `scripts/probe_integrity.py`：字节损坏检测与实例锁探测（进程内调用领域对象）。
- `scripts/probe_reingest.py`：重新入库补救探测。
- `scripts/slow-ffmpeg`：仅演练用的 FFmpeg 节流包装（真实渲染约 2 秒，包装后 sleep 12 秒以放大 running 观察窗口），生产环境不存在。

## 实测环境

- Python 3.11.2（linux/arm64）、PyAV 16.1.0、imageio-ffmpeg 0.6.0 自带 FFmpeg 7.0.2。
- 依赖装在独立目录（非仓库内），通过 `PYTHONPATH` 引入；仓库本身未安装/未改动。

## 复现步骤

脚本顶部写有本次环境的绝对路径常量（仓库路径、依赖路径、FFmpeg 路径、临时根目录 `/tmp/loom-drill`），
换环境时改这些常量即可。

```bash
# 依赖（av、imageio-ffmpeg）装在独立 target，例如 /tmp/loom-deps
export PYTHONPATH=/path/to/repo:/tmp/loom-deps:/tmp/loom-pylibs/lib/python3.11/site-packages
export LOOM_FFMPEG=$(python3 -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())")

# 仓库自带的有界恢复/验规工作流（不依赖外部媒体）
python3 -m reel_loom.demo --workflow recovery
python3 -m reel_loom.demo --workflow conformance

# 本目录三个探测/演练
python3 -I scripts/drill.py
python3 -I scripts/probe_integrity.py
python3 -I scripts/probe_reingest.py
```

`-I` 以隔离模式运行，避免当前目录的同名模块污染导入；所有数据写入系统临时目录，结束后可整体删除。
