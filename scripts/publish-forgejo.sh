#!/usr/bin/env bash
set -euo pipefail

repo_name="${1:-local-agent-delegate}"
base_url="${FORGEJO_BASE_URL:?FORGEJO_BASE_URL is required, for example https://forgejo.example.com}"
owner="${FORGEJO_OWNER:?FORGEJO_OWNER is required}"

if [ -z "${FORGEJO_TOKEN:-}" ]; then
  printf 'FORGEJO_TOKEN is required\n' >&2
  exit 1
fi

curl -fsS \
  -H "Authorization: token ${FORGEJO_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST \
  -d "{\"name\":\"${repo_name}\",\"private\":false,\"auto_init\":false}" \
  "${base_url}/api/v1/user/repos" >/dev/null || true

git remote remove origin 2>/dev/null || true
git remote add origin "${base_url}/${owner}/${repo_name}.git"
git -c "http.extraHeader=Authorization: token ${FORGEJO_TOKEN}" push -u origin main
