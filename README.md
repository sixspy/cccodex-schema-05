# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 半截换版恢复链（换版跑到中途抛错）

换版拆成两阶段提交：**契约行先落 → 目录行再落**。目录行失败时保留已提交的契约行，
登记一条**残段**（契约行已升现行、目录行停在旧版），不再整体回滚。

**半截提交只许择一取数**（`POST /api/residuals/{id}/mode`，混挂返回 409）：

| 取数 | 语义 | 提交瞬间 | 是否占库存 |
|------|------|----------|-----------|
| 清掉 | 当场把已写契约行退回草稿，旧现行复归 | 打开即生效 | 否 |
| 快照 | 先把半截冻结成快照，再挂补目录任务 | 冻结点即生效 | 是 |

拍板取数后再点**续写**（`POST /api/residuals/{id}/resume`），后端按已选取数一次落成；
出错回中文白话。续写前会比对半截行与契约态，**不同源即停机**（`status=已停`）。

- 并发：续写先抢残段锁（`lock_token`），锁后重读终态，两侧同时点续写只有一个落成，另一个 409。
- 复验：缺口与补齐各复验一轮（`POST /api/residuals/{id}/verify`），结论写 `orchestration_log`。
- 四处同标记：残段列表 / 换版附页 / 契约详情 / 对账卡片，标记统一为 `残段#<id>·<状态>`。
- 占库存计数分两层：`occupies_total` 是半截行仍为现行的全部残段（快照收口后仍计入），
  `pending_occupies` 只算其中未收口的，也就是当下还在压着的库存。

接口一览：

```
GET  /api/residuals                 残段列表（含占库存/未收口计数）
GET  /api/residuals/{id}            残段详情（快照 / 补目录任务 / 编排记录）
POST /api/residuals/{id}/mode       择一取数 {"mode":"清掉"|"快照"}
POST /api/residuals/{id}/resume     续写落成
POST /api/residuals/{id}/verify     缺口 + 补齐各复验一轮
GET  /api/orchestration-log         编排记录（全量，倒序）
GET  /api/contracts/{id}            契约详情（含 residual_marker）
POST /api/contracts/{id}/apply      换版；{"fault":"after_contract"} 演练半截
```

预留切口（未做，供后续功能题）：双写迁移 vs 锁表切流、跨表依赖冻结、回滚窗口等。

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
