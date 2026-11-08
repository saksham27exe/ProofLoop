# REST API

The backend is a FastAPI app on `http://127.0.0.1:8000`. Interactive OpenAPI docs: <http://localhost:8000/docs>
(click **Authorize** and sign in).

All endpoints except `/health` and `/auth/bootstrap*` require `Authorization: Bearer <token>`.
Roles: **viewer** (read) < **analyst** (run executions and benchmarks) < **admin** (users, settings, sources).

## Endpoints

| Method | Endpoint | Purpose | Role |
|---|---|---|---|
| GET | `/health` | Liveness / readiness | public |
| GET | `/health/llm` · `/models` · `/models/cloud` | Active model status · installed local models · live cloud catalog | viewer |
| POST | `/auth/bootstrap` · `/auth/login` · `/auth/token` | First admin · sign in · OAuth2 form (Swagger) | public |
| POST / GET | `/auth/logout` · `/auth/change-password` · `/auth/me` | Session management | any |
| GET/POST/PATCH | `/users` · `/users/{id}` · `/users/{id}/reset-password` · GET `/audit` | User admin, audit log | admin |
| POST | `/executions` | Create an execution and interpret its plan | analyst |
| GET | `/executions` · `/executions/{id}` | List (filter by verdict / environment / text) · details | viewer |
| GET | `/executions/{id}/traces` · `/executions/{id}/snapshots` | Hash-chained trace · snapshot hashes | viewer |
| POST | `/executions/{id}/run` | Run ProofLoop's agent (`approved`, `force`, `verify`) | analyst |
| POST | `/executions/{id}/verify` | Verify (creates the immutable report) | analyst |
| POST | `/executions/{id}/undo` | Restore a connected source from the backup taken before this run | analyst |
| GET | `/executions/{id}/report` · `/reports/{id}/export?format=json\|markdown` | Report · download with integrity header | viewer |
| POST | `/executions/{id}/session/start` | **Your own agent:** backup + before-snapshot; returns tool specs and policy | analyst |
| POST | `/executions/{id}/session/tools/{tool}` | Run one ProofLoop tool for your agent (policy-checked, traced) | analyst |
| POST | `/executions/{id}/session/events` | Report tool calls your agent made itself, or notes | analyst |
| POST | `/executions/{id}/session/finish` · `/session/abort` | Record the claim and verify · abandon | analyst |
| GET | `/executions/{id}/session/tools` | OpenAI-style tool definitions for a session | viewer |
| GET/POST/DELETE | `/sources` · `/sources/{id}` | List · connect (tested first) · disconnect data sources | viewer / admin / admin |
| POST | `/sources/{id}/refresh` · `/sources/practice/reset` | Re-read structure · regenerate the practice store | analyst |
| GET/PUT | `/sources/{id}/policy` | Write policy (guardrails) of a data source | viewer / admin |
| GET | `/stats` · `/scenarios` · `/options` | Dashboard data | viewer |
| GET/POST | `/sandbox/{env}` · `/sandbox/{env}/reset` | Inspect / reset a demo sandbox | viewer / analyst |
| GET/PUT | `/settings` | Runtime settings, model backend (cloud needs `cloud_consent`) | viewer / admin |
| POST/GET | `/benchmark/run` · `/benchmark/latest` | Benchmark | analyst / viewer |

Errors use one shape: `{"error": {"code": "approval_required", "message": "..."}}`.

## Example: run and verify a demo task (PowerShell)

```powershell
$t = (Invoke-RestMethod -Method Post http://127.0.0.1:8000/auth/login -ContentType application/json `
      -Body '{"username":"admin","password":"<your password>"}').access_token
$h = @{ Authorization = "Bearer $t" }
$e = Invoke-RestMethod -Method Post http://127.0.0.1:8000/executions -Headers $h -ContentType application/json `
      -Body '{"instruction":"Close ticket 12","environment":"api","fault_mode":"tool_failure_false_claim"}'
(Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$($e.id)/run" -Headers $h `
      -ContentType application/json -Body '{"verify":true}').report.summary
```

## Example: verify your own agent (curl)

```bash
TOKEN=...   # from /auth/login
ID=$(curl -s -X POST localhost:8000/executions -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"instruction":"Change the email of customer 5 to a@b.example","environment":"src_practice","agent_config":"external"}' | jq -r .id)
curl -s -X POST localhost:8000/executions/$ID/session/start -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
curl -s -X POST localhost:8000/executions/$ID/session/tools/update_rows -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"arguments":{"table":"customers","where":{"id":5},"values":{"email":"a@b.example"}}}'
curl -s -X POST localhost:8000/executions/$ID/session/finish -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"claim":"DONE"}' | jq .verdict
```

In Python, use the SDK instead - see [own_agents.md](own_agents.md).
