"""Conservative MySQL read-only boundary, shared by every SQL entry point.

This is defense in depth; database grants must still be read-only. Queries can
consume resources, and views may call stored functions outside this parser.
"""
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.tokens import TokenType

# Deliberately small: unfamiliar functions fail closed rather than running UDFs.
SAFE_FUNCTIONS = frozenset({
    'AND', 'OR', 'CASE', 'ABS', 'AVG', 'COUNT', 'SUM', 'MIN', 'MAX', 'COALESCE', 'IF', 'NULLIF',
    'LOWER', 'UPPER', 'LENGTH', 'CHAR_LENGTH', 'CONCAT', 'CONCAT_WS',
    'SUBSTRING', 'TRIM', 'ROUND', 'FLOOR', 'CEIL', 'CAST', 'TRY_CAST',
    'EXTRACT', 'YEAR', 'MONTH', 'DAY', 'DATE', 'DATEDIFF', 'DATE_DIFF',
    'CURRENT_DATE', 'CURRENT_TIMESTAMP', 'CURRENT_TIME',
    'TIME_TO_STR', 'TS_OR_DS_TO_TIMESTAMP', 'TS_OR_DS_TO_DATE',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LAG', 'LEAD',
})
SAFE_ANONYMOUS = frozenset({'NOW', 'DATABASE', 'VERSION'})
FORBIDDEN_NODES = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock,
                   exp.Parameter, exp.SessionParameter, exp.PropertyEQ)


def validate_read_only(sql: str, *, allow_metadata: bool = False) -> str:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 100_000:
        raise ValueError('SQL must contain one bounded read-only statement.')
    # Reject executable comments and mode-dependent escapes. Do not forward
    # original comments/text after parsing (including optimizer hints).
    if re.search(r'/\*\s*(?:!|M!)', sql, re.I) or '\\' in sql:
        raise ValueError('Executable comments and backslash escapes are not allowed.')
    try:
        tokens = sqlglot.Dialect.get_or_raise('mysql').tokenize(sql)
        for left, right in zip(tokens, tokens[1:]):
            if left.token_type == TokenType.IDENTIFIER and right.token_type == TokenType.L_PAREN:
                raise ValueError('Quoted function calls are not allowed.')
        trees = sqlglot.parse(sql, read='mysql', error_level=ErrorLevel.RAISE)
        if len(trees) != 1 or trees[0] is None:
            raise ValueError('Exactly one SQL statement is required.')
        tree = trees[0]
        if allow_metadata and isinstance(tree, exp.Show):
            # Only internally generated, exact metadata commands are accepted.
            if not re.fullmatch(r'SHOW DATABASES|SHOW CREATE TABLE `[\w$-]+`\.`[\w$-]+`', sql, re.ASCII):
                raise ValueError('Unsupported metadata statement.')
            return sql
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ValueError('Only SELECT queries are allowed.')
        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise ValueError('SQL writes, variables and locks are not allowed.')
            if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
                raise ValueError('Qualified function calls are not allowed.')
            if isinstance(node, exp.Func):
                if isinstance(node, exp.Anonymous):
                    permitted = node.name.upper() in SAFE_ANONYMOUS
                else:
                    permitted = node.sql_name() in SAFE_FUNCTIONS
                if not permitted:
                    raise ValueError('Function is outside the read-only allowlist.')
        # Generate only the parsed, inspected tree; no unparsed SQL reaches DMC.
        return tree.sql(dialect='mysql', comments=False, unsupported_level=ErrorLevel.RAISE)
    except SqlglotError:
        # Parser errors can contain SQL literals; don't expose them in MCP logs.
        raise ValueError('SQL could not be safely parsed as a read-only query.') from None


def metadata_identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_$-]+', value):
        raise ValueError('Unsupported metadata identifier.')
    return value


def sql_literal(value: str) -> str:
    # Backslashes are ambiguous across MySQL SQL modes and are rejected.
    if '\\' in value or '\x00' in value:
        raise ValueError('Unsupported metadata filter.')
    return "'" + value.replace("'", "''") + "'"
