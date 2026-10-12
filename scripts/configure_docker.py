"""Generate Docker's local database password without changing the user's .env."""
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / '.env.docker'
try:
    with path.open('x',encoding='utf-8') as file:
        file.write('XBUDDY_POSTGRES_PASSWORD=' + secrets.token_hex(24) + '\n')
    print('Created .env.docker; credentials are not printed. Keep this file private.')
except FileExistsError:
    print('.env.docker already exists; left unchanged.')
