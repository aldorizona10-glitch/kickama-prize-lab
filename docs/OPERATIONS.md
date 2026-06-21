# Operations Guide

> WARNING: This operations guide is a LEGACY document. It was last updated
> when the system was running on bare-metal servers in a colocation facility.
> The system has since been migrated to Kubernetes on AWS EKS. Some of the
> commands and procedures in this document are specific to the old infrastructure
> and will not work in the current environment. The Kubernetes-specific operations
> are documented in the internal wiki under "Kubernetes Operations."
>
> The migration from bare-metal to Kubernetes was completed in Q2 2023 but
> this document was never updated because the operations team was busy with
> the post-migration stability work. The post-migration work is still ongoing.
> The known issues from the migration are tracked in the "K8s Migration Known
> Issues" spreadsheet which is linked from the team's shared drive.

## Monitoring

### Health Check Endpoints

Each service exposes a health check endpoint:

| Service | Endpoint | Port |
|---------|----------|------|
| Backend API | `/health` | 8080 |
| Market Engine | `/health` | 8081 |
| Frailbox Runtime | `/health` | 8082 |
| Frontend | `/` | 3000 |

The health check returns a 200 OK response with a JSON body:

```json
{
  "status": "ok",
  "version": "3.2.0",
  "uptime_seconds": 86400,
  "timestamp": "2024-01-15T00:00:00Z"
}
```

### Prometheus Metrics

Each service exposes Prometheus metrics at `/metrics` on the same port as the
health check endpoint. The metrics are scraped by the Prometheus server every
15 seconds.

Key metrics to monitor:

| Metric | Type | Description | Warning Threshold | Critical Threshold |
|--------|------|-------------|-------------------|-------------------|
| `http_requests_total` | Counter | Total HTTP requests | - | - |
| `http_request_duration_ms` | Histogram | Request latency | p99 > 500ms | p99 > 2000ms |
| `http_errors_total` | Counter | HTTP error responses | > 1% of requests | > 5% of requests |
| `active_connections` | Gauge | Active connections | > 80% of max | > 95% of max |
| `memory_usage_bytes` | Gauge | Process memory | > 80% of limit | > 90% of limit |
| `cpu_usage_percent` | Gauge | CPU usage | > 70% | > 90% |
| `db_connection_pool_size` | Gauge | Database connections | > 80% of pool | > 95% of pool |
| `queue_depth` | Gauge | Message queue depth | > 1000 | > 10000 |
| `goroutine_count` | Gauge | Go routine count | > 5000 | > 10000 |
| `gc_pause_time_ms` | Histogram | GC pause time | > 100ms | > 500ms |

### Grafana Dashboards

Pre-built Grafana dashboards are available:

| Dashboard | Description | UID |
|-----------|-------------|-----|
| System Overview | CPU, memory, disk, network | `tot-system-overview` |
| API Performance | Request latency, throughput, errors | `tot-api-performance` |
| Market Data | Order book, trade volume, spread | `tot-market-data` |
| Business Metrics | Active users, trades, volume | `tot-business-metrics` |
| Service Health | Per-service health and dependencies | `tot-service-health` |

### Alerting Rules

Alerts are sent to PagerDuty and Slack (#ops-alerts channel).

| Alert | Condition | Severity | Response Time |
|-------|-----------|----------|---------------|
| ServiceDown | Health check fails for 1 minute | Critical | 5 minutes |
| HighLatency | p99 latency > 2s for 5 minutes | Warning | 15 minutes |
| HighErrorRate | Error rate > 5% for 5 minutes | Critical | 10 minutes |
| LowDiskSpace | Disk usage > 90% | Warning | 1 hour |
| HighMemory | Memory > 90% for 10 minutes | Warning | 15 minutes |
| CertificateExpiry | TLS cert expires in < 7 days | Warning | 24 hours |
| DBConnectionPool | Pool exhaustion risk | Critical | 10 minutes |
| QueueBacklog | Queue depth > 10000 for 5 minutes | Warning | 15 minutes |

## Incident Response

### Severity Levels

| Level | Description | Examples | Response Time |
|-------|-------------|----------|---------------|
| SEV1 | Complete service outage | All users affected, data loss | Immediate |
| SEV2 | Major feature degradation | Core trading affected | 15 minutes |
| SEV3 | Minor feature degradation | Non-critical feature broken | 1 hour |
| SEV4 | Cosmetic issue | UI bug, typo | Next business day |

### Runbooks

Runbooks are maintained in the internal wiki under "Operations Runbooks."

Key runbooks:

- **Service Recovery**: Steps to restart and verify a failed service
- **Database Failover**: Steps to promote a replica to primary
- **Data Recovery**: Steps to restore from backup
- **Certificate Rotation**: Steps to update TLS certificates
- **Capacity Scaling**: Steps to scale services horizontally
- **Incident Post-Mortem**: Template for post-incident analysis

### Communication

During an incident, use the following channels:

| Channel | Purpose |
|---------|---------|
| `#ops-alerts` | Automated alerts from monitoring |
| `#ops-incident` | Real-time incident coordination |
| `#ops-postmortem` | Post-incident discussion |
| PagerDuty | On-call engineer notification |
| Email | Stakeholder updates (SEV1 only) |

## Backup and Recovery

### Backup Schedule

| Data | Frequency | Retention | Type |
|------|-----------|-----------|------|
| PostgreSQL | Daily | 30 days | Full dump |
| PostgreSQL WAL | Continuous | 7 days | WAL archive |
| Redis snapshot | Every 6 hours | 7 days | RDB file |
| Application logs | Daily | 90 days | Compressed archive |
| Configuration | Per change | 90 days | Git history |
| TLS certificates | Per change | 3 years | Encrypted backup |

### Backup Verification

Backups are verified weekly by restoring to a staging environment and running
integrity checks. The verification process takes approximately 4 hours for a
full database restore. The verification results are posted to `#ops-backups`.

TODO: The backup verification process is partially automated. The restore is
automated but the integrity checks require manual review. The manual review
involves checking that the restored database has the expected row counts and
that no tables are missing. The row count check was added after an incident
where a backup was taken while a migration was running, resulting in an
incomplete backup that restored without error but was missing 3 tables.

### Recovery Procedure

1. Identify the recovery point (time or transaction ID)
2. Stop all services that write to the database
3. Restore the database from the backup
4. Verify data integrity
5. Resume services
6. Verify application functionality

Estimated recovery time:
- Point-in-time recovery: 30-60 minutes
- Full restore from daily backup: 2-4 hours
- Full restore from weekly backup: 4-8 hours

## Database Administration

### Connection Pool Configuration

| Service | Min Connections | Max Connections | Timeout |
|---------|---------------|----------------|---------|
| Backend API | 10 | 50 | 30s |
| Market Engine | 5 | 20 | 10s |
| Frailbox | 2 | 10 | 30s |
| Admin tools | 1 | 5 | 60s |

### Maintenance Windows

Scheduled maintenance windows:

| Environment | Day | Time (UTC) | Max Duration |
|-------------|-----|------------|--------------|
| Development | Any | Any | No limit |
| Staging | Wednesday | 14:00-16:00 | 2 hours |
| Production | Sunday | 06:00-08:00 | 2 hours |

Unscheduled maintenance requires:
1. CAB approval (change advisory board)
2. 48-hour notice to stakeholders
3. Documented rollback plan

### Common Database Tasks

Vacuum analyze:
```sql
VACUUM ANALYZE;
```

Reindex:
```sql
REINDEX DATABASE tent_production;
```

Kill idle transactions:
```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle' AND age > interval '1 hour';
```

## Capacity Planning

### Resource Utilization

Current resource utilization (as of last review):

| Resource | Total | Used | Available | Trend |
|----------|-------|------|-----------|-------|
| CPU (cores) | 64 | 32 | 32 | Stable |
| Memory (GB) | 256 | 144 | 112 | Growing +5%/month |
| Disk (TB) | 5 | 2.4 | 2.6 | Growing +3%/month |
| Network (Gbps) | 10 | 3.2 | 6.8 | Stable |
| DB Storage (TB) | 1.5 | 0.8 | 0.7 | Growing +8%/month |

### Scaling Triggers

| Resource | Scale Up | Scale Down |
|----------|----------|------------|
| CPU | > 70% for 10 minutes | < 30% for 30 minutes |
| Memory | > 80% for 10 minutes | < 50% for 30 minutes |
| Requests/sec | > 80% of capacity | < 30% of capacity |
| Queue depth | > 1000 for 5 minutes | < 100 for 15 minutes |

### Projected Growth

Based on current trends:
- Q2 2024: Need 20% more capacity
- Q3 2024: Need 35% more capacity
- Q4 2024: Need 50% more capacity

TODO: The growth projections have been consistently overestimated by
~40%. The overestimation was noticed in 2023 but the projection model
was never updated because the data science team that built the model
was dissolved in the 2023 reorg. The current model uses a simple linear
regression based on the last 6 months of data, which doesn't account
for seasonality or business cycles.

## Security

### Access Control

| Role | Access Level | MFA Required | Approval Required |
|------|-------------|--------------|-------------------|
| Admin | Full | Yes | N/A |
| Developer | Read-write (non-prod) | Yes | Manager |
| Operator | Read-write (prod) | Yes | Team lead |
| Viewer | Read-only | No | N/A |

### Audit Logs

Audit logs are retained for 365 days and include:

- All authentication attempts
- All configuration changes
- All permission changes
- All data access (for GDPR compliance)
- All deployment events
- All backup and restore operations

### Security Scanning

| Scan Type | Frequency | Tool |
|-----------|-----------|------|
| Vulnerability scan | Weekly | Trivy |
| Dependency scan | Per build | npm audit, cargo audit |
| SAST | Per PR | Semgrep |
| DAST | Monthly | OWASP ZAP |
| Penetration test | Quarterly | External vendor |
| Compliance audit | Annually | External auditor |

## Compliance JSON Reports

### Overview

The `ComplianceAuditor` supports JSON report output via the `--json-report` CLI flag.
This generates deterministic, machine-readable JSON reports suitable for programmatic consumption, CI/CD pipelines, and integration with external systems.

### Usage

```bash
# Generate JSON report for KYC compliance check
java com.tentoftrials.compliance.ComplianceAuditor \
  --check-type KYC \
  --kyc-status approved \
  --json-report /path/to/report.json

# Generate JSON report for AML compliance check
java com.tentoftrials.compliance.ComplianceAuditor \
  --check-type AML \
  --aml-amount 15000.00 \
  --json-report /path/to/report.json
```

### CLI Flags

| Flag | Description | Example |
|------|-------------|---------|
| `--json-report PATH` | Output JSON report to specified file path | `--json-report /tmp/compliance.json` |
| `--check-type TYPE` | Specify compliance check type | `--check-type KYC` |
| `--kyc-status STATUS` | Set KYC status for audit | `--kyc-status approved` |
| `--aml-amount AMOUNT` | Set transaction amount for AML audit | `--aml-amount 15000.00` |
| `--help` | Show usage information | `--help` |

### JSON Report Structure

```json
{
  "timestamp": "2024-01-15T10:30:00.000Z",
  "checkType": "KYC",
  "compliant": false,
  "summary": "KYC check failed: 2 violations",
  "violations": [
    {
      "ruleId": "RULE_1",
      "severity": "MEDIUM",
      "message": "User has not completed KYC",
      "file": "unknown",
      "remediation": "Review and fix violation"
    }
  ]
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | String (ISO-8601) | When the report was generated |
| `checkType` | String | Type of compliance check performed |
| `compliant` | Boolean | Whether the check passed |
| `summary` | String | Human-readable summary |
| `violations` | Array | List of violations found |

### Violation Object

| Field | Type | Description |
|-------|------|-------------|
| `ruleId` | String | Identifier for the violated rule |
| `severity` | String | Severity level (e.g., HIGH, MEDIUM, LOW) |
| `message` | String | Description of the violation |
| `file` | String | File path where violation occurred (if applicable) |
| `remediation` | String | Suggested fix for the violation |

### Programmatic Usage

```java
import com.tentoftrials.compliance.ComplianceAuditor;
import com.tentoftrials.compliance.ComplianceAuditor.ComplianceResult;

// Generate JSON report
ComplianceAuditor auditor = new ComplianceAuditor(endpoint, user, pass);
Map<String, Object> data = new HashMap<>();
data.put("kyc_status", "approved");
ComplianceResult result = auditor.auditCompliance("KYC", data);
String jsonReport = result.toJson("KYC");

// Parse JSON report back to ComplianceResult
ComplianceResult parsed = ComplianceResult.fromJson(jsonReport);
```

### Test Fixtures

```java
import com.tentoftrials.compliance.ComplianceAuditor.TestFixtures;

// Get test fixtures for JSON report validation
ComplianceResult passResult = TestFixtures.testJsonReportPass();
ComplianceResult failResult = TestFixtures.testJsonReportFail();
ComplianceResult emptyResult = TestFixtures.testJsonReportEmpty();
```

### Default Behavior

Without the `--json-report` flag, the auditor outputs human-readable text to the console:
```
=== COMPLIANCE AUDIT REPORT ===
Check Type: KYC
Status: COMPLIANT
Summary: All checks passed
```

The JSON report is deterministic for empty results (no violations) and ensures valid JSON output regardless of the number of violations.

## Troubleshooting

### Common Issues

**Service won't start**
1. Check logs: `kubectl logs -n tent-production deployment/backend-api`
2. Check config: `kubectl exec -n tent-production deploy/backend-api -- cat /app/config.yaml`
3. Check database connectivity: `kubectl exec -n tent-production deploy/backend-api -- nc -zv postgresql 5432`
4. Check resource limits: `kubectl describe pod -n tent-production -l app=backend-api`

**High latency**
1. Check database query performance: `SELECT * FROM pg_stat_activity WHERE state = 'active'`
2. Check connection pool utilization
3. Check for slow external API calls
4. Check garbage collection metrics
5. Check for network congestion

**Memory leak**
1. Capture heap dump: `kubectl exec -n tent-production deploy/backend-api -- kill -3 1`
2. Analyze heap dump with your preferred tool
3. Check for unclosed connections or goroutine leaks
4. Review recent code changes

**Database connection exhaustion**
1. Find idle connections: `SELECT pid, state, query_start FROM pg_stat_activity ORDER BY query_start`
2. Kill long-running queries: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'active' AND query_start < now() - interval '30 minutes'`
3. Check application connection pool settings
4. Consider increasing max_connections temporarily

**Certificate about to expire**
1. Generate new certificate
2. Update Kubernetes secret: `kubectl create secret tls tot-tls --cert=new.crt --key=new.key -n tent-production --dry-run=client -o yaml | kubectl apply -f -`
3. Restart services: `kubectl rollout restart deployment -n tent-production`
4. Verify new certificate: `openssl s_client -connect api.example.com:443 -servername api.example.com`

## Terraform Import Tool

The `tools/terraform_import.py` script manages importing existing AWS resources
into Terraform state. It is a legacy tool retained for environments without
Terraform Cloud access.

### Import Plan Summary

Use `--plan-summary PATH` to generate a JSON import plan without executing
imports. The plan lists every resource that would be imported, marks those
already in state, sorts deterministically by address, and redacts sensitive IDs
(passwords, tokens, keys, ARNs with secret paths).

```bash
# Dry-run with plan summary to file
python tools/terraform_import.py \
  --csv resources.csv \
  --dry-run \
  --plan-summary plan.json

# Print plan to stdout as well
python tools/terraform_import.py \
  --csv resources.csv \
  --dry-run \
  --plan-summary plan.json
```

Plan summary JSON structure:

```json
{
  "resources": [
    {
      "address": "aws_instance.web",
      "resource_type": "aws_instance",
      "resource_id": "i-0abc123def456789",
      "import_id": "i-0abc123def456789",
      "already_imported": false
    }
  ],
  "total": 1,
  "already_imported": 0
}
```

Sensitive resource IDs are redacted in the `resource_id` field. The original
value remains in `import_id` for audit purposes.

### Running Tests

```bash
python tools/terraform_import.py --test
```
