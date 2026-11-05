#!/usr/bin/env bash
# Create or update the US fetcher stack, then upload the handler as its code.
#
#   infra/us-fetcher/deploy.sh [stack-name]     # default: whalewatch-us-fetcher
#
# Needs the AWS CLI, signed in to the account (aws configure, or AWS_PROFILE).
# Always us-east-1: the point of the function is where it runs. Safe to re-run;
# a code-only change is the second half alone, and takes seconds.
set -euo pipefail

STACK="${1:-whalewatch-us-fetcher}"
REGION="us-east-1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDLER="$HERE/../../app/ingestion/remote/handler.py"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "==> stack $STACK in $REGION"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$HERE/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset

FUNCTION="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text)"
POLICY="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='WorkerPolicyArn'].OutputValue" --output text)"

echo "==> code for $FUNCTION"
# The handler alone, at the zip's root, as handler.py: it imports nothing
# outside the standard library, which tests/test_us_fetcher_handler.py checks.
cp "$HANDLER" "$BUILD/handler.py"
(cd "$BUILD" && zip -q -X handler.zip handler.py)
aws lambda update-function-code \
  --region "$REGION" \
  --function-name "$FUNCTION" \
  --zip-file "fileb://$BUILD/handler.zip" \
  --query "{sha256: CodeSha256, size: CodeSize}" --output text
aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION"

cat <<EOF
==> deployed. For the worker:
    US_FETCHER_BACKEND=lambda
    US_FETCHER_FUNCTION=$FUNCTION
    US_FETCHER_REGION=$REGION
    credentials with $POLICY attached
then: uv run python -m app.cli fetcher-check
EOF
