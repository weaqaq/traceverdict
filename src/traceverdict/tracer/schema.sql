CREATE TABLE task (
  task_id        TEXT PRIMARY KEY,
  suite          TEXT NOT NULL,          -- 'self' | 'swebv'
  source         TEXT NOT NULL,          -- 'self' | 'swebench_verified'
  repo_ref       TEXT NOT NULL,          -- git bundle 路径或远端 URL
  base_commit    TEXT NOT NULL,
  image_ref      TEXT NOT NULL,          -- docker 镜像（含 tag 或 digest）
  instruction    TEXT NOT NULL,
  budget_json    TEXT NOT NULL,          -- {max_steps,max_tokens,max_wall_s,max_cost_usd}
  forbidden_json TEXT NOT NULL,          -- ["migrations/**", ...]
  gt_type        TEXT NOT NULL,          -- 'pytest' | 'swebench'
  gt_spec_json   TEXT NOT NULL,          -- pytest 选择器 / FAIL_TO_PASS 等
  tags_json      TEXT NOT NULL DEFAULT '[]',
  created_at     TEXT NOT NULL
);

CREATE TABLE config (
  config_id         TEXT PRIMARY KEY,
  agent_name        TEXT NOT NULL,       -- 'mini-swe-agent' | 'retrace-mini' | ...
  agent_version     TEXT NOT NULL,
  model_name        TEXT NOT NULL,
  model_params_json TEXT NOT NULL,       -- temperature 等
  prompt_version    TEXT NOT NULL,
  harness_version   TEXT NOT NULL,
  notes             TEXT
);

CREATE TABLE run (
  run_id          TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES task(task_id),
  config_id       TEXT NOT NULL REFERENCES config(config_id),
  repetition_idx  INTEGER NOT NULL DEFAULT 0,
  mode            TEXT NOT NULL,         -- 'scenario' | 'det_replay'
  status          TEXT NOT NULL,         -- 'ok'|'agent_error'|'harness_error'|'timeout'|'budget'
  exit_reason     TEXT,
  started_at      TEXT,
  finished_at     TEXT,
  wall_time_s     REAL,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  cost_usd        REAL,
  seed            INTEGER,
  env_fingerprint TEXT                   -- image digest + base_commit 校验串
);

CREATE TABLE event (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL REFERENCES run(run_id),
  step_idx     INTEGER NOT NULL,
  ts           TEXT NOT NULL,
  etype        TEXT NOT NULL,            -- 'llm_call'|'tool_call'|'fs_diff'|'note'|'error'|'final'
  payload_json TEXT NOT NULL,            -- llm_call 含 prompt_sha256 与可选全文（配置项控制）
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  latency_ms   INTEGER
);

CREATE TABLE artifact (
  artifact_id TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES run(run_id),
  kind        TEXT NOT NULL,             -- 'patch'|'fs_diff'|'log'|'report'
  path        TEXT NOT NULL,
  sha256      TEXT NOT NULL
);

CREATE TABLE verdict (
  verdict_id     TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES run(run_id),
  track          TEXT NOT NULL,          -- 'rule' | 'judge'
  name           TEXT NOT NULL,          -- 'fail_to_pass'|'pass_to_pass'|'forbidden'|'patch_valid'|'budget'
                                          -- judge: 'rubric_intent'|'rubric_shortcut'|'rubric_maintain'|'rubric_honesty'
  passed         INTEGER,                -- 规则轨 0/1；judge 轨 NULL
  score          REAL,                   -- judge 轨 0..2；规则轨 NULL
  detail_json    TEXT NOT NULL,
  judge_model    TEXT,
  rubric_version TEXT
);

CREATE TABLE comparison (
  comparison_id    TEXT PRIMARY KEY,
  baseline_config  TEXT NOT NULL,
  candidate_config TEXT NOT NULL,
  task_set_sha     TEXT NOT NULL,        -- 冻结任务清单文件的 sha256
  stats_json       TEXT NOT NULL,        -- Δpass、CI、p 值、成本/时延分位
  alarm            TEXT NOT NULL,        -- 'none'|'warn'|'hard'
  created_at       TEXT NOT NULL
);

CREATE TABLE injection (
  injection_id      TEXT PRIMARY KEY,    -- 'I1'..'I5'
  description       TEXT NOT NULL,
  config_patch_json TEXT NOT NULL        -- 对 config 的坏改动描述
);
