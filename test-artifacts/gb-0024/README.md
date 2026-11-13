# gb-0024 候选逐一剔除（leave-one-out）诊断测试批次

本批次为“已完成权衡”新增只读的候选剔除诊断：沿用冻结测量与原权衡保存的同一批
权重情景（不重新抽样），逐份剔除候选后重新做 L2 标准化与 TOPSIS，统计剩余试样
两两之间的**严格逆序**与**并列变化**频率，二者互斥并分别计数；原权衡结果不改写。

## 运行方式

需要 numpy（生产镜像已固定 `numpy==2.1.3`）。本地无 Docker/MySQL 时用独立虚拟环境，
后端依赖以 SQLite + 假 Redis 替身运行，不触碰真实库与 `/artifacts`：

```bash
python3 -m venv --without-pip /tmp/venv
/tmp/venv/bin/python get-pip.py            # 离线环境可先下载 get-pip.py
/tmp/venv/bin/pip install numpy fastapi sqlalchemy redis PyJWT pymysql httpx

# 数值 oracle（批处理打分器与既有逐情景打分器逐元素一致性、手算反例、续跑、指纹）
PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_diagnosis.py

# 服务集成（SQLite：检查点、模拟中断、续跑只补缺失块、部分存储恢复、原权衡不改写）
PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_integration.py

# 端到端 API（FastAPI 路由 + JWT 权限 + 中间件，含 viewer 403、证据端点）
PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_api.py
```

## 三个测试文件覆盖的要点

| 文件 | 关键断言 |
|---|---|
| `test_diagnosis.py` | 批处理 `score_batch` 与 `topsis.score_normalized` 在 60 组随机矩阵（含零权重情景）下打分、名次、首位分摊**逐元素相等**；`average_ranks_batch` 对标量 `average_ranks`（200 组、含强制并列）；手算严格逆序与严格→并列反例；端到端管线记录中心权重并列变化；续跑文档与首次完全一致；指纹确定性 |
| `test_integration.py` | 每个剔除候选写一行子表检查点（O(n) 写入，progress 保持定长）；第 4 块模拟崩溃后 3 块已落库；续跑只重建 9/12 个缺失块；丢失某个 sidecar 文件时仅补该块且无重复检查点行；产物含完整反例、数据库只存精简块；原权衡状态/快照/指纹不变；重复执行已完成诊断为空操作 |
| `test_api.py` | 未登录 401、viewer 写入 403、缺任务 404、短名 422、重名 409；列表/详情/按 run 过滤；完成后严格逆序与并列变化分列、基线复算精确一致、精简块无反例摘要；`/evidence` 端点从产物读取完整反例并支持按剔除候选过滤 |

## 手算反例（确定性，不依赖随机抽样）

- 严格逆序：3 个 benefit 指标、权重 `(0.43089747, 0.33078418, 0.23831835)`，
  剔除第 4 行后第 1、3 行的严格顺序翻转（见 `test_diagnosis.py` 第 9 节）。
- 严格→并列：候选 `A=(8,2) B=(2,8) C=(9,5)`，等权 benefit；含 C 时 A、B 有严格
  次序，剔除 C 后两候选关于正/负理想点对称，贴近度完全相等（平均并列名次）。

## 存储与续跑设计（已知取舍）

- `loo_diagnoses` 存定长 progress 与小清单（元数据、聚合、每个剔除候选的**定长块头**，
  不含成对记录）；`loo_diagnosis_blocks` 每个剔除候选一行精简投影，插入一次，避免每步
  重写增长中的 JSON。
- 完整反例（情景编号、权重、剔除前后得分/名次）按剔除候选各写一个文件，持久保存在
  `/artifacts/diagnosis-<id>/blocks/<code>.json`，并以 `/artifacts/diagnosis-<id>/manifest.json`
  封存清单与块头；不合并为单文件，120 候选上限下：DB 清单约 1.7 MB、最大单行检查点约
  1.9 MB、最大块文件约 9.2 MB，均在 MySQL `max_allowed_packet`（默认 64 MB）内。
- 页面经 `GET /diagnoses/{id}/blocks/{code}` 读精简成对计数（无反例摘要），经
  `GET /diagnoses/{id}/evidence` 按需读块文件中的完整反例。
- 崩溃在“块文件已写、检查点行未提交”之间时，重跑只重建该块且不触发唯一约束冲突；块文件
  丢失但检查点行仍在时同理，仅补该块。
- 批处理打分器逐操作复刻既有标量实现（先按方向对真实值取 max/min 再选择，零权重全零列
  保持 0 而非对 inf 取极值；稳定范数对全零差分列显式置零）。
