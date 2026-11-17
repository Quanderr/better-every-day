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
src/bed/connectors    github, linear, slack
src/bed/platform/aws  DynamoDB, Secrets Manager, SQS, AgentCore, Lambda handlers
docker/               worker and control images (linux/arm64)
infra/terraform/aws   VPC, firewall, runtime, Lambdas, table, queue, schedules
config/example.yaml   example configuration
```

## Configuration

One YAML file, validated with `uv run bed validate <path>` and again at `terraform plan`. Unknown keys are errors. See `config/example.yaml`.

| Key | Meaning | Default |
|---|---|---|
| `model.provider` | Model provider name | required (`bedrock`) |
| `model.id` | Bedrock inference profile id | required |
| `connectors.vcs.<name>` | `type: github` or `gitlab`, `auth_secret: <Secrets Manager name>`, optional `api_url` (GitHub Enterprise / self-hosted GitLab) | |
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
| `repos[].name` | `owner/repo` | required |
| `repos[].vcs`, `repos[].tracker` | Connector names | required |
| `repos[].tracker_project` | Linear team key | required |
| `repos[].schedule`, `repos[].timezone` | 5-field cron and IANA timezone; must not restrict both day-of-month and day-of-week, no day-of-week steps or wrapping ranges (EventBridge limits) | required |
| `repos[].base_branch` | Branch to clone and target | `main` |
| `repos[].verify.mode` | `sandbox` runs checks in the worker; `ci` leaves them to the repo's CI | `sandbox` |
| `repos[].verify.install` | Install command, run once per session | none |
| `repos[].verify.commands` | Check commands; required in `sandbox` mode | `[]` |
| `repos[].exclude_paths` | Globs never touched | `[]` |
| `repos[].protected_paths` | Extra globs always rejected, added to the built-in list | `[]` |
| `repos[].prompt` | Repo guidance (prompt layer 3) | `""` |
| `repos[].categories`, `repos[].limits` | Override the defaults; limits merge key by key | defaults |

Protected paths are always rejected: CI and build configuration (`.github/**`, `.gitlab-ci.yml`, `.gitlab/**`, `*.gitlab-ci.yml`, `.circleci/**`, `.buildkite/**`, `Jenkinsfile`, `.travis.yml`, `.drone.yml`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`, `.husky/**`, `.pre-commit-config.yaml`), build scripts and manifests (`Makefile`, `*.mk`, `package.json`, `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `noxfile.py`), registry config (`.npmrc`, `.yarnrc*`, `.pypirc`), `CODEOWNERS`, lockfiles (`*.lock`, `package-lock.json`, `pnpm-lock.yaml`, `npm-shrinkwrap.json`, `go.sum`, `bun.lockb`), `.env*`, `*.pem`, `*.key`. Add more per repo or in `defaults` with `protected_paths` (lists merge).

### Keep CI secrets away from bot branches

Bot branches are pushed into the repository itself, so their CI runs before anyone reviews them, and tests are code the bot may edit. Make sure that CI cannot read deploy or production secrets on unprotected branches:
- GitHub: keep secrets in environments with required reviewers or a `main`-only branch rule; do not grant the App the Workflows permission.
- GitLab: mark CI/CD variables as protected so they are only exposed on protected branches; leave bot branches unprotected.

## Secrets

Create one JSON secret per connector in Secrets Manager, named as in `auth_secret`:

- GitHub App: `{"app_id": "...", "private_key": "-----BEGIN ...", "installation_id": "..."}`. App permissions: contents read and write, pull requests read and write, issues read and write, metadata read. Install it on the target repos.
- GitLab: `{"token": "glpat-..."}`: a project or group access token with `api` scope and Maintainer role, so it can create read tokens for the worker (`read_repository`). Each token is revoked when the run's result arrives or the run times out; GitLab only accepts expiry dates, so an unrevoked token lapses within two days. Optionally add `"read_token": "glpat-..."` (a project access token with only `read_repository`) to skip minting. Self-hosted: set `api_url: https://<host>/api/v4` and add the host to `extra_allowed_domains`. Close comments only count from members with Developer access or higher.
- Linear: `{"api_key": "lin_api_..."}`.
- Jira Cloud: `{"email": "bot@example.com", "api_token": "..."}` from id.atlassian.com, for a user who can create issues and remote links in the projects. Set `url: https://<site>.atlassian.net` on the connector; `tracker_project` is the Jira project key. Tickets carry the labels `better-every-day` and `bed-fp-<fingerprint>`.
- Slack: `{"bot_token": "xoxb-..."}` with the `chat:write` scope; invite the bot to the channel.

## Deploy

1. Create the secrets above.
2. Write your config file.
3. Build and push both images (arm64):
   ```
   docker build --platform linux/arm64 -f docker/worker.Dockerfile -t <worker_repo>:<tag> .
   docker build --platform linux/arm64 -f docker/control.Dockerfile -t <control_repo>:<tag> .
   docker push <worker_repo>:<tag> && docker push <control_repo>:<tag>
   ```
   The ECR repositories are created by Terraform; on a first deploy apply with `-target=aws_ecr_repository.worker -target=aws_ecr_repository.control` before pushing.
4. `terraform -chdir=infra/terraform/aws apply -var region=<region> -var config_path=<path> -var worker_image_tag=<tag> -var control_image_tag=<tag>`.

Restrict who can create AgentCore runtimes in PUBLIC network mode with the IAM condition keys `bedrock-agentcore:subnets` and `bedrock-agentcore:securityGroups` on your deployer roles.

Pass `-var alarm_topic_arn=<SNS topic ARN>` to be notified when results land in the dead-letter queue; the alarm exists either way. Findings stuck in `publishing` for over an hour, or that failed to publish, appear in the daily summary; a failed publish is retried by the next run that finds the same issue, reusing its ticket.

Repo install and test commands get only an allowlisted environment (`PATH`, `HOME`, locale, CA bundle, `MISE_*`). VPC endpoints carry policies limited to this account, the results queue and ECR image layers.

Worker DNS is limited by a Route 53 Resolver DNS Firewall to the same allowlist plus `amazonaws.com`, and Bedrock and SQS are reached through VPC endpoints; the worker role only works from those endpoints. Pass `-var bedrock_daily_token_alarm=<tokens>` to tune the daily Bedrock token alarm (default 50M).

Worker egress is limited by AWS Network Firewall to the domains in `infra/terraform/aws/firewall.tf` plus `extra_allowed_domains`, matched on the TLS server name. This does not stop a process that sends an allowlisted server name to a raw IP address, and allowlisted public hosts (GitHub, npm, PyPI) can still receive data from repo code; TLS inspection is not configured. Blocked connections appear in the `/better-every-day/network-firewall/alert` log group.

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
- A second cloud is not implemented yet. See `config/example-gitlab-jira.yaml` for a GitLab and Jira setup.
