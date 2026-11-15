# cinevault_services/
> L2 | Parent: [../AGENTS.md](../AGENTS.md)

- `__init__.py`：服务包标记。
- `analysis.py`：对视频抽样，记录音画候选时刻。
- `audit.py`：追加变动事件，核对登记数量。
- `automation.py`：为新资料提交有界的本机分析，使用引擎版本记录结果。
- `cache.py`：保留登记预览，预算管理兼容副本。
- `compatibility.py`：解释播放失败与原片状态。
- `desktop_picker.py`：限时调用 Windows 原生文件夹选择器。
- `library_health.py`：按来源汇总预览与异常。
- `managed_vision.py`：校验权重并启动回环推理。
- `managed_vision_assets.py`：固定上游版本与哈希。
- `model_registry.py`：定义已登记分类引擎的状态。
- `model_tags.py`：索引模型标签，独立于手动标签。
- `models.py`：读取本机分类组件配置。
- `playback.py`：选择直接播放、换封装或转码。
- `providers.py`：验证模型请求与抽样授权，写入通用场景和普通动作标签。
- `trial_samples.py`：将固定样片标识映射到便携包内的文件。

`server.py` 为服务注入片目、文件和任务能力。

[PROTOCOL]: Update this header on change, then check AGENTS.md
