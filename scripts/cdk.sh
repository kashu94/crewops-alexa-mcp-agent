#!/usr/bin/env bash
# Run the AWS CDK CLI with settings from .env.
#
#   scripts/cdk.sh diff
#   scripts/cdk.sh deploy --all
#   scripts/cdk.sh destroy --all
#
# .env must name an AWS CLI profile (AWS_PROFILE). Access keys live in ~/.aws, never in .env.
# The script refuses to run without a named profile, so nothing can reach a default account
# by accident, and it prints the target account before doing anything.
set -euo pipefail

cd "$(dirname "$0")/.."

# Load .env without overriding variables already set in the shell, the same rule the Python
# code follows: `AWS_REGION=... scripts/cdk.sh diff` beats the file.
if [[ -f .env ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    line="${line#export }"
    key="${line%%=*}"
    value="${line#*=}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "${value}" =~ ^\"(.*)\"$ || "${value}" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%% #*}"
    fi
    [[ -z "${!key+set}" ]] && export "${key}=${value}"
  done < .env
fi

: "${AWS_REGION:=ap-southeast-2}"
: "${CREWOPS_STAGE:=dev}"
export AWS_REGION CREWOPS_STAGE
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1

if [[ -z "${AWS_PROFILE:-}" || "${AWS_PROFILE}" == "default" ]]; then
  echo "Set AWS_PROFILE in .env to your named profile (for example: AWS_PROFILE=crewops)." >&2
  echo "The default profile is refused on purpose." >&2
  exit 1
fi
export AWS_PROFILE

identity=$(aws sts get-caller-identity --output text --query '[Account,Arn]') || {
  echo "Could not authenticate with profile '${AWS_PROFILE}'." >&2
  exit 1
}
read -r account arn <<<"${identity}"
echo "Profile: ${AWS_PROFILE}  Account: ${account}  Region: ${AWS_REGION}  Stage: ${CREWOPS_STAGE}"
echo "Identity: ${arn}"

exec npx --yes aws-cdk@latest "$@" --profile "${AWS_PROFILE}"
