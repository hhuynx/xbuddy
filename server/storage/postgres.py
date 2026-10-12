"""Small dialect adapter for the application's fixed, parameterized SQL statements."""
import re


def translate(sql):
    # Preserve question marks in quoted SQL literals; all data remain bound parameters.
    parts = re.split(r"('(?:''|[^'])*')", sql)
    sql = ''.join(p if i % 2 else p.replace('?', '%s') for i, p in enumerate(parts))
    # psycopg parses percent characters when parameters are supplied, including LIKE literals.
    sql = re.sub(r'%(?!s)', '%%', sql)
    if sql.strip().upper() == 'BEGIN IMMEDIATE':
        # Match SQLite's single-writer critical sections across Web and Worker processes.
        return 'SELECT pg_advisory_xact_lock(82491701)'
    sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
    sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql)
    sql = re.sub(r"strftime\([^;]+?,'now'\)", 'CAST(CURRENT_TIMESTAMP AS TEXT)', sql)
    if 'INSERT OR IGNORE INTO' in sql:
        sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO').rstrip(';') + ' ON CONFLICT DO NOTHING'
    if 'INSERT OR REPLACE INTO tool_capabilities' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO') + (
            ' ON CONFLICT(name) DO UPDATE SET provider=excluded.provider,'
            'provider_fingerprint=excluded.provider_fingerprint,status=excluded.status,'
            'details_json=excluded.details_json,checked_at=excluded.checked_at')
    return sql


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        return self.connection.execute(translate(sql), parameters)

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)
