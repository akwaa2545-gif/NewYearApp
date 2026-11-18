"""Deliberate SQLite-query compatibility for the app's private native SQL driver.

This module is not an HTTP SQL endpoint. Only application-owned tables and the
SELECT/INSERT/UPDATE/DELETE subset are accepted; unsupported syntax fails closed.
Values remain positional ODBC bindings, including when T-SQL reorders pagination.
"""
from dataclasses import dataclass
from functools import lru_cache
from html import escape
import math
import re
import sqlite3

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from .schema import TABLES, validate_schema


_PARAMETER = re.compile(r'__ED_PARAM_(\d+)__')
_BINDING = re.compile(r'__ED_(PARAM|ARRAY)_(\d+)__')
_POST_2014_FUNCTIONS = frozenset((
    'OPENJSON', 'JSON_VALUE', 'JSON_QUERY', 'JSON_MODIFY', 'ISJSON', 'JSON_OBJECT',
    'JSON_ARRAY', 'JSON_ARRAYAGG', 'JSON_OBJECTAGG', 'STRING_AGG', 'STRING_SPLIT',
    'TRANSLATE', 'TRIM', 'GREATEST', 'LEAST', 'CONCAT_WS', 'DATETRUNC', 'DATE_BUCKET',
))


@dataclass(frozen=True)
class Compiled:
    sql: str
    parameters: tuple
    kind: str
    table: str | None = None
    ignore: bool = False


def _unsupported(message):
    raise sqlite3.NotSupportedError(message)


def _case_condition(condition):
    return exp.Case(ifs=[exp.If(this=condition, true=exp.Literal.number(1))],
                    default=exp.Literal.number(0))


def _predicate(value):
    if isinstance(value, (exp.Paren, exp.Escape)):
        return _predicate(value.this)
    return isinstance(value, (exp.Predicate, exp.And, exp.Or, exp.Not, exp.Exists))


def _case_value(value):
    if not _predicate(value):
        return value
    # SQLite scalar comparisons retain NULL; EXISTS itself is never NULL.
    if isinstance(value, exp.Exists):
        return _case_condition(value.copy())
    return exp.Case(ifs=[exp.If(this=value.copy(), true=exp.Literal.number(1)),
                        exp.If(this=exp.Not(this=value.copy()), true=exp.Literal.number(0))],
                    default=exp.Null())


def _normalize_cases(tree):
    for case in reversed(list(tree.find_all(exp.Case))):
        for branch in case.args.get('ifs', []):
            if case.this is None and not _predicate(branch.this):
                branch.set('this', exp.NEQ(this=branch.this.copy(), expression=exp.Literal.number(0)))
            branch.set('true', _case_value(branch.args['true']))
        if case.args.get('default') is not None:
            case.set('default', _case_value(case.args['default']))


def _distinct_order(tree):
    # SQL Server requires DISTINCT's ORDER BY expressions in its projection.
    # Sort the already-distinct rows outside, preserving the original row set.
    for select in reversed(list(tree.find_all(exp.Select))):
        if not select.args.get('distinct') or not select.args.get('order'):
            continue
        names = [value.alias_or_name for value in select.expressions]
        if not all(names) or len(set(names)) != len(names) or any(
                not isinstance(value, (exp.Column, exp.Alias)) for value in select.expressions):
            _unsupported('DISTINCT ordering requires named, unique result columns.')
        order = select.args['order'].copy()
        for column in order.find_all(exp.Column):
            if column.name not in names:
                _unsupported('DISTINCT ordering must use a selected result column.')
            column.set('table', exp.to_identifier('__ed_distinct'))
        inner = select.copy()
        inner.set('order', None)
        aliases = {table.alias_or_name: table.name for table in inner.find_all(exp.Table)}
        source = inner.args.get('from_')
        binary_names = set()
        for projected in list(inner.expressions):
            column = projected.this if isinstance(projected, exp.Alias) else projected
            if not isinstance(column, exp.Column):
                continue
            table = aliases.get(column.table) if column.table else (
                source.this.name if source and isinstance(source.this, exp.Table) else None)
            info = next((value for value in TABLES.get(table, {}).get('column_info', [])
                         if value['name'] == column.name), None)
            if info and info['type'] == 'TEXT':
                # SQL Server 2014 rejects an outer COLLATE over an explicitly
                # collated derived column (445). Binary DISTINCT also retains
                # trailing spaces that SQL text equality would otherwise fold.
                binary = exp.Cast(this=column.copy(), to=exp.DataType.build('VARBINARY(MAX)', dialect='tsql'))
                binary_names.add(projected.alias_or_name)
                # Binary comparisons pad with zero bytes. Include length so a
                # trailing NUL remains distinct from shorter text (and empty).
                length_name = '__ed_distinct_length_' + str(len(binary_names))
                while length_name in names:
                    length_name += '_'
                inner.append('expressions', exp.alias_(exp.Anonymous(this='DATALENGTH',
                    expressions=[column.copy()]), length_name))
                if isinstance(projected, exp.Alias):
                    projected.set('this', binary)
                else:
                    column.replace(exp.alias_(binary, column.name))
        def result_column(name):
            value = exp.column(name, table='__ed_distinct')
            return exp.Cast(this=value, to=exp.DataType.build('NVARCHAR(MAX)', dialect='tsql')) if name in binary_names else value
        for column in list(order.find_all(exp.Column)):
            if column.name in binary_names:
                column.replace(result_column(column.name))
        outer = exp.Select(expressions=[exp.alias_(result_column(name), name) if name in binary_names
                                       else result_column(name) for name in names],
            from_=exp.From(this=exp.Subquery(this=inner,
                alias=exp.TableAlias(this=exp.to_identifier('__ed_distinct')))), order=order)
        for key in ('limit', 'offset'):
            if inner.args.get(key) is not None:
                outer.set(key, inner.args[key])
                inner.set(key, None)
        if select is tree:
            tree = outer
        else:
            select.replace(outer)
    return tree


def _hoist_aggregate_subqueries(tree):
    """T-SQL forbids SUM(CASE ... EXISTS(subquery)); project the value first."""
    for select in list(tree.find_all(exp.Select)):
        sums = [total for total in select.find_all(exp.Sum)
                if total.find_ancestor(exp.Select) is select and total.this.find(exp.Exists)]
        if not sums:
            continue
        if select.args.get('group') or select.args.get('having'):
            _unsupported('Grouped aggregates containing subqueries require an explicit repository query.')
        inner = exp.Select(expressions=[])
        for key in ('from_', 'joins', 'where'):
            value = select.args.get(key)
            if value is not None:
                inner.set(key, [item.copy() for item in value] if isinstance(value, list) else value.copy())
                select.set(key, None)
        for index, total in enumerate(sums):
            name = f'__ed_aggregate_{index}'
            inner.append('expressions', exp.alias_(total.this.copy(), name))
            total.set('this', exp.column(name))
        select.set('from_', exp.From(this=exp.Subquery(this=inner,
            alias=exp.TableAlias(this=exp.to_identifier('__ed_aggregate')))))
    return tree


def _escape_like(value, escape, had_escape):
    if value is None:
        return None
    value = str(value)
    result, index = [], 0
    while index < len(value):
        character = value[index]
        if had_escape and character == escape and index + 1 < len(value):
            result.append(value[index:index + 2])
            index += 2
            continue
        if character == '[' or not had_escape and character == escape:
            result.append(escape)
        result.append(character)
        index += 1
    return ''.join(result)


def _transform(tree, schema):
    like_bindings = {}
    for table in tree.find_all(exp.Table):
        if table.name not in TABLES or table.db or table.catalog:
            _unsupported('Only fixed EventDesk tables are supported.')
        table.set('db', exp.to_identifier(schema, quoted=True))
    for literal in tree.find_all(exp.Literal):
        if '__ED_PARAM_' in str(literal.this):
            _unsupported('Reserved parameter marker in SQL literal.')
    for placeholder in list(tree.find_all(exp.Placeholder)):
        match = _PARAMETER.fullmatch(str(placeholder.this or ''))
        if not match:
            _unsupported('Only positional SQL bindings are supported.')
        placeholder.replace(exp.Var(this=match.group(0)))
    for like in list(tree.find_all(exp.Like)):
        parent = like.parent
        had_escape = isinstance(parent, exp.Escape)
        escape = parent.expression.this if had_escape and isinstance(parent.expression, exp.Literal) else '\\'
        if had_escape and (not isinstance(parent.expression, exp.Literal) or len(escape) != 1):
            _unsupported('LIKE requires a literal, single-character escape.')
        pattern = like.expression.this if isinstance(like.expression, exp.Collate) else like.expression
        marker = _PARAMETER.fullmatch(pattern.this) if isinstance(pattern, exp.Var) else None
        if marker:
            like_bindings[int(marker.group(1))] = (escape, had_escape)
        elif isinstance(pattern, exp.Literal) and pattern.is_string:
            pattern.set('this', _escape_like(pattern.this, escape, had_escape))
        else:
            _unsupported('LIKE patterns must be literal or bound values.')
        if not had_escape:
            like.replace(exp.Escape(this=like.copy(), expression=exp.Literal.string(escape)))
    for collation in tree.find_all(exp.Collate):
        if collation.expression.name.upper() != 'NOCASE':
            _unsupported('Unsupported SQLite collation.')
        collation.set('expression', exp.Var(this='Latin1_General_100_CI_AS'))
    for glob in list(tree.find_all(exp.Glob)):
        if not isinstance(glob.expression, exp.Literal) or glob.expression.this != '*[^0-9]*':
            _unsupported('Unsupported SQLite GLOB pattern.')
        glob.replace(exp.Like(this=exp.Collate(this=glob.this.copy(),
            expression=exp.Var(this='Latin1_General_100_BIN2')), expression=exp.Literal.string('%[^0-9]%')))
    _normalize_cases(tree)
    for total in tree.find_all(exp.Sum):
        if _predicate(total.this):
            total.set('this', _case_value(total.this.copy()))
    tree = _hoist_aggregate_subqueries(tree)
    for select in tree.find_all(exp.Select):
        for projected in select.expressions:
            value = projected.this if isinstance(projected, exp.Alias) else projected
            if _predicate(value):
                case = _case_value(value.copy())
                if isinstance(projected, exp.Alias):
                    projected.set('this', case)
                else:
                    projected.replace(case)
    for greatest in list(tree.find_all((exp.Greatest, exp.Least))):
        values = [greatest.this, *greatest.expressions]
        if len(values) != 2:
            _unsupported('Only two-argument scalar MIN/MAX is supported.')
        left, right = values
        greatest.replace(exp.Case(ifs=[
            exp.If(this=exp.Or(this=exp.Is(this=left.copy(), expression=exp.Null()),
                              expression=exp.Is(this=right.copy(), expression=exp.Null())), true=exp.Null()),
            exp.If(this=(exp.GT if isinstance(greatest, exp.Greatest) else exp.LT)(
                this=left.copy(), expression=right.copy()), true=left.copy())],
            default=right.copy()))
    for trim in list(tree.find_all(exp.Trim)):
        if trim.expression is None:
            if trim.args.get('position') in (None, 'BOTH'):
                trim.set('this', exp.Trim(this=trim.this.copy(), position='TRAILING'))
                trim.set('position', 'LEADING')
            continue
        if trim.args.get('position') != 'LEADING' or trim.expression.this != '0':
            _unsupported('Unsupported character-trimming expression.')
        value = trim.this.sql('tsql', identify=True)
        # PATINDEX sentinel handles an all-zero ID without numeric conversion or overflow.
        replacement = sqlglot.parse_one(
            f"SUBSTRING({value}, PATINDEX('%[^0]%', {value} + '.'), LEN({value}))", read='tsql')
        trim.replace(replacement)
    # Neither parser success nor generator warnings are validation. Reject dialect
    # constructs left over after the specifically supported transformations.
    if tree.find(exp.Command) or tree.find(exp.Pragma) or tree.find(exp.Glob):
        _unsupported('Unsupported SQL command.')
    return _distinct_order(tree), like_bindings


def _sql(tree):
    return tree.sql('tsql', identify=True, unsupported_level=sqlglot.ErrorLevel.RAISE)


def _in_type(node, tree):
    column = node.this.this if isinstance(node.this, exp.Collate) else node.this
    if not isinstance(column, exp.Column):
        _unsupported('Large IN lists require a fixed event column.')
    aliases = {table.alias_or_name: table.name for table in tree.find_all(exp.Table)}
    table = aliases.get(column.table)
    if not table:
        select = node.find_ancestor(exp.Select)
        source = select.args.get('from_').this if select and select.args.get('from_') else tree.this
        table = source.name if isinstance(source, exp.Table) else None
    if table not in TABLES:
        _unsupported('Cannot resolve the event column for a large IN list.')
    info = next((item for item in TABLES[table]['column_info'] if item['name'] == column.name), None)
    if info is None or info['type'] not in ('TEXT', 'INTEGER'):
        _unsupported('Unsupported column type for a large IN list.')
    return info['type'], column.name


def _compact_in(tree, parameter_count):
    groups = []
    if parameter_count <= 1800:
        return tree, groups
    for node in list(tree.find_all(exp.In)):
        if node.args.get('query') or len(node.expressions) < 2:
            continue
        markers = [_PARAMETER.fullmatch(value.this) if isinstance(value, exp.Var) else None
                   for value in node.expressions]
        if not all(markers):
            continue
        kind, name = _in_type(node, tree)
        index = len(groups)
        groups.append((tuple(int(marker.group(1)) for marker in markers), kind))
        sql_type = 'bigint' if kind == 'INTEGER' else 'nvarchar(4000)'
        value = f"[__ed_n].[v].value('(text())[1]', '{sql_type}')"
        if kind == 'TEXT':
            value = 'COALESCE(' + value + ", N'')"
        selected = ("CASE WHEN [__ed_n].[v].value('(@is_null)[1]', 'int')=1 THEN NULL ELSE "
                    + value + ' END')
        if kind == 'TEXT':
            selected += ' COLLATE ' + ('Latin1_General_100_CI_AS' if name in ('name', 'username')
                                      else 'Latin1_General_100_BIN2')
        # This fixed fragment references one bound XML variable. Parsing the XML
        # once avoids repeatedly converting a document for each candidate row.
        query = exp.Var(this=(f'SELECT {selected} FROM '
            f"@__ed_xml_{index}.nodes('/r/v') AS [__ed_n]([v])"))
        node.set('expressions', [])
        node.set('query', exp.Subquery(this=query))
    return tree, groups


def _xml_array(values, kind):
    pieces, length = ['<r>'], 7
    for value in values:
        if value is None:
            piece = '<v is_null="1">0</v>' if kind == 'INTEGER' else '<v is_null="1"/>'
        else:
            text = str(value)
            if any(not (code in (9, 10, 13) or 0x20 <= code <= 0xD7FF
                        or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF)
                   for code in map(ord, text)):
                _unsupported('Large IN values must contain valid XML 1.0 characters.')
            if len(text.encode('utf-16-le')) // 2 > 4000:
                _unsupported('A large IN value exceeds the non-truncating SQL XML limit.')
            # XML newline normalization would otherwise replace CR with LF.
            encoded = escape(text, quote=True).replace('\r', '&#13;')
            piece = '<v is_null="0">' + encoded + '</v>'
        length += len(piece.encode('utf-8'))
        if length > 8 * 1024 * 1024:
            _unsupported('Large IN bindings exceed the event request size limit.')
        pieces.append(piece)
    pieces.append('</r>')
    return ''.join(pieces)


def _assert_sql2014(rendered):
    tokens = sqlglot.Tokenizer(dialect='tsql').tokenize(rendered)
    for index, token in enumerate(tokens[:-1]):
        if token.token_type not in (TokenType.STRING, TokenType.IDENTIFIER):
            word = token.text.upper()
            if word in _POST_2014_FUNCTIONS and tokens[index + 1].text == '(':
                _unsupported('This SQL function is not available on SQL Server 2014.')
            if word == 'FOR' and tokens[index + 1].text.upper() == 'JSON':
                _unsupported('SQL Server 2014 does not support FOR JSON.')


def _table(tree):
    target = tree.this.this if isinstance(tree, exp.Insert) and isinstance(tree.this, exp.Schema) else tree.this
    if not isinstance(target, exp.Table):
        _unsupported('A fixed target table is required.')
    return target.name


def _upsert(tree, table, schema):
    if not isinstance(tree.this, exp.Schema) or not isinstance(tree.expression, exp.Values):
        _unsupported('Upsert requires explicit columns and one VALUES row.')
    if len(tree.expression.expressions) != 1:
        _unsupported('Multirow upserts are not supported.')
    columns = [column.name for column in tree.this.expressions]
    if any(column not in TABLES[table]['columns'] for column in columns):
        _unsupported('Unknown upsert column.')
    conflict = tree.args['conflict']
    if conflict.args.get('where') or conflict.args.get('index_predicate'):
        _unsupported('Conditional conflict clauses require an explicit repository query.')
    keys = [key.this.name for key in conflict.args.get('conflict_keys', [])]
    if not keys or any(key not in columns for key in keys):
        _unsupported('An explicit supported conflict key is required.')
    values = ', '.join(_sql(value) for value in tree.expression.expressions[0].expressions)
    quoted = ', '.join('[' + column + ']' for column in columns)
    match = ' AND '.join('target.[' + key + ']=source.[' + key + ']' for key in keys)
    updates = []
    action = conflict.args.get('action')
    if action and action.name == 'DO UPDATE':
        for assignment in conflict.expressions:
            if not isinstance(assignment, exp.EQ) or assignment.this.name not in TABLES[table]['columns']:
                _unsupported('Unsupported upsert assignment.')
            value = assignment.expression.copy()
            for column in value.find_all(exp.Column):
                column.set('table', exp.to_identifier('source' if column.table == 'excluded' else 'target'))
            updates.append('[' + assignment.this.name + ']=' + _sql(value))
    elif not action or action.name != 'DO NOTHING':
        _unsupported('Unsupported conflict action.')
    matched = ' WHEN MATCHED THEN UPDATE SET ' + ', '.join(updates) if updates else ''
    output = ','.join('inserted.[' + key + ']' for key in TABLES[table]['primary_key'])
    return (f'MERGE [{schema}].[{table}] WITH (HOLDLOCK) AS target '
            f'USING (VALUES ({values})) AS source ({quoted}) ON {match}' + matched
            + f' WHEN NOT MATCHED THEN INSERT ({quoted}) VALUES ('
            + ','.join('source.[' + column + ']' for column in columns) + ') OUTPUT ' + output + ';')


@lru_cache(maxsize=512)
def _template(statement, schema):
    schema = validate_schema(schema)
    if '__ED_PARAM_' in statement or '__ED_ARRAY_' in statement:
        _unsupported('Reserved SQL parameter marker.')
    tokens = sqlglot.Tokenizer(dialect='sqlite').tokenize(statement)
    placeholders = [token for token in tokens if token.token_type == TokenType.PLACEHOLDER]
    pieces, offset = [], 0
    for index, token in enumerate(placeholders):
        if token.text != '?':
            _unsupported('Only positional SQL bindings are supported.')
        pieces.extend((statement[offset:token.start], f':__ED_PARAM_{index}__'))
        offset = token.end + 1
    pieces.append(statement[offset:])
    try:
        trees = sqlglot.parse(''.join(pieces), read='sqlite')
    except sqlglot.errors.SqlglotError as error:
        raise sqlite3.NotSupportedError('Unsupported event SQL.') from error
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Insert, exp.Update, exp.Delete)):
        _unsupported('Only one application query or mutation is permitted.')
    tree = trees[0]
    table = None if isinstance(tree, exp.Select) else _table(tree)
    if table is not None and table not in TABLES:
        _unsupported('Unknown event table.')
    tree, like_bindings = _transform(tree, schema)
    tree, groups = _compact_in(tree, len(placeholders))
    kind = type(tree).__name__.lower()
    ignore = isinstance(tree, exp.Insert) and tree.args.get('alternative') == 'IGNORE'
    if isinstance(tree, exp.Insert) and tree.args.get('conflict'):
        rendered = _upsert(tree, table, schema)
    elif isinstance(tree, exp.Delete):
        if any(tree.args.get(key) for key in ('using', 'tables', 'order', 'limit', 'offset', 'returning')):
            _unsupported('Unsupported delete clause.')
        output = ', '.join('[DELETED].[' + key + ']' for key in TABLES[table]['primary_key'])
        where = ' ' + _sql(tree.args['where']) if tree.args.get('where') else ''
        rendered = 'DELETE FROM ' + _sql(tree.this) + ' OUTPUT ' + output + where
    else:
        if ignore:
            tree.set('alternative', None)
        if table:
            output_table = 'DELETED' if isinstance(tree, exp.Delete) else 'INSERTED'
            tree.set('returning', exp.Returning(expressions=[exp.column(key, table=output_table)
                      for key in TABLES[table]['primary_key']]))
        rendered = _sql(tree)
    if groups:
        rendered = ''.join(f'DECLARE @__ed_xml_{index} XML=CONVERT(XML, __ED_ARRAY_{index}__, 1); '
                           for index in range(len(groups))) + rendered
    positions = [(match.group(1), int(match.group(2))) for match in _BINDING.finditer(rendered)]
    if len(positions) > 2100:
        _unsupported('This event query exceeds the SQL Server bound-parameter limit.')
    rendered = _BINDING.sub('?', rendered)
    _assert_sql2014(rendered)
    return rendered, tuple(positions), len(placeholders), kind, table, ignore, tuple(like_bindings.items()), tuple(groups)


def compile_sql(statement, parameters=(), schema='eventdesk'):
    rendered, positions, count, kind, table, ignore, like_bindings, groups = _template(statement, schema)
    if count != len(parameters):
        raise sqlite3.ProgrammingError('SQL parameter count does not match bindings.')
    escapes = dict(like_bindings)
    arrays = []
    for indexes, value_kind in groups:
        values = [parameters[index] for index in indexes]
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))
               or isinstance(value, float) and not math.isfinite(value)
               or isinstance(value, int) and not -(2**63) <= value < 2**63) for value in values):
            _unsupported('Large IN lists require finite scalar bindings.')
        if value_kind == 'INTEGER' and any(value is not None and type(value) is not int for value in values):
            _unsupported('Large integer-ID lists require integer bindings.')
        arrays.append(_xml_array(values, value_kind))
    rebound = tuple(arrays[index] if binding == 'ARRAY' else
                    _escape_like(parameters[index], *escapes[index]) if index in escapes else parameters[index]
                    for binding, index in positions)
    return Compiled(rendered, rebound, kind, table, ignore)
