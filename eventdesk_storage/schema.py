"""Fixed native SQL Server schema for the EventDesk business tables."""
import json
from importlib.resources import files
import re


MIN_SQL_SERVER_YEAR = 2014
MIN_SQL_SERVER_MAJOR_VERSION = 12
MIN_SQL_SERVER_COMPATIBILITY_LEVEL = 120


def validate_schema(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value):
        raise ValueError('Invalid event database schema.')
    return value


_CATALOG = json.loads(files(__package__).joinpath('table_catalog.json').read_text(encoding='utf-8'))
TABLES = {
    name: {**entry, 'columns': tuple(c['name'] for c in entry['column_info']),
           'primary_key': tuple(c['name'] for c in entry['column_info'] if c['pk']),
           'identity': 'id' if any(c['name'] == 'id' and c['pk'] for c in entry['column_info']) else None}
    for name, entry in _CATALOG.items()
}


def _type(table, column):
    if column['type'] == 'INTEGER':
        return 'BIGINT'
    if column['type'] == 'BLOB':
        return 'VARBINARY(MAX)'
    name = column['name']
    if name == 'employee_id':
        return 'NVARCHAR(128) COLLATE Latin1_General_100_BIN2'
    if name in ('qr_token', 'token', 'session_id', 'image_id'):
        return 'NVARCHAR(128) COLLATE Latin1_General_100_BIN2'
    indexed = {key for ix in TABLES[table]['indexes'] for key in ix['columns']}
    if column['pk'] or name in indexed:
        collation = 'Latin1_General_100_CI_AS' if name in ('name', 'username') else 'Latin1_General_100_BIN2'
        return 'NVARCHAR(400) COLLATE ' + collation
    return 'NVARCHAR(MAX)'


def schema_statements(schema='eventdesk'):
    """Yield only trusted DDL. Caller-selected names pass a closed identifier rule."""
    schema = validate_schema(schema)
    yield "IF SCHEMA_ID(N'" + schema + "') IS NULL EXEC(N'CREATE SCHEMA [" + schema + "]')"
    for table, entry in TABLES.items():
        definitions = []
        for column in entry['column_info']:
            identity = ' IDENTITY(1,1)' if column['name'] == entry['identity'] else ''
            null = ' NOT NULL' if column['notnull'] or column['pk'] else ' NULL'
            default = '' if column['default'] is None else ' DEFAULT ' + column['default']
            definitions.append('[' + column['name'] + '] ' + _type(table, column) + identity + null + default)
        definitions.append('PRIMARY KEY (' + ','.join('[' + key + ']' for key in entry['primary_key']) + ')')
        for check in entry.get('checks', []):
            definitions.append('CHECK (' + check + ')')
        yield (f"IF OBJECT_ID(N'[{schema}].[{table}]', N'U') IS NULL CREATE TABLE [{schema}].[{table}] ("
               + ',\n'.join(definitions) + ')')
    for table, entry in TABLES.items():
        for number, index in enumerate(entry['indexes']):
            if not index['columns'] or index['columns'] == list(entry['primary_key']):
                continue
            name = f'ed_{table}_{number}'
            unique = 'UNIQUE ' if index['unique'] else ''
            columns = ','.join('[' + key + ']' for key in index['columns'])
            where = index.get('where')
            nullable = {c['name'] for c in entry['column_info'] if not c['notnull'] and not c['pk']}
            if index['unique'] and any(key in nullable for key in index['columns']):
                nonnull = ' AND '.join('[' + key + '] IS NOT NULL' for key in index['columns'])
                where = '(' + where + ') AND ' + nonnull if where else nonnull
            suffix = ' WHERE ' + where if where else ''
            yield (f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[{schema}].[{table}]')"
                   f" AND name=N'{name}') CREATE {unique}INDEX [{name}] ON [{schema}].[{table}] ({columns}){suffix}")
        for number, foreign in enumerate(entry['foreign_keys']):
            _, _, target, source_key, target_key, *_ = foreign
            name = f'ed_fk_{table}_{number}'
            yield (f"IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[{schema}].[{table}]')"
                   f" AND name=N'{name}') ALTER TABLE [{schema}].[{table}] ADD CONSTRAINT [{name}]"
                   f" FOREIGN KEY ([{source_key}]) REFERENCES [{schema}].[{target}] ([{target_key}])")
    yield f"""IF OBJECT_ID(N'[{schema}].[EventRevision]', N'U') IS NULL
        CREATE TABLE [{schema}].[EventRevision] (singleton INT NOT NULL PRIMARY KEY CHECK(singleton=1), revision BIGINT NOT NULL);
        IF NOT EXISTS (SELECT 1 FROM [{schema}].[EventRevision] WHERE singleton=1)
        INSERT INTO [{schema}].[EventRevision] VALUES (1,0)"""
    yield f"""IF OBJECT_ID(N'[{schema}].[EventOperationReceipt]', N'U') IS NULL
        CREATE TABLE [{schema}].[EventOperationReceipt] (
            operation_id VARCHAR(36) NOT NULL PRIMARY KEY, request_hash CHAR(64) NOT NULL,
            response_json NVARCHAR(MAX) NOT NULL, revision BIGINT NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())"""
    yield f"""IF OBJECT_ID(N'[{schema}].[EventChangeJournal]', N'U') IS NULL
        CREATE TABLE [{schema}].[EventChangeJournal] (
            revision BIGINT NOT NULL PRIMARY KEY, changes_json NVARCHAR(MAX) NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())"""
    yield f"""IF OBJECT_ID(N'[{schema}].[EventMigrationReceipt]', N'U') IS NULL
        CREATE TABLE [{schema}].[EventMigrationReceipt] (
            source_sha256 CHAR(64) NOT NULL PRIMARY KEY, manifest_json NVARCHAR(MAX) NOT NULL,
            applied_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())"""


def initialize_schema(raw_connection, schema='eventdesk'):
    capabilities = raw_connection.cursor().execute("""SELECT
        CONVERT(varchar(128), SERVERPROPERTY('ProductVersion')),
        compatibility_level FROM sys.databases WHERE database_id=DB_ID()""").fetchone()
    if (not capabilities or int(str(capabilities[0]).split('.')[0]) < MIN_SQL_SERVER_MAJOR_VERSION
            or int(capabilities[1]) < MIN_SQL_SERVER_COMPATIBILITY_LEVEL):
        raise RuntimeError('SQL Server 2014 and database compatibility level 120 or newer are required.')
    for statement in schema_statements(schema):
        raw_connection.cursor().execute(statement)
    raw_connection.commit()
