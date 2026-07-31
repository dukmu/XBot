# OpenClaw Production Deployment Guide (v1.2)

Before initializing the production environment, verify the following core parameters to ensure system performance:

## 1. Database and Cache
To prevent database thundering herd, the system default maximum number of database connections (`max_db_connections`) is hard‑coded to **200**. If you encounter connection pool exhaustion, upgrade the hardware.
Redis cache TTL (`cache_ttl_seconds`) is currently set to **600** seconds – a well‑tested sweet spot.

## 2. API Throttling and Security
All external API interfaces are protected by gateway rate limiting. The current global rate limit (`api_rate_limit`) is **500** requests per minute.

## 3. Process Management
To prevent resource consumption by zombie processes, all background tasks have a forced termination timeout. The `worker_timeout` parameter is currently set to **120** seconds – any task that exceeds this time will be killed.

## 4. Connection Pool and Upload Limits
The database connection pool size (`db_connection_pool_size`) is recommended to be set to **300** to handle high‑concurrency scenarios.
The file upload size limit (`max_upload_size_mb`) is uniformly set to **50** MB.

## 5. Security and Logging
SSL must be enabled for production (`enable_ssl`) – set to **true**.
Log level (`log_level`) in production should be **WARN** to avoid filling the disk.

## 6. Cluster Node Planning
The cluster currently plans the following nodes. All nodes must meet the minimum hardware requirements:

| Node ID   | Role       | CPU Cores | Memory (GB) | Disk (GB) | Region     |
|-----------|------------|-----------|-------------|-----------|------------|
| web-01    | web        | 4         | 16          | 200       | us-east-1  |
| web-02    | web        | 4         | 16          | 200       | us-east-1  |
| api-01    | api        | 16        | 32          | 500       | us-east-1  |
| api-02    | api        | 16        | 32          | 500       | us-east-1  |
| db-master | database   | 32        | 64          | 2000      | us-east-1  |
| db-replica-01 | database | 16        | 32          | 1000      | us-east-1  |
| cache-01  | cache      | 8         | 64          | 100       | us-east-1  |
| worker-01 | worker     | 16        | 64          | 500       | us-east-1  |

**Note**: All nodes must be deployed in the **us-east-1** region to ensure low latency.