"""Portable compiler checks; real SQL behavior is covered in the native suite."""
import sqlite3
import unittest

try:
    from eventdesk_storage.compiler import compile_sql
except ModuleNotFoundError as error:
    if error.name != 'sqlglot':
        raise
    compile_sql = None
from eventdesk_storage.schema import TABLES, validate_schema


@unittest.skipUnless(compile_sql, 'Install the optional SQL Server backend dependencies.')
class SqlServerCompilerTests(unittest.TestCase):
    def test_known_select_is_qualified_and_parameters_stay_bound(self):
        compiled = compile_sql('SELECT * FROM employees WHERE employee_id=?', ('0012345',))
        self.assertIn('[eventdesk].[employees]', compiled.sql)
        self.assertNotIn('0012345', compiled.sql)
        self.assertEqual(compiled.parameters, ('0012345',))

    def test_limit_parameter_order_is_preserved(self):
        compiled = compile_sql('SELECT id FROM employees ORDER BY id LIMIT ? OFFSET ?', (10, 20))
        self.assertEqual(compiled.parameters, (20, 10))

    def test_nested_query_parameters_follow_source_text_not_ast_walk_order(self):
        compiled = compile_sql(
            'SELECT (SELECT ? FROM employees WHERE id=?) AS value FROM employees WHERE employee_id=?',
            ('select-value', 7, '0000007'))
        self.assertEqual(compiled.parameters, ('select-value', 7, '0000007'))

    def test_cached_query_never_reuses_bound_data(self):
        first = compile_sql('SELECT name FROM employees WHERE employee_id=?', ('first',))
        second = compile_sql('SELECT name FROM employees WHERE employee_id=?', ('second',))
        self.assertEqual(first.parameters, ('first',))
        self.assertEqual(second.parameters, ('second',))

    def test_sqlite_boolean_sum_is_a_case_expression(self):
        compiled = compile_sql('SELECT SUM(checked_in_at IS NOT NULL) AS checked FROM employees')
        self.assertIn('CASE', compiled.sql)

    def test_nested_case_boolean_values_preserve_null_and_parameter_order(self):
        compiled = compile_sql('SELECT CASE WHEN ? THEN ?=1 ELSE EXISTS '
            '(SELECT 1 FROM reward_images WHERE prize_id=?) END AS value FROM reward_drafts', (1, None, 9))
        self.assertIn('WHEN ? <> 0 THEN CASE', compiled.sql)
        self.assertIn('ELSE NULL END', compiled.sql)
        self.assertEqual(compiled.parameters, (1, None, None, 9))

    def test_distinct_sort_wraps_without_adding_sort_values_to_distinct_rows(self):
        compiled = compile_sql('SELECT DISTINCT department AS dept FROM employees WHERE id>? '
            'ORDER BY dept COLLATE NOCASE LIMIT ? OFFSET ?', (4, 2, 1))
        self.assertIn('FROM (SELECT DISTINCT', compiled.sql)
        self.assertIn('CAST([department] AS VARBINARY(MAX)) AS [dept]', compiled.sql)
        self.assertIn('CAST([__ed_distinct].[dept] AS NVARCHAR(MAX))', compiled.sql)
        self.assertEqual(compiled.sql.count('COLLATE'), 1)
        self.assertEqual(compiled.parameters, (4, 1, 2))

    def test_delete_output_follows_the_target_table(self):
        compiled = compile_sql('DELETE FROM employees WHERE id=?', (4,))
        self.assertTrue(compiled.sql.startswith('DELETE FROM [eventdesk].[employees] OUTPUT [DELETED].[id]'))

    def test_upsert_can_reset_a_column_omitted_from_insert_values(self):
        compiled = compile_sql('''INSERT INTO lucky_draw_participants (employee_id,active,added_at)
            VALUES (?,1,?) ON CONFLICT(employee_id) DO UPDATE SET active=1,removed_at=NULL''', (1, 'time'))
        self.assertIn('[removed_at]=NULL', compiled.sql)

    def test_unsupported_mutation_clauses_fail_closed(self):
        for statement in ('DELETE FROM employees LIMIT 1',
                          'DELETE FROM employees ORDER BY id LIMIT 1',
                          "INSERT INTO event_settings(key,value_json,updated_at,updated_by) VALUES ('x','1','t','a') "
                          "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json WHERE 0"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.NotSupportedError):
                compile_sql(statement)

    def test_like_patterns_preserve_literal_brackets(self):
        compiled = compile_sql('SELECT name FROM employees WHERE name LIKE ?', ('[AB]%',))
        self.assertEqual(compiled.parameters, ('\\[AB]%',))
        self.assertIn("ESCAPE '\\'", compiled.sql)

    def test_large_in_lists_keep_other_bindings_and_pack_each_list_once(self):
        import xml.etree.ElementTree as ET
        first, second = list(range(1500)), list(range(1500, 3000))
        statement = ('SELECT id FROM employees WHERE department=? AND (id IN ('
                     + ','.join('?' for _ in first) + ') OR id IN (' + ','.join('?' for _ in second) + '))')
        compiled = compile_sql(statement, ('IT', *first, *second))
        self.assertEqual(compiled.parameters[2], 'IT')
        self.assertEqual([int(node.text) for node in ET.fromstring(compiled.parameters[0])], first)
        self.assertEqual([int(node.text) for node in ET.fromstring(compiled.parameters[1])], second)
        self.assertEqual(compiled.sql.count(".nodes('/r/v')"), 2)
        self.assertNotIn('OPENJSON', compiled.sql)

    def test_sql_server_2014_trim_and_scalar_minmax(self):
        compiled = compile_sql('SELECT trim(name),ltrim(name),rtrim(name),MAX(?,?),MIN(?,?) FROM employees',
                               (1, 2, 3, 4))
        self.assertIn('LTRIM(RTRIM([name]))', compiled.sql)
        for modern in (' TRIM(', 'GREATEST(', 'LEAST(', 'OPENJSON('):
            self.assertNotIn(modern, compiled.sql)

    def test_post_2014_functions_fail_closed(self):
        for sql in ("SELECT JSON_EXTRACT(extra_json,'$.position') FROM employees",
                    "SELECT TRANSLATE(name,'A','a') FROM employees", "SELECT STRING_AGG(name,',') FROM employees"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.NotSupportedError):
                compile_sql(sql)

    def test_xml_array_preserves_null_empty_unicode_cr_and_xml_metacharacters(self):
        import xml.etree.ElementTree as ET
        special = ['\u54e1\u5de5', '', None, '<>&\"\'\r\n\t', '__ED_ARRAY_0__', '\U00020000']
        values = ['unused'] * 2000 + special
        statement = 'SELECT id FROM employees WHERE employee_id IN (' + ','.join('?' for _ in values) + ')'
        compiled = compile_sql(statement, values)
        nodes = ET.fromstring(compiled.parameters[0])
        decoded = [None if node.attrib['is_null'] == '1' else node.text or '' for node in nodes]
        self.assertEqual(decoded, values)
        self.assertNotIn('<>&', compiled.parameters[0])
        self.assertIn('&#13;', compiled.parameters[0])

    def test_large_xml_arrays_reject_xml_invalid_characters_and_truncation(self):
        statement = 'SELECT id FROM employees WHERE employee_id IN (' + ','.join('?' for _ in range(2001)) + ')'
        for bad in ('NUL\x00ID', '\ud800', '\uffff', 'x' * 4001):
            with self.subTest(value=repr(bad)), self.assertRaises(sqlite3.NotSupportedError):
                compile_sql(statement, ['unused'] * 2000 + [bad])

    def test_large_in_list_rejects_nonfinite_and_nonscalar_values(self):
        statement = 'SELECT id FROM employees WHERE id IN (' + ','.join('?' for _ in range(2001)) + ')'
        for bad in (float('nan'), float('inf'), {'not': 'scalar'}):
            with self.subTest(value=bad), self.assertRaises(sqlite3.NotSupportedError):
                compile_sql(statement, (*range(2000), bad))

    def test_boolean_projection_and_correlated_aggregate_are_sql_server_compatible(self):
        projected = compile_sql('SELECT EXISTS(SELECT 1 FROM employees) AS has_data')
        self.assertIn('CASE WHEN EXISTS', projected.sql)
        aggregate = compile_sql('''SELECT COUNT(*) AS total,
            SUM(checked_in_at IS NOT NULL OR EXISTS
                (SELECT 1 FROM event_checkins c WHERE c.employee_id=employees.id)) AS checked
            FROM employees''')
        self.assertIn('FROM (SELECT CASE', aggregate.sql)
        self.assertIn('SUM([__ed_aggregate_0])', aggregate.sql)

    def test_unknown_table_and_multiple_statements_are_rejected(self):
        for sql in ('SELECT * FROM sys.tables', 'SELECT * FROM employees; DELETE FROM employees',
                    'DROP TABLE employees', 'EXEC sp_executesql ?'):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.NotSupportedError):
                compile_sql(sql)

    def test_schema_identifier_is_closed(self):
        self.assertEqual(validate_schema('eventdesk_test'), 'eventdesk_test')
        for schema in ('dbo.x', 'x];DROP TABLE x--', '', '1foo'):
            with self.assertRaises(ValueError):
                validate_schema(schema)

    def test_schema_contains_all_event_and_image_tables(self):
        self.assertIn('employees', TABLES)
        self.assertIn('_eventdesk_projector_images', TABLES)
        self.assertEqual(TABLES['employees']['primary_key'], ('id',))
        self.assertNotIn('EventOperationReceipt', TABLES)


if __name__ == '__main__':
    unittest.main()
