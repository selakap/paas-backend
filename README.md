# PaaS POC Backend

Local FastAPI backend with 3 flows:

1. `POST /build` — clone a git repo, docker build (repo must already have a
   Lambda-compatible `Dockerfile`, e.g. `FROM public.ecr.aws/lambda/python:3.12`),
   push to ECR, return the image URI/ARN.
2. `POST /deploy/cron` — take an image URI and create a container-image Lambda
   + EventBridge schedule rule via CDK.
3. `POST /deploy/api` — take an image URI and create a container-image Lambda
   + HTTP API (API Gateway v2) via CDK.

## Prerequisites

- Python 3.10+
- Docker running locally (used for `docker build` / `docker push`)

  > **Deployment note:** this is the one dependency that blocks running the
  > API on AWS Fargate — Fargate doesn't support Docker-in-Docker, so a
  > Fargate-hosted API can't shell out to `docker build` itself. If you want
  > a Fargate-friendly deployment, consider offloading the build step to
  > **AWS CodeBuild** (managed, privileged build containers that build and
  > push to ECR) and having `/build` call `StartBuild` + poll for
  > completion instead of running `docker` locally. That removes the local
  > Docker requirement entirely and would let the whole app (including the
  > Sonar scan, which has no such constraint) run on Fargate instead of
  > requiring an EC2-backed host.

- AWS CLI configured with credentials (`aws configure`) that have permissions
  for ECR, Lambda, EventBridge, API Gateway, IAM, CloudFormation
- Node.js + AWS CDK CLI: `npm install -g aws-cdk`
- CDK bootstrapped in your target account/region (one-time):
  ```
  cdk bootstrap aws://ACCOUNT_ID/REGION
  ```
- (Optional) SonarCloud scanning — see [SonarCloud scanning](#sonarcloud-scanning)
  below. Only needed if you want to run `run_sonar: true` on `/build` or use
  the `/approvals` flow.

## Running locally, from scratch

This walks through everything from installing Python to making your first
request, for a machine that has none of this set up yet.

### 1. Install Python 3.10+

Check if you already have a usable version:

```bash
python --version   # or: python3 --version
```

If you need to install it:

- **Windows**: download the installer from https://www.python.org/downloads/
  and run it. Check "Add python.exe to PATH" on the first screen. Verify in a
  new terminal with `python --version`.
- **macOS**: `brew install python@3.12`
- **Linux (Debian/Ubuntu)**: `sudo apt update && sudo apt install python3 python3-venv python3-pip`

### 2. Get the code

```bash
git clone <this-repo-url>
cd paas-backend
```

(If you already have the folder locally, just `cd` into it.)

### 3. Create and activate a virtual environment

A venv keeps this project's Python packages isolated from the rest of your
system.

```bash
python -m venv .venv
```

Activate it — the command differs by shell:

```bash
# macOS / Linux (bash/zsh)
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows cmd.exe
.venv\Scripts\activate.bat

# Windows Git Bash
source .venv/Scripts/activate
```

Your prompt should now show a `(.venv)` prefix. Everything below assumes the
venv is active.

> Windows PowerShell note: if `Activate.ps1` is blocked by the execution
> policy, run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
> in that same terminal first, then re-run the activate command.

### 4. Install dependencies

```bash
python -m pip install --upgrade pip

# Backend deps (FastAPI, uvicorn, boto3, pydantic)
pip install -r requirements.txt

# CDK app deps (used when main.py shells out to `cdk deploy`)
pip install -r cdk/requirements.txt
```

### 5. Install and check the remaining prerequisites

- **Docker**: install Docker Desktop (Windows/macOS) or `docker` (Linux),
  then confirm it's running with `docker info`.
- **AWS CLI**: install from
  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html,
  then run `aws configure` and supply an access key / secret / default
  region for an account with permissions for ECR, Lambda, EventBridge,
  API Gateway, IAM, and CloudFormation.
- **Node.js + AWS CDK CLI**: install Node.js (https://nodejs.org/), then:
  ```bash
  npm install -g aws-cdk
  cdk --version
  ```
- **Bootstrap CDK** in your target account/region (one-time per
  account/region):
  ```bash
  cdk bootstrap aws://ACCOUNT_ID/REGION
  ```

Make sure `AWS_REGION` / `AWS_DEFAULT_REGION` and credentials are set in your
shell env, since both boto3 and the CDK CLI rely on them:

```bash
# macOS / Linux / Git Bash
export AWS_REGION=us-east-1

# Windows PowerShell
$env:AWS_REGION = "us-east-1"
```

### 6. Run the app

With the venv still active:

```bash
uvicorn main:app --reload --port 8000
```

`--reload` restarts the server automatically when you edit the code — handy
for local development, drop it for anything long-running.

You should see log output ending in something like
`Uvicorn running on http://127.0.0.1:8000`.

### 7. Verify it's up

```bash
curl http://localhost:8000/health
```

Expected response: `{"status":"ok"}`

Interactive API docs (Swagger UI) are at http://localhost:8000/docs — you can
also invoke every endpoint from there without curl.

### 8. Invoke the app

See [Example usage](#example-usage) below for `curl` calls against `/build`,
`/deploy/cron`, and `/deploy/api`. Start with `/build`, then feed its
`image_uri` into one of the `/deploy/*` calls.

When you're done, stop the server with `Ctrl+C` and deactivate the venv with
`deactivate`.

## Example usage

### 1. Build & push

```bash
curl -X POST http://localhost:8000/build \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/your-org/your-lambda-repo.git",
    "branch": "main",
    "function_name": "my-cron-fn"
  }'
```

Response:
```json
{
  "status": "success",
  "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-cron-fn:ab12cd34",
  "image_tag": "ab12cd34",
  "repository_arn": "arn:aws:ecr:us-east-1:123456789012:repository/my-cron-fn",
  "repository_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-cron-fn"
}
```

### 2. Deploy as a cron job

```bash
curl -X POST http://localhost:8000/deploy/cron \
  -H "Content-Type: application/json" \
  -d '{
    "function_name": "my-cron-fn",
    "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-cron-fn:ab12cd34",
    "schedule_expression": "rate(5 minutes)"
  }'
```

### 3. Deploy behind an API Gateway

```bash
curl -X POST http://localhost:8000/deploy/api \
  -H "Content-Type: application/json" \
  -d '{
    "function_name": "my-api-fn",
    "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-api-fn:ef56gh78"
  }'
```

Both deploy endpoints return the stack's `CfnOutput`s (e.g. `FunctionArn`,
`ApiUrl`) as JSON.

## SonarCloud scanning

`services/sonar_scan.py` wraps the `sonar-scanner` CLI and polls SonarCloud
for the Quality Gate result. It's used in two places:

- `POST /build` — pass `"run_sonar": true` (and optionally
  `"block_on_quality_gate_failure": true|false`, default `true`) to scan the
  cloned source before the Docker build. If the Quality Gate fails and
  blocking is on, the build is aborted with an error pointing at the
  dashboard URL. The result (if run) is included under `sonar_scan` in the
  response.
- `POST /approvals` — if Sonar is configured (see below), an approval
  request automatically kicks off a scan in a background thread after the
  response is returned. Poll `GET /approvals/{id}` and watch
  `sonar_scan_status` move from `running` to `completed`/`failed`, alongside
  `sonar_quality_gate` and `sonar_dashboard_url`.

If Sonar env vars aren't set, `run_sonar: true` on `/build` fails fast with a
clear error, and `/approvals` simply skips scanning (`sonar_scan_status:
"not_requested"`) — it's entirely optional.

### 1. Install the SonarScanner CLI

The scanner requires a JRE 17+ on PATH, plus the `sonar-scanner` binary
itself:

- **All platforms**: download from
  https://docs.sonarsource.com/sonarqube-cloud/advanced-setup/ci-based-analysis/sonarscanner-cli/
  and add the `bin/` folder to your `PATH`.
- **macOS**: `brew install sonar-scanner`
- **Windows**: unzip the CLI distribution somewhere permanent and add its
  `bin` directory to `PATH` (System Properties → Environment Variables), or
  `choco install sonarscanner-msbuild-net5` if you use Chocolatey.

Verify it's on PATH:

```bash
sonar-scanner --version
```

### 2. Create a SonarCloud account, org, and token

1. Sign up / log in at https://sonarcloud.io and create (or pick) an
   organization — this is your `SONAR_ORGANIZATION`.
2. Generate a user token: **My Account → Security → Generate Token**.
3. You don't need to pre-create a project — `sonar_scan.run_scan` passes
   `sonar.projectKey` / `sonar.projectName` (derived from `function_name`)
   and SonarCloud will create the project on first analysis if your
   organization allows it.

### 3. Set environment variables

```bash
# macOS / Linux / Git Bash
export SONAR_TOKEN=your_sonarcloud_token
export SONAR_ORGANIZATION=your_sonarcloud_org_key
export SONAR_HOST_URL=https://sonarcloud.io   # optional, this is the default

# Windows PowerShell
$env:SONAR_TOKEN = "your_sonarcloud_token"
$env:SONAR_ORGANIZATION = "your_sonarcloud_org_key"
$env:SONAR_HOST_URL = "https://sonarcloud.io"   # optional, this is the default
```

`SONAR_TOKEN` and `SONAR_ORGANIZATION` are required for scanning to run at
all (`sonar_scan.is_configured()` checks both); `SONAR_HOST_URL` only needs
to be set if you're pointing at a self-hosted SonarQube instance instead of
SonarCloud. Set these in the same shell you launch `uvicorn` from, before
starting the server.

### 4. Try it

```bash
curl -X POST http://localhost:8000/build \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/your-org/your-lambda-repo.git",
    "branch": "main",
    "function_name": "my-cron-fn",
    "run_sonar": true
  }'
```

## Project layout

```
paas-backend/
  main.py                     # FastAPI app, 3 endpoints
  requirements.txt
  services/
    builder.py                 # git clone -> docker build -> ECR push
    cdk_deploy.py               # shells out to `cdk deploy` with -c context, reads outputs
    sonar_scan.py                # runs sonar-scanner, polls SonarCloud Quality Gate
  cdk/
    app.py                      # CDK entrypoint, picks stack based on context
    cdk.json
    requirements.txt
    stacks/
      lambda_cron_stack.py      # DockerImageFunction + EventBridge rule
      lambda_api_stack.py       # DockerImageFunction + HTTP API
```

## Porting to other stacks (e.g. .NET, Java)

None of this app's logic is actually Python-specific — it's almost entirely
CLI orchestration (`git`, `docker`, `cdk`, `sonar-scanner`) plus AWS SDK
calls and HTTP polling, all of which have direct equivalents in other
runtimes. If you have an existing ASP.NET Core or Spring Boot app and want
to add this as new controllers instead of running a separate Python
service:

| This app (Python)                          | .NET equivalent                                              | Java equivalent                                                   |
|----------------------------------------------|-----------------------------------------------------------------|------------------------------------------------------------------|
| `subprocess.run([...])`                     | `System.Diagnostics.Process.Start(...)`                        | `java.lang.ProcessBuilder`                                        |
| `boto3` (ECR, Lambda, EventBridge, API GW)  | `AWSSDK.ECR`, `AWSSDK.Lambda`, `AWSSDK.CloudFormation`, etc.     | AWS SDK for Java v2 (`software.amazon.awssdk:ecr`, `:lambda`, `:cloudformation`, etc.) |
| `requests` (SonarCloud API polling)         | `HttpClient`                                                    | `java.net.http.HttpClient`                                        |
| FastAPI endpoints / Pydantic models         | ASP.NET Core controllers / DTOs with data annotations           | Spring `@RestController` / DTOs with `jakarta.validation` annotations |
| `cdk` CLI shelled out from `cdk_deploy.py`  | Same — `cdk deploy` shelled out via `Process.Start`, or a CDK app written in C# (CDK supports C# as a first-class language) | Same — `cdk deploy` shelled out via `ProcessBuilder`, or a CDK app written in Java (also a first-class CDK language) |

The one dependency that doesn't go away: the **CDK CLI is Node.js-based**
regardless of which language you write the stacks in, so `cdk deploy` still
requires Node.js on the host/container even for a C#/Java CDK app — you'd
add Node.js to the runtime image, not write any Node.js code. Everything
else (Docker, git, Sonar) is just an external binary invocation either way,
so the language you write the API layer in doesn't change what has to be
installed alongside it — see the Fargate note under
[Prerequisites](#prerequisites) for how that interacts with hosting choices.

## POC notes / shortcuts taken

- No auth, no persistence/DB — every call is synchronous and stateless.
- `/build` assumes the repo already contains a Dockerfile targeting a Lambda
  base image; it does not generate or validate one.
- ECR repo is created (if missing) using the lowercased `function_name`.
- CDK is invoked via subprocess (`cdk deploy`), so deploys are synchronous and
  block the request — fine for a POC, but you'd want a background job queue
  (e.g. Celery/RQ + polling endpoint, or websockets for streaming CDK logs)
  before this is used for real.
- Each deploy call targets its own stack (named `<function_name>-cron-stack`
  or `<function_name>-api-stack`), so re-running with the same function_name
  updates that stack rather than creating a new one.
