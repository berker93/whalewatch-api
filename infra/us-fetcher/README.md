# The US-region fetcher

One Lambda function in us-east-1 that fetches a URL and returns its bytes,
for the sites that answer non-US addresses badly or not at all: Senate eFD and
the House Clerk. The worker invokes it, it fetches, it puts the body in an S3
spool bucket, and the worker reads the body from there, archives it to the raw
store and deletes the spool copy.

```
worker (anywhere)                          us-east-1
─────────────────                          ─────────────────────────────────────
LambdaFetcher ── lambda:Invoke ──────────▶ handler.lambda_handler
   │              {url, method, headers,      │  urllib + a cookie jar
   │               body, cookies}             ├──▶ efdsearch.senate.gov
   │                                          │
   │  ◀── {status, headers, cookies, ─────────┤  body ──▶ s3://<spool>/spool/<id>
   │       body: {bucket, key, sha256}}
   │
   └── s3:GetObject, check sha256, s3:DeleteObject
   └── RawStore.put (raw/senate-efd/...)  ──▶ then parse
```

The code is [app/ingestion/remote/handler.py](../../app/ingestion/remote/handler.py),
standard library only, so the zip is that one file. The worker side is
[fetcher.py](../../app/ingestion/remote/fetcher.py) next to it.

## What it costs

Nothing, at this volume. A few thousand invocations a month at 512 MB and
around a second each is inside Lambda's free tier, and a spool that holds each
body for seconds costs nothing to store. You pay for data transfer out of
us-east-1 to a worker outside AWS, which for HTML pages is cents a month.

## First deploy

You need an AWS account and the AWS CLI (`brew install awscli`), signed in as
someone allowed to create IAM roles, Lambda functions and S3 buckets:

```sh
aws configure          # or: export AWS_PROFILE=...
infra/us-fetcher/deploy.sh
```

The script deploys [template.yaml](template.yaml) as the CloudFormation stack
`whalewatch-us-fetcher` (pass another name as its argument), then uploads the
handler as the function's code. Until that upload, the function runs a
placeholder that refuses every request, so a half-finished deploy fails loudly.

It creates:

| Resource | What for |
| --- | --- |
| `SpoolBucket` | Response bodies in transit. Private, TLS-only, every object expired after a day |
| `Function` | The fetcher. Python 3.13 on arm64, 512 MB, 60 s timeout |
| `FunctionRole` | Lets the function write its logs and put to `spool/`. Nothing else |
| `FunctionLogs` | One JSON line per fetch (host, method, status, size; never cookies). 14 days |
| `WorkerPolicy` | What the worker needs: invoke the function; get and delete in `spool/` |

## Giving the worker access

The worker needs credentials with `WorkerPolicy` attached. Off AWS, that means
an IAM user that has only that policy:

1. IAM → Users → Create user (`whalewatch-worker`), no console access.
2. Attach policies directly → `WorkerPolicy` (its ARN is the stack's
   `WorkerPolicyArn` output).
3. The user → Security credentials → Create access key → "Application running
   outside AWS".

Then in the worker's environment:

```sh
US_FETCHER_BACKEND=lambda
US_FETCHER_FUNCTION=whalewatch-us-fetcher
US_FETCHER_REGION=us-east-1
US_FETCHER_ACCESS_KEY_ID=AKIA...
US_FETCHER_SECRET_ACCESS_KEY=...
```

and check it:

```sh
uv run python -m app.cli fetcher-check
```

## Changing it

- **Code only** (handler.py changed): re-run `deploy.sh`. The stack has no
  changes, so CloudFormation does nothing and the upload is a few seconds.
- **A new host**: add it to the `AllowedHosts` parameter
  (`aws cloudformation deploy ... --parameter-overrides AllowedHosts=...`, or
  edit the default) *and* to `LOCAL_ALLOWED_HOSTS` in fetcher.py, so local runs
  and deployed runs refuse the same things.
- **A cap on concurrency**: `MaxConcurrency` reserves that many concurrent
  executions. It defaults to 0, meaning no reservation, because a new account's quota is
  10 and AWS keeps 10 unreserved, so any reservation fails until Service Quotas
  raises "Concurrent executions".

## Why the allow-list matters

Anyone holding the worker's key can make this function fetch any URL it allows,
from an AWS address, with any headers. The allow-list, checked on every
redirect hop as well, is what keeps a leaked key from being worth anything
beyond the sites we read anyway.
