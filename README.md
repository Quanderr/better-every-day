# better-every-day

A self-hosted service that makes selected repositories a little better every day. On a schedule it scans a repo, picks a few small, real problems, fixes each one in a sandbox, runs the repo's own checks, files a ticket and opens a pull request. Humans only review. A daily Slack message lists what was opened.

## Architecture

- **Control plane** (AWS Lambda, trusted): loads config, syncs outcomes of earlier bot PRs, applies backpressure, starts worker sessions, gates results, creates tickets, pushes branches, opens PRs, sends the summary. Holds all write credentials. Makes no model calls and runs no repo code.
- **Worker** (Bedrock AgentCore Runtime session, untrusted): clones with a short-lived read-only token, installs toolchains with `mise`, runs a scout agent and a fixer agent (Strands, Opus on Bedrock), verifies with the repo's commands and returns file changes over SQS. It never sees VCS write or tracker credentials.
- **Untrusted worker output**: the control plane checks that the worker's base commit is on the base branch and recomputes every fingerprint itself.
- **Gates in code**: diff size, file count, excluded and protected paths, path safety, overlap with open bot PRs, duplicate fingerprints, green verification. Rejections are recorded, never published.
- **Idempotent publish**: ticket, branch and PR are looked up before being created, and every step is saved, so a redelivered or retried result never creates a second ticket or PR.
- **Learning**: merged and closed bot PRs feed a short per-repo lessons text into later runs; only comments by repo owners, members and collaborators are used, and lessons go into the user message marked as data, never into the system prompt. Closed fingerprints are never proposed again.
- **Budgets**: token budget and wall-clock timeout are checked before every model call, not only between fixes.
- **Ports and adapters**: `bed.core`, `bed.agents`, `bed.worker` and `bed.connectors` import nothing from AWS. AWS lives in `bed.platform.aws` and `infra/terraform/aws`.

```
src/bed/core          models, config, gates, ports, control-plane logic
src/bed/agents        prompts, workspace tools, Strands runner
src/bed/worker        shell, workspace, pipeline, AgentCore entrypoint
src/bed/connectors    github, gitlab, linear, jira, slack, shared HTTP client
src/bed/platform/aws  DynamoDB, Secrets Manager, SQS, AgentCore, Lambda handlers
docker/               worker and control images (linux/arm64)
infra/terraform/aws   VPC, firewall, runtime, Lambdas, table, queue, schedules
config/               example configurations (GitHub + Linear, GitLab + Jira)
```

## Configuration

One YAML file, validated with `uv run bed validate <path>` and again at `terraform plan`. Unknown keys are errors. See `config/example.yaml` (GitHub, Linear, Slack) and `config/example-gitlab-jira.yaml` (GitLab, Jira, Slack).

| Key | Meaning | Default |
|---|---|---|
| `model.provider` | Model provider name | required (`bedrock`) |
| `model.id` | Bedrock inference profile id | required |
| `connectors.vcs.<name>` | `type: github` or `gitlab`, `auth_secret: <Secrets Manager name>`, optional `api_url` (self-hosted GitLab; see Limits for GitHub Enterprise) | |
| `connectors.tracker.<name>` | `type: linear` or `jira`, `auth_secret`; Jira also needs `url` and takes optional `issue_type` (default `Task`) | |
| `connectors.notifier.<name>` | `type: slack`, `auth_secret`, `channel` | |
| `notifications.notifier` | Notifier used for the daily summary | none: no summary |
| `notifications.summary_schedule` | 5-field cron | |
| `notifications.timezone` | IANA timezone | |
| `defaults.prompt` | Deployment-wide guidance (prompt layer 2) | `""` |
| `defaults.categories` | Any of `bug`, `error-handling`, `test-gap`, `dead-code`, `perf`, `docs` | all |
| `defaults.limits.max_prs_per_run` | PRs opened per run | 2 |
| `defaults.limits.max_open_bot_prs` | Skip the run at or above this many open bot PRs | 5 |
| `defaults.limits.max_diff_lines` | Added plus removed lines per change | 150 |
| `defaults.limits.max_files` | Files per change | 5 |
| `defaults.limits.token_budget` | Model tokens per run | 2000000 |
| `defaults.limits.timeout_minutes` | Wall-clock per run | 60 |
| `defaults.base_branch`, `defaults.verify`, `defaults.exclude_paths`, `defaults.protected_paths` | Defaults for the matching `repos[]` keys; a repo value replaces the default, except `limits` (merged key by key) and `protected_paths` (lists concatenated) | |
| `repos[].name` | `owner/repo` | required |
| `repos[].vcs`, `repos[].tracker` | Connector names | required |
| `repos[].tracker_project` | Linear team key or Jira project key | required |
| `repos[].schedule`, `repos[].timezone` | 5-field cron and IANA timezone; must not restrict both day-of-month and day-of-week, no day-of-week steps or wrapping ranges (EventBridge limits) | required |
| `repos[].base_branch` | Branch to clone and target | `main` |
| `repos[].verify.mode` | `sandbox` runs checks in the worker; `ci` leaves them to the repo's CI | `sandbox` |
| `repos[].verify.install` | Install command, run once per session | none |
| `repos[].verify.commands` | Check commands; required in `sandbox` mode | `[]` |
| `repos[].exclude_paths` | Globs never touched | `[]` |
| `repos[].protected_paths` | Extra globs always rejected, added to the built-in list | `[]` |
| `repos[].prompt` | Repo guidance (prompt layer 3) | `""` |
| `repos[].categories`, `repos[].limits` | Override the defaults; limits merge key by key | defaults |

Bot PRs/MRs are found by the `better-every-day` label. Do not remove it: unlabelled bot PRs are invisible to outcome sync, overlap checks and `max_open_bot_prs`.

Protected paths are always rejected: CI and build configuration (`.github/**`, `.gitlab-ci.yml`, `.gitlab/**`, `*.gitlab-ci.yml`, `.circleci/**`, `.buildkite/**`, `Jenkinsfile`, `.travis.yml`, `.drone.yml`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`, `.husky/**`, `.pre-commit-config.yaml`), build scripts and manifests (`Makefile`, `*.mk`, `package.json`, `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `noxfile.py`), registry config (`.npmrc`, `.yarnrc*`, `.pypirc`), `CODEOWNERS`, lockfiles (`*.lock`, `package-lock.json`, `pnpm-lock.yaml`, `npm-shrinkwrap.json`, `go.sum`, `bun.lockb`), `.env*`, `*.pem`, `*.key`. Add more per repo or in `defaults` with `protected_paths` (lists merge).

### Keep CI secrets away from bot branches

Bot branches are pushed into the repository itself, so their CI runs before anyone reviews them, and tests are code the bot may edit. Make sure that CI cannot read deploy or production secrets on unprotected branches:
- GitHub: keep secrets in environments with required reviewers or a `main`-only branch rule; do not grant the App the Workflows permission.
- GitLab: mark CI/CD variables as protected so they are only exposed on protected branches; leave bot branches unprotected.

## Secrets

Create one JSON secret per connector in Secrets Manager, named as in `auth_secret`, in the deploy region, encrypted with the default `aws/secretsmanager` key (the control role has no `kms:Decrypt` for customer-managed keys):

- GitHub App: `{"app_id": "...", "private_key": "-----BEGIN ...", "installation_id": "..."}`. App permissions: contents read and write, pull requests read and write, issues read and write, metadata read. Install it on the target repos.
- GitLab: `{"token": "glpat-..."}`: a project or group access token with `api` scope and Maintainer role, so it can create read tokens for the worker (`read_repository`). Each token is revoked when the run's result arrives or the run times out; GitLab only accepts expiry dates, so an unrevoked token lapses within two days. Optionally add `"read_token": "glpat-..."` (a project access token with only `read_repository`) to skip minting. Self-hosted: set `api_url: https://<host>/api/v4` and add the host to `extra_allowed_domains`. Close comments only count from members with Developer access or higher.
- Linear: `{"api_key": "lin_api_..."}`.
- Jira Cloud: `{"email": "bot@example.com", "api_token": "..."}` from id.atlassian.com, for a user who can create issues and remote links in the projects. Set `url: https://<site>.atlassian.net` on the connector; `tracker_project` is the Jira project key. Tickets carry the labels `better-every-day` and `bed-fp-<fingerprint>`.
- Slack: `{"bot_token": "xoxb-..."}` with the `chat:write` scope; invite the bot to the channel.

## Deploy

### Prerequisites

- Terraform >= 1.9, `uv`, Docker with `linux/arm64` build support (buildx/QEMU on x86 hosts), AWS CLI with credentials for the target account.
- A region where Bedrock AgentCore Runtime and AWS Network Firewall are available.
- Bedrock model access for the Anthropic model in `model.id`, granted in the Bedrock console (Anthropic models need the one-time use-case form). For a cross-region inference profile such as `us.…`, every region the profile routes to must allow it.
- Terraform state is local by default (`*.tfstate` is gitignored). For anything beyond a trial, add a `backend.tf` with an S3 backend before the first apply; losing local state orphans every resource.

### Approximate fixed cost

Before any Bedrock tokens, the network stack costs roughly $700 per month at us-east-1 list prices: Network Firewall with 2 endpoints (about $575), 5 interface endpoints in 2 AZs (about $75), one NAT gateway (about $35), plus per-GB processing. Lambda, DynamoDB, SQS and AgentCore are billed per use. Check current AWS pricing for your region.

### First deploy

All `terraform` commands below take the same variables; set them once:
```
export TF_VAR_region=<region> TF_VAR_config_path=$PWD/<config.yaml> \
       TF_VAR_worker_image_tag=<tag> TF_VAR_control_image_tag=<tag>
```

1. Create the secrets above.
2. Write your config file and check it: `uv run bed validate <config.yaml>`.
3. Initialise and create the ECR repositories:
   ```
   terraform -chdir=infra/terraform/aws init
   terraform -chdir=infra/terraform/aws apply -target=aws_ecr_repository.worker -target=aws_ecr_repository.control
   ```
4. Log in to ECR and build and push both images (arm64):
   ```
   WORKER_REPO=$(terraform -chdir=infra/terraform/aws output -raw worker_repository_url)
   CONTROL_REPO=$(terraform -chdir=infra/terraform/aws output -raw control_repository_url)
   aws ecr get-login-password --region $TF_VAR_region | docker login --username AWS --password-stdin ${WORKER_REPO%%/*}
   docker build --platform linux/arm64 -f docker/worker.Dockerfile -t $WORKER_REPO:$TF_VAR_worker_image_tag .
   docker build --platform linux/arm64 -f docker/control.Dockerfile -t $CONTROL_REPO:$TF_VAR_control_image_tag .
   docker push $WORKER_REPO:$TF_VAR_worker_image_tag && docker push $CONTROL_REPO:$TF_VAR_control_image_tag
   ```
5. `terraform -chdir=infra/terraform/aws apply`.
6. Smoke test with a manual run (see Operations) and watch the logs.

The private subnets use the first two availability zones of the region by name. AgentCore supports only some AZ IDs per region; if the runtime is rejected for its subnets, check the supported AZ IDs in the AgentCore documentation.

Restrict who can create AgentCore runtimes in PUBLIC network mode with the IAM condition keys `bedrock-agentcore:subnets` and `bedrock-agentcore:securityGroups` on your deployer roles.

### Updates

- Config only: edit the file and run `terraform apply` with the same image tags. The control Lambdas read the config on cold start, so a change takes effect on their next cold start (new Lambda versions are not published for a config-only change).
- Code: ECR tags are immutable, so build and push both images under a **new** tag, then `terraform apply` with the new tags. Avoid deploying while a run is in progress; a result that arrives later is still processed.

### Teardown

`terraform destroy` fails on non-empty resources. First empty the config bucket including all object versions, and delete the images in both ECR repositories. The DynamoDB table has point-in-time recovery but no deletion protection; export it first if you want to keep the history.

## Operations

- Start a run now: `aws lambda invoke --function-name better-every-day-schedule --cli-binary-format raw-in-base64-out --payload '{"action":"run","repo":"<owner/repo>"}' /dev/stdout`.
- Send the summary now: same command with `'{"action":"summary"}'`.
- Logs: `/aws/lambda/better-every-day-schedule`, `/aws/lambda/better-every-day-result`, the worker under `/aws/bedrock-agentcore/runtimes/`, blocked egress in `/better-every-day/network-firewall/alert`.
- Alarms: `better-every-day-results-dlq-not-empty` and `better-every-day-bedrock-daily-cost`. Pass `-var alarm_topic_arn=<SNS topic ARN>` to be notified; the alarms exist either way.
- Dead-letter queue: results that failed processing 5 times land in `better-every-day-results-dlq` (kept 14 days). Check the result Lambda logs for the cause, fix it, then redrive to the main queue from the SQS console or with `aws sqs start-message-move-task --source-arn <dlq arn>`. Results older than 24 hours after a run timed out are ignored.
- Findings stuck in `publishing` for over an hour, or that failed to publish, appear in the daily summary; a failed publish is retried by the next run that finds the same issue, reusing its ticket.

Names above assume the default `-var name=better-every-day`.

## Security notes

Repo install and test commands get only an allowlisted environment (`PATH`, `HOME`, locale, CA bundle, `MISE_*`). VPC endpoints carry policies limited to this account, the results queue and ECR image layers.

Worker DNS is limited by a Route 53 Resolver DNS Firewall to the same allowlist plus `amazonaws.com`, and Bedrock and SQS are reached through VPC endpoints; the worker role only works from those endpoints. Pass `-var bedrock_daily_cost_alarm_usd=<usd>` to tune the daily Bedrock cost alarm (default 30 USD). The estimate multiplies Bedrock token metrics (input, output, cache read, cache write) by `bedrock_usd_per_million_tokens`, which defaults to Claude Opus 5.5 list prices; set it to your model's Bedrock prices for your region.

Worker egress is limited by AWS Network Firewall to the domains in `infra/terraform/aws/firewall.tf` plus `extra_allowed_domains`, matched on the TLS server name. This does not stop a process that sends an allowlisted server name to a raw IP address, and allowlisted public hosts (GitHub, npm, PyPI) can still receive data from repo code; TLS inspection is not configured. Blocked connections appear in the `/better-every-day/network-firewall/alert` log group.

The built-in allowlist covers GitHub, gitlab.com, npm/yarn, PyPI, RubyGems, Maven Central, Gradle, the Go module proxy, mise, nodejs.org, java.net, go.dev and ruby-lang.org. Other ecosystems (for example Rust: `static.rust-lang.org`, `.crates.io`) and private registries need `-var 'extra_allowed_domains=[...]'`; a domain starting with `.` matches subdomains.

## Add a connector

1. Implement the matching Protocol from `bed.core.ports` (`VcsConnector`, `TrackerConnector` or `NotifierConnector`).
2. Expose `create(secret: dict[str, str], options: dict[str, str])` and register it as an entry point in the `bed.vcs`, `bed.tracker` or `bed.notifier` group.
3. Reference its entry-point name as `type` in config. Extra keys on the connector become `options`.

## Development

```
uv sync --all-extras
uv run pytest
uv run ruff check
```

## Limits

- No Docker inside the worker: repos whose tests need services should use `verify.mode: ci`.
- No cache between runs; every run installs toolchains and dependencies from scratch.
- Session storage is capped around 1 GB; a full disk ends the run as `workspace_full`. Disk usage and file count per run are logged by the worker.
- No replies to review comments, no auto-merge.
- GitHub Enterprise is not supported: `api_url` changes the API host, but the worker always clones from `github.com`.
- GitLab deploy tokens are not supported (the worker authenticates as `x-access-token`); use project or group access tokens.
- AWS is the only supported cloud.
