# Recurring Task Scheduling Guide

This guide describes how to configure, automate, and monitor recurring batch tasks in **Reconciliator**. It details the available periodic tasks, prerequisites for non-interactive execution, and production setups across **systemd timers**, **crontab**, **Kubernetes CronJobs**, and **workflow orchestrators** (Airflow / Prefect).

---

## 1. Architecture & Execution Model

Reconciliator follows a **stateless, idempotent, CLI-first pipeline architecture**:

* **Database-Backed State**: Harvest progress and resumption tokens are persisted in PostgreSQL ([`harvest_state_table`](../src/reconciliator/database/schema.py#L354)). The pipeline does not require a long-running scheduler daemon process.
* **Clean Process Lifecycle**: Invoking tasks as discrete jobs allows heavy machine-learning runtimes (PyTorch, Hugging Face transformer models, USearch / FAISS vector engines) to release GPU memory and RAM cleanly upon completion.
* **Deterministic Non-Zero Exit Codes**: CLI commands raise non-zero exit codes on failure (`code=1`), ensuring external schedulers detect errors, report status, and trigger retries or alerts accurately.
* **Headless & Structured Logging**: Standard log formatting (`[timestamp] [level] [task_id=...] name: message`) is emitted directly to `stdout`/`stderr`. Rich terminal animations and ANSI escapes are automatically suppressed when standard output is not attached to a TTY.

---

## 2. Catalog of Recurring Tasks

| Task Name | CLI Command | Suggested Schedule | Purpose |
|---|---|---|---|
| **End-to-End Pipeline** | `reconciliator run -c config.yaml` | Nightly / Weekly | Executes all enabled stages sequentially (`ingest-vocab` → `ingest-oai` → `ingest-wikidata` → `build-index` → `reconcile`). |
| **Incremental OAI-PMH Harvest** | `reconciliator ingest-oai --all -c config.yaml` | Hourly / Daily | Pulls updated bibliographic/authority records from OAI-PMH and ResourceSync feeds using `from_date` checkpoints. |
| **Vocabulary Download** | `reconciliator download-skosmos -c config.yaml` | Weekly / Bi-weekly | Fetches updated SKOS/RDF controlled vocabularies (YSO, YKL, KANTO, SLM) from Skosmos APIs. |
| **Vocabulary Ingestion** | `reconciliator ingest-vocab -c config.yaml` | Weekly (after download) | Ingests downloaded SKOS vocabularies, concept hierarchies, mappings, and relator terms into the database. |
| **Wikidata Ingestion** | `reconciliator ingest-wikidata -c config.yaml` | Weekly / Monthly | Streams updated Wikidata entity dumps, extracting authority persons, works, and identifiers. |
| **Vector Index Rebuild** | `reconciliator build-index -c config.yaml` | Daily (after ingest) | Generates dense embeddings and updates FAISS / PyTorch ANN indices for newly harvested works and manifestations. |
| **Agent Vector Index Rebuild** | `reconciliator build-agent-index -c config.yaml` | Daily (after ingest) | Generates embeddings and updates the agent authority vector index. |
| **Work Consolidation** | `reconciliator consolidate-works -c config.yaml` | Daily (after harvest) | Unifies cross-source works using deterministic identifiers and graph equivalence rules into `work_consolidations`. |
| **Agent Clustering** | `reconciliator cluster-agents -c config.yaml` | Daily (after harvest) | Clusters raw agent authority records into canonical agent identity clusters with consensus frequencies. |
| **Corpus Reconciliation** | `reconciliator reconcile -c config.yaml` | Scheduled / On-demand | Clusters, matches, and enriches staged bibliographic records against authoritative clusters, emitting TSV reports. |
| **Telemetry & Task Pruning** | `reconciliator prune --completed --older-than 30d -y` | Weekly | Drops stale task progress records from `task_progress_table` to maintain database hygiene. |

---

## 3. Deployment Prerequisites & Best Practices

Before configuring automated scheduling, ensure the environment satisfies the following operational requirements:

### A. Prevent Overlapping Executions (Mutual Exclusion)
Reconciliator relies on the external scheduler to prevent concurrent runs of the same task. Running simultaneous harvests against the same source will cause races on OAI-PMH resumption tokens.
* **Kubernetes**: Specify `concurrencyPolicy: Forbid`.
* **Systemd**: Managed automatically (a running service will not be restarted by a timer until complete).
* **Crontab**: Wrap commands with `/usr/bin/flock`.

### B. Persistent Volume for Vector Indices and Caches
Vector indices (`work_vectors.faiss`, `work_vectors.pt`) and vocabulary files reside on disk (configured under `storage:` in `config.yaml`).
* In containerized environments (Kubernetes), the directory containing index files (`/data`) **must** be mounted from a **PersistentVolumeClaim (PVC)** or a shared storage volume.
* Transformer model weights download to `~/.cache/huggingface`. Set `HF_HOME=/data/hf_cache` on the persistent mount, or bake pretrained models directly into the container image to prevent re-downloading weights on every run.

### C. External PostgreSQL Configuration
In production, ensure `storage.postgres.auto_start_container` is set to `false` so Reconciliator connects to your managed database instead of attempting to start a local Podman container:
```yaml
storage:
  postgres:
    auto_start_container: false
    database_url: "postgresql+psycopg://user:password@postgres-host:5432/reconciliator"
```
You can also inject `RECONCILIATOR_DATABASE_URL` via environment variables.

### D. Absolute Paths
Always supply absolute paths for configuration files (`-c /opt/reconciliator/config.yaml`). Relative data paths in `config.yaml` are automatically resolved relative to the directory containing the YAML file.

---

## 4. Scheduling Solution 1: Systemd Timers (Linux VM / Bare Metal)

Systemd timers are the recommended scheduling solution on standalone Linux servers. Systemd prevents overlapping executions, manages process environments, logs to `journald`, and supports dependencies.

### A. Nightly End-to-End Pipeline

Create service file `/etc/systemd/system/reconciliator-nightly.service`:
```ini
[Unit]
Description=Reconciliator Nightly Ingestion and Reconciliation Pipeline
After=network.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=reconciliator
Group=reconciliator
WorkingDirectory=/opt/reconciliator
Environment="PATH=/opt/reconciliator/.venv/bin:/usr/local/bin:/usr/bin"
Environment="HF_HOME=/opt/reconciliator/data/hf_cache"
EnvironmentFile=-/etc/reconciliator/env
ExecStart=/opt/reconciliator/.venv/bin/reconciliator run -c /etc/reconciliator/config.yaml
TimeoutStopSec=300
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Create timer file `/etc/systemd/system/reconciliator-nightly.timer`:
```ini
[Unit]
Description=Trigger Reconciliator Nightly Pipeline at 02:00

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

### B. Hourly Incremental OAI Harvest

Create service file `/etc/systemd/system/reconciliator-harvest.service`:
```ini
[Unit]
Description=Reconciliator Incremental OAI-PMH Harvest
After=network.target

[Service]
Type=oneshot
User=reconciliator
Group=reconciliator
WorkingDirectory=/opt/reconciliator
Environment="PATH=/opt/reconciliator/.venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=-/etc/reconciliator/env
ExecStart=/opt/reconciliator/.venv/bin/reconciliator ingest-oai --all -c /etc/reconciliator/config.yaml
StandardOutput=journal
StandardError=journal
```

Create timer file `/etc/systemd/system/reconciliator-harvest.timer`:
```ini
[Unit]
Description=Run incremental harvest every hour

[Timer]
OnCalendar=*-*-* *:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

### C. Weekly Telemetry Prune

Create service file `/etc/systemd/system/reconciliator-prune.service`:
```ini
[Unit]
Description=Reconciliator Task Telemetry Prune

[Service]
Type=oneshot
User=reconciliator
Group=reconciliator
WorkingDirectory=/opt/reconciliator
Environment="PATH=/opt/reconciliator/.venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=-/etc/reconciliator/env
ExecStart=/opt/reconciliator/.venv/bin/reconciliator prune --completed --older-than 30d -y -c /etc/reconciliator/config.yaml
```

Create timer file `/etc/systemd/system/reconciliator-prune.timer`:
```ini
[Unit]
Description=Prune telemetry older than 30 days every Sunday at 04:00

[Timer]
OnCalendar=Sun *-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### Activation and Monitoring
```bash
# Reload daemon and enable timers
sudo systemctl daemon-reload
sudo systemctl enable --now reconciliator-nightly.timer reconciliator-harvest.timer reconciliator-prune.timer

# View active timers
systemctl list-timers 'reconciliator*'

# Inspect execution logs
journalctl -u reconciliator-nightly.service -e --no-pager
```

---

## 5. Scheduling Solution 2: Linux Crontab

When using traditional `crontab`, use `flock` to prevent concurrent executions and redirect logs cleanly:

```cron
# Edit with: crontab -u reconciliator -e
SHELL=/bin/bash
PATH=/opt/reconciliator/.venv/bin:/usr/local/bin:/usr/bin
HF_HOME=/opt/reconciliator/data/hf_cache

# 1. Hourly incremental harvest (at minute 15)
15 * * * * /usr/bin/flock -n /var/lock/reconciliator-harvest.lock reconciliator ingest-oai --all -c /etc/reconciliator/config.yaml >> /var/log/reconciliator/harvest.log 2>&1

# 2. Nightly end-to-end pipeline (at 02:00)
0 2 * * * /usr/bin/flock -n /var/lock/reconciliator-pipeline.lock reconciliator run -c /etc/reconciliator/config.yaml >> /var/log/reconciliator/pipeline.log 2>&1

# 3. Weekly Skosmos vocabulary update (Sunday at 01:00)
0 1 * * 0 /usr/bin/flock -n /var/lock/reconciliator-vocab.lock sh -c 'reconciliator download-skosmos -c /etc/reconciliator/config.yaml && reconciliator ingest-vocab -c /etc/reconciliator/config.yaml' >> /var/log/reconciliator/vocab.log 2>&1

# 4. Weekly telemetry prune (Sunday at 04:30)
30 4 * * 0 /usr/bin/flock -n /var/lock/reconciliator-prune.lock reconciliator prune --completed --older-than 30d -y -c /etc/reconciliator/config.yaml >> /var/log/reconciliator/prune.log 2>&1
```

---

## 6. Scheduling Solution 3: Kubernetes CronJobs

For cloud-native deployments, Kubernetes CronJobs run containerized tasks with persistent volume backing and secret injection.

### A. Kubernetes Secret & Persistent Volume Claim

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: reconciliator-secrets
  namespace: data-pipeline
type: Opaque
stringData:
  RECONCILIATOR_DATABASE_URL: "postgresql+psycopg://app_user:StrongPassword@postgres-service.data-pipeline.svc.cluster.local:5432/reconciliator"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: reconciliator-data-pvc
  namespace: data-pipeline
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Gi
```

### B. Daily Incremental Harvest CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: reconciliator-harvest
  namespace: data-pipeline
spec:
  schedule: "15 * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: harvest
            image: ghcr.io/org/reconciliator:latest
            command:
              - "reconciliator"
              - "ingest-oai"
              - "--all"
              - "-c"
              - "/etc/reconciliator/config.yaml"
            env:
              - name: RECONCILIATOR_DATABASE_URL
                valueFrom:
                  secretKeyRef:
                    name: reconciliator-secrets
                    key: RECONCILIATOR_DATABASE_URL
              - name: HF_HOME
                value: "/data/hf_cache"
            resources:
              requests:
                cpu: "2"
                memory: "4Gi"
              limits:
                cpu: "4"
                memory: "8Gi"
            volumeMounts:
              - name: config-volume
                mountPath: /etc/reconciliator
                readOnly: true
              - name: data-volume
                mountPath: /data
          volumes:
            - name: config-volume
              configMap:
                name: reconciliator-config
            - name: data-volume
              persistentVolumeClaim:
                claimName: reconciliator-data-pvc
```

### C. Nightly End-to-End Pipeline CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: reconciliator-nightly-run
  namespace: data-pipeline
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      activeDeadlineSeconds: 14400 # 4 hour timeout
      backoffLimit: 1
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: pipeline
            image: ghcr.io/org/reconciliator:latest
            command:
              - "reconciliator"
              - "run"
              - "-c"
              - "/etc/reconciliator/config.yaml"
            env:
              - name: RECONCILIATOR_DATABASE_URL
                valueFrom:
                  secretKeyRef:
                    name: reconciliator-secrets
                    key: RECONCILIATOR_DATABASE_URL
              - name: HF_HOME
                value: "/data/hf_cache"
            resources:
              requests:
                cpu: "4"
                memory: "16Gi"
              limits:
                cpu: "8"
                memory: "32Gi"
            volumeMounts:
              - name: config-volume
                mountPath: /etc/reconciliator
                readOnly: true
              - name: data-volume
                mountPath: /data
          volumes:
            - name: config-volume
              configMap:
                name: reconciliator-config
            - name: data-volume
              persistentVolumeClaim:
                claimName: reconciliator-data-pvc
```

---

## 7. Scheduling Solution 4: Workflow Orchestration (Apache Airflow)

If your architecture uses a centralized DAG orchestrator (such as Airflow or Prefect), you can model individual tasks as discrete nodes with explicit dependencies:

```python
from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

default_args = {
    "owner": "reconciliator",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="reconciliator_daily_pipeline",
    default_args=default_args,
    schedule_interval="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
) as dag:

    def create_step(task_name: str, subcommand: list[str]) -> KubernetesPodOperator:
        return KubernetesPodOperator(
            task_id=task_name,
            name=f"reconciliator-{task_name}",
            namespace="data-pipeline",
            image="ghcr.io/org/reconciliator:latest",
            cmds=["reconciliator"] + subcommand + ["-c", "/etc/reconciliator/config.yaml"],
            env_from=[{"secretRef": {"name": "reconciliator-secrets"}}],
            volume_mounts=[
                {"name": "data-volume", "mountPath": "/data", "readOnly": False},
                {"name": "config-volume", "mountPath": "/etc/reconciliator", "readOnly": True},
            ],
            volumes=[
                {"name": "data-volume", "persistentVolumeClaim": {"claimName": "reconciliator-data-pvc"}},
                {"name": "config-volume", "configMap": {"name": "reconciliator-config"}},
            ],
            get_logs=True,
        )

    # Individual task definition
    download_skos = create_step("download-skosmos", ["download-skosmos"])
    ingest_vocab = create_step("ingest-vocab", ["ingest-vocab"])
    harvest_oai = create_step("ingest-oai", ["ingest-oai", "--all"])
    build_index = create_step("build-index", ["build-index"])
    consolidate = create_step("consolidate-works", ["consolidate-works"])
    cluster_agents = create_step("cluster-agents", ["cluster-agents"])
    reconcile = create_step("reconcile", ["reconcile"])

    # DAG Dependency Topology
    download_skos >> ingest_vocab >> harvest_oai
    harvest_oai >> build_index >> consolidate >> cluster_agents >> reconcile
```

---

## 8. Monitoring & Operational Troubleshooting

### A. Checking Task Telemetry
Inspect task progress, records processed, and error rates using the CLI:
```bash
# View recent task statuses in terminal
reconciliator status

# Watch live progress of an ongoing task
reconciliator status watch --task-id oai-ingest-melinda-1726822800
```

Or query [`task_progress_table`](../src/reconciliator/database/schema.py#L323) directly in PostgreSQL:
```sql
SELECT
    task_id,
    task_type,
    status,
    current_stage,
    items_processed,
    error_count,
    rate_per_sec,
    start_time,
    last_heartbeat
FROM task_progress
ORDER BY last_heartbeat DESC
LIMIT 10;
```

### B. Investigating Scheduled Run Errors
1. **Sanitization and Foreign Key Errors**:
   - Check the `error_count` column in `task_progress`.
   - Truncated values and quarantined foreign keys are logged with `[sanitization]` or `[validation]` prefixes in standard logging output.
2. **Resuming an Interrupted Harvest**:
   - If an OAI harvest job was killed by a timeout or OOM killer, the next invocation automatically resumes using the stored token in `harvest_state`.
   - To force a full re-harvest from the beginning, pass `--restart`:
     ```bash
     reconciliator ingest-oai --source melinda --restart -c config.yaml
     ```
3. **Stale Lock Cleanup**:
   - If using `flock`, locks in `/var/lock` are automatically released by the Linux kernel when the process terminates. No manual lock cleanup is required.

