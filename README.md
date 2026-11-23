# 循环泵累计故障率核算台（Flask + SQLite）

面向维护班组的网页工具：上传每泵一行的台账 CSV，按累计运行小时计算
**故障累计发生率（Aalen–Johansen 竞争风险累积发生率，CIF）**，支持任意
小时查询、单泵记录更正；台账与汇总在同一 SQLite 事务内生效。

## 1. 计算口径（与需求逐条对应）

每条记录：泵号、入册累计运行小时、出册累计运行小时、结束方式
（`failure` 故障 / `preventive` 预防更换 / `censored` 观察结束）。

对每个不同的运行小时时点 t，严格按以下顺序处理：

1. **先计入新入册的泵**（延迟进入风险集）；
2. 用**当时在册数 n(t)** 计算两类事件的风险率：
   - 故障风险率 `h_f(t) = d_f(t) / n(t)`
   - 预防更换风险率 `h_p(t) = d_p(t) / n(t)`
3. **移除所有出册泵**（故障、预防更换、观察结束都退出风险集）。

累计故障率按**此前未发生两类事件的概率 S(t⁻) 加权累加**（预防更换为
竞争事件，观察结束只退出风险集、不带事件概率）：

```
S(t)   = Π_{u≤t} (1 − d_f(u)/n(u) − d_p(u)/n(u))
F_f(t) = Σ_{u≤t} S(u⁻) · d_f(u)/n(u)      # 累计故障发生率
F_p(t) = Σ_{u≤t} S(u⁻) · d_p(u)/n(u)      # 累计预防更换率
恒等式：S(t) + F_f(t) + F_p(t) = 1
```

任意小时查询取不晚于该小时的最近时点值（阶梯函数），并返回该时点的
逐时点计数（入册、在册 n、故障 d_f、预防 d_p、观察结束 d_c 等）。

## 2. 数据与事务保证

SQLite 三张表（见 `app/db.py`）：

| 表 | 内容 |
|---|---|
| `ledger` | 泵台账，泵号主键，含入/出册小时、结束方式、更新时间 |
| `life_table` | 汇总：每个时点一行，逐时点计数、风险率、S、F_f、F_p |
| `meta` | 泵总数、汇总更新时间、最近数据来源 |

- 上传 CSV = 整体替换台账；单泵更正 = upsert 一行。两条写路径都在
  **一个事务**内完成「写台账 → 重算并替换汇总」，任何一步失败即
  `ROLLBACK`，**保留原有台账与汇总**（`app/services.py`）。
- 单泵更正只需提交该泵数据，服务端自行从库内加载全量台账重算，
  **无须读取/回传其他泵明细**。
- 查询接口（`/query`、`/api/query`、`/api/lifetable`）只读 `life_table`
  与 `meta`，不执行任何写操作。

## 3. 目录结构

```
app/
  __init__.py     Flask 路由（上传、台账、更正、查询、JSON API）
  lifetable.py    生命表/CIF 纯计算与校验
  csvio.py        CSV 解析（中英文表头、三种结束方式别名、逐行报错）
  db.py           SQLite 连接与建表
  services.py     事务服务层（替换台账、单泵 upsert、只读查询）
  templates/      4 个 Jinja 页面
  static/style.css
tests/test_app.py 20 个 unittest 用例（数学、边界、CSV、事务回滚、网页、只读性）
sample/pumps_sample.csv
Dockerfile, docker-compose.yml, requirements.txt, wsgi.py
```

## 4. 用 Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 数据库在命名卷 `pump_data` 内（容器路径 `/app/instance/pumps.db`），
  重建容器数据不丢；健康检查 `GET /health`。
- 生产环境请通过环境变量 `FLASK_SECRET_KEY` 改密钥。

停止：`docker compose down`（保留数据卷）；连数据一起清除：
`docker compose down -v`。

## 5. 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py                      # 或 gunicorn wsgi:app
```

## 6. 网页试用流程

1. 首页「上传 CSV」选择 `sample/pumps_sample.csv`（或同结构文件，
   表头支持 `泵号/入册累计运行小时/出册累计运行小时/结束方式`，
   结束方式填 `故障/预防更换/观察结束`，也接受英文 failure/preventive/censored；
   编码支持 UTF-8 与 GBK）。
2. 首页查看逐时点计算明细表。
3. 「故障率查询」输入任意小时（如 1500、2000），得到累计故障率、
   累计预防率、无事件概率及该结果使用的逐时点计数。
4. 「台账」页可新增泵或对单台泵做更正，保存后汇总立即同步重算。

JSON 接口：

```bash
curl 'http://localhost:8000/api/query?hours=2000'
curl 'http://localhost:8000/api/lifetable'
```

## 7. 自测

```bash
python -I -m unittest discover -s tests -v     # 20 个用例应全部 OK
```

## 8. 示例数据的手算基准（sample CSV，6 台泵）

| t | 入册 | n(t) | d_f | d_p | d_c | S(t) | F_f(t) | F_p(t) |
|---|---|---|---|---|---|---|---|---|
| 0 | 4 | 4 | 0 | 0 | 0 | 1.0000 | 0.0000 | 0.0000 |
| 500 | 1 | 5 | 0 | 0 | 0 | 1.0000 | 0.0000 | 0.0000 |
| 1000 | 1 | 6 | 1 | 1 | 0 | 0.6667 | 0.1667 | 0.1667 |
| 2000 | 0 | 4 | 1 | 0 | 1 | 0.5000 | 0.3333 | 0.1667 |
| 3000 | 0 | 2 | 0 | 1 | 1 | 0.2500 | 0.3333 | 0.4167 |

末时点 `S + F_f + F_p = 0.25 + 0.3333 + 0.4167 = 1`。
