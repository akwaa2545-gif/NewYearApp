"""SQL primary storage; metadata remains importable without native SQL packages."""
from .schema import (MIN_SQL_SERVER_COMPATIBILITY_LEVEL, MIN_SQL_SERVER_MAJOR_VERSION,
                     MIN_SQL_SERVER_YEAR, TABLES, initialize_schema)


def __getattr__(name):
    if name == 'SqlServerConnection':
        from .connection import SqlServerConnection
        return SqlServerConnection
    raise AttributeError(name)


__all__ = ['SqlServerConnection', 'TABLES', 'initialize_schema', 'MIN_SQL_SERVER_YEAR',
           'MIN_SQL_SERVER_MAJOR_VERSION', 'MIN_SQL_SERVER_COMPATIBILITY_LEVEL']
