#!/usr/bin/env bash
# Generate backend/.env with fresh cryptographic secrets.
# Existing files are never overwritten: secrets rotation must be deliberate,
# because rotating TOKEN_ENCRYPTION_KEY makes stored tokens undecryptable.
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BACKEND_DIR}/.env"
EXAMPLE_FILE="${BACKEND_DIR}/.env.example"

if [[ -f "${ENV_FILE}" ]]; then
  echo "backend/.env уже существует, генерация пропущена."
  exit 0
fi

if [[ ! -f "${EXAMPLE_FILE}" ]]; then
  echo "Не найден ${EXAMPLE_FILE}" >&2
  exit 1
fi

TOKEN_ENCRYPTION_KEY="$(openssl rand -hex 32)"
JWT_ACCESS_SECRET="$(openssl rand -base64 48 | tr -d '\n=+/')"
MEDIA_URL_SECRET="$(openssl rand -base64 48 | tr -d '\n=+/')"
INIT_ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '\n=+/')Aa1"
# Exported so the substitution helper below can read them from the environment
# instead of receiving secrets as argv (visible in the process list).
export TOKEN_ENCRYPTION_KEY JWT_ACCESS_SECRET MEDIA_URL_SECRET INIT_ADMIN_PASSWORD

cp "${EXAMPLE_FILE}" "${ENV_FILE}"

python3 - "$ENV_FILE" <<'PYTHON'
import os
import re
import sys

path = sys.argv[1]
keys = (
    "TOKEN_ENCRYPTION_KEY",
    "JWT_ACCESS_SECRET",
    "MEDIA_URL_SECRET",
    "INIT_ADMIN_PASSWORD",
)

with open(path, "r", encoding="utf-8") as handle:
    content = handle.read()

for key in keys:
    value = os.environ[key]
    # Replacement is a plain string, so regex escapes in secrets stay literal.
    content = re.sub(rf"(?m)^{key}=.*$", lambda _m, v=value: f"{key}={v}", content)

with open(path, "w", encoding="utf-8") as handle:
    handle.write(content)
PYTHON

chmod 600 "${ENV_FILE}"

echo "Создан backend/.env с новыми секретами (права 600)."
echo
echo "Учетная запись администратора для первого входа:"
echo "  email:    $(grep '^INIT_ADMIN_EMAIL=' "${ENV_FILE}" | cut -d= -f2-)"
echo "  password: ${INIT_ADMIN_PASSWORD}"
echo
echo "Сохраните пароль в менеджере паролей: он больше не будет показан."
