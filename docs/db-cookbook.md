# 写查询前先看这张表

助手拿到的是**两个原语**（`panel_db_schema` / `panel_db_query`），不是一叠报表。
这份文档是那些「报表本该长什么样」的答案，写成可以抄的 SQL —— 因为下一句话通常
不在这份清单里，而 `SELECT` 能跟着问题走。

## 三条先要知道的事

1. **时间是定宽 UTC 字符串**：`2026-11-20T14:05:03Z`。字符串序即时间序，所以
   `ORDER BY created_at` 就是按时间排，`substr(created_at,1,13)` 就是「哪一小时」。
   不要用 `datetime()` 去转 —— 那要多此一举地把时区猜一遍。
2. **`tasks` 是账本，`sessions` 是人看到的东西**。一个会话由任务派生，所以
   「有多少任务」和「有多少会话」永远不是同一个数。
3. **`task.state` 的取值以 `panel_db_schema("tasks")` 的 `values:` 行为准** ——
   那里是从数据里读出来的，不是我在这里抄的。

---

## 分布与排行

**最近 7 天每天的任务量、失败量**

```sql
SELECT substr(created_at,1,10) AS day,
       count(*) AS n,
       sum(state = 'failed') AS failed,
       round(100.0 * sum(state = 'failed') / count(*), 1) AS failure_pct
  FROM tasks
 WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
 GROUP BY 1 ORDER BY 1 DESC;
```

**哪个 agent 最常失败**（只看有足够样本的，避免 1 次失败就 100%）

```sql
SELECT agent_name, count(*) AS n,
       sum(state = 'failed') AS failed,
       round(100.0 * sum(state = 'failed') / count(*), 1) AS failure_pct
  FROM tasks
 GROUP BY agent_id
 HAVING n >= 5
 ORDER BY failure_pct DESC, failed DESC
 LIMIT 10;
```

**回合时长：平均与最慢的（秒）**

```sql
SELECT agent_name, count(*) AS n,
       round(avg(julianday(updated_at) - julianday(created_at)) * 86400, 1) AS avg_s,
       round(max(julianday(updated_at) - julianday(created_at)) * 86400, 1) AS max_s
  FROM tasks
 WHERE state IN ('completed', 'failed')
 GROUP BY agent_id ORDER BY avg_s DESC LIMIT 10;
```

> P95 需要窗口函数；SQLite 3.25+ 支持。面板自带的 SQLite 够用：
>
> ```sql
> SELECT agent_name, avg(secs) AS avg_s FROM (
>   SELECT agent_name,
>          (julianday(updated_at) - julianday(created_at)) * 86400 AS secs,
>          row_number() OVER (PARTITION BY agent_id ORDER BY
>            (julianday(updated_at) - julianday(created_at))) AS rn,
>          count(*) OVER (PARTITION BY agent_id) AS total
>     FROM tasks WHERE state IN ('completed', 'failed')
> ) WHERE rn >= total * 0.95 GROUP BY 1;
> ```

## 卡住的东西

**working 但进度很久没动**（默认用 30 分钟做阈值）

```sql
SELECT id, agent_name, title,
       substr(progress_at, 1, 19) AS last_progress,
       round((julianday('now') - julianday(coalesce(progress_at, updated_at))) * 1440, 1) AS idle_min
  FROM tasks
 WHERE state IN ('submitted', 'working')
   AND julianday('now') - julianday(coalesce(progress_at, updated_at)) > 30/1440.0
 ORDER BY idle_min DESC;
```

**注册了但从没跑过任务的 agent**

```sql
SELECT a.id, a.host, a.status, a.first_seen_at
  FROM agents a
  LEFT JOIN tasks t ON t.agent_id = a.id
 WHERE t.id IS NULL
 ORDER BY a.first_seen_at DESC;
```

## 健康与卫生

**孤儿行**（父记录没了，子记录还在）

```sql
SELECT 'task_events→tasks' AS orphan_kind, count(*) AS n
  FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE t.id IS NULL
UNION ALL
SELECT 'session_messages→sessions', count(*)
  FROM session_messages m LEFT JOIN sessions s ON s.id = m.session_id WHERE s.id IS NULL
UNION ALL
SELECT 'tasks→agents', count(*)
  FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id WHERE a.id IS NULL;
```

**附件占用**

```sql
SELECT mime, count(*) AS n, sum(size) AS bytes,
       round(sum(size) / 1048576.0, 1) AS mb
  FROM session_attachments GROUP BY mime ORDER BY bytes DESC;
```

> `session_attachments.data` 是 BLOB，返回时会被换成 `<blob 12.3 KB>`。要总大小用
> `size` 列或 `sum(length(data))`，别 `SELECT *`。

**库本身对不对**

```sql
PRAGMA integrity_check;
PRAGMA user_version;   -- 与当前发布的 SCHEMA_VERSION 对比
```

## 会话视角

**最活跃的会话**

```sql
SELECT s.id, s.title, s.agent_name, s.message_count, s.state,
       substr(s.updated_at, 1, 19) AS updated
  FROM sessions s ORDER BY s.updated_at DESC LIMIT 20;
```

**一轮里 agent 说了多少字**（找出话特别多/特别少的）

```sql
SELECT session_id, count(*) AS messages,
       sum(length(text)) AS chars, round(avg(length(text)), 0) AS avg_chars
  FROM session_messages
 GROUP BY session_id ORDER BY chars DESC LIMIT 10;
```

## 注册面

**按来源 IP 看注册尝试，成功与失败**

```sql
SELECT ip, outcome, count(*) AS n, max(at) AS last_at
  FROM registrations GROUP BY ip, outcome ORDER BY n DESC LIMIT 20;
```

**卡片变了几次**（`changed_at` 非空 = 卡片内容变过）

```sql
SELECT id, host, changed_at, checked_at, failures
  FROM agents WHERE changed_at IS NOT NULL ORDER BY changed_at DESC;
```

---

## 写查询的三条纪律

1. **先 `panel_db_schema`，再写列名**。猜错的代价是拿到一条 hint 再重试一次，
   白烧一步；看一眼结构只要一步。
2. **聚合写在 SQL 里**，不要把整表拉回来自己数。`panel_db_query` 默认只返回 100 行，
   且会在结果里写 `matched N, showing M` —— 看到这两个数不等就说明该加 `WHERE` 或 `GROUP BY`。
3. **要回答的是问题，不是表**。用户问「哪个 agent 最差」，答案是一行结论加一个数字，
   不是一段 `SELECT` 结果。查询是手段。
