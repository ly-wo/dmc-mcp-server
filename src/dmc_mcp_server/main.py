from __future__ import annotations

import threading
from functools import wraps

from mcp.server.fastmcp import FastMCP

from .client import DMCClient, InstanceCredentials
from .credentials import load_credentials
from .sql_policy import validate_read_only

mcp = FastMCP("dmc-mcp-server")

_client: DMCClient | None = None
_profiles: dict[str, InstanceCredentials] = {}
_state_lock = threading.RLock()


def _serialize_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _state_lock:
            return fn(*args, **kwargs)
    return wrapped


@_serialize_auth
def _get_client() -> DMCClient:
    if _client is None:
        reload_credentials()
    assert _client is not None
    return _client


def _validate_select_only(sql: str) -> str:
    return validate_read_only(sql)


@mcp.tool()
@_serialize_auth
def reload_credentials() -> str:
    """Reload the private local DMC_CREDENTIALS_FILE. No secret arguments.

    This clears all sessions, including on failure. Edit the file outside chat;
    never paste cookies, passwords or tokens into a tool or conversation.
    """
    global _client, _profiles
    if _client is not None:
        _client.update_cookie('', 0)
        _client._http.close()
    _client = None
    _profiles = {}
    try:
        cookie, csrf, profiles = load_credentials()
    except (OSError, ValueError):
        raise ValueError('Cannot load private credentials. Check DMC_CREDENTIALS_FILE locally.') from None
    _client = DMCClient(cookie=cookie, mc_gtk=csrf)
    _profiles = profiles
    return 'Credentials reloaded; all previous sessions cleared.'


@mcp.tool()
@_serialize_auth
def login_instance(profile: str) -> str:
    """Log in using a profile alias from the private local credentials file.

    Profile credentials must belong to a database account with read-only grants.
    Passwords and console cookies must never be supplied as tool arguments.
    """
    client = _get_client()
    creds = _profiles.get(profile)
    if creds is None:
        raise ValueError('Unknown local credentials profile.')
    client.ensure_login(
        instance_id=creds.instance_id, user=creds.user, password=creds.password,
        db_type=creds.db_type, region_id=creds.region_id,
    )
    return f'Login successful. Instance: {creds.instance_id}, User: {creds.user}'


@mcp.tool()
def execute_select(
    instance_id: str,
    sql: str,
    db_name: str,
    page_size: int = 50,
) -> str:
    """
    Execute a SELECT query on a logged-in database instance.
    Only SELECT statements are allowed (enforced by SQL validation).

    Args:
        instance_id: Instance ID (must be logged in via login_instance first)
        sql: SELECT SQL statement to execute
        db_name: Target database name within the instance
        page_size: Max rows to return, default 50

    Returns:
        Query results as formatted text.
    """
    safe_sql = _validate_select_only(sql)
    client = _get_client()
    data = client.execute_sql(
        instance_id=instance_id,
        sql=safe_sql,
        db_name=db_name,
        page_size=page_size,
    )

    items = data.get("items", [])
    col_info = data.get("info", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(col_info)]

    if not items:
        return f"Query executed successfully. 0 rows returned. Time: {data.get('timeCost', '?')}ms"

    max_col_name_len = max(len(c) for c in col_names)
    lines = []
    lines.append(" | ".join(col_names))
    lines.append("-" * min(200, max_col_name_len * len(col_names)))
    for row in items:
        vals = []
        for col_name in col_names:
            val = row.get(col_name, "")
            vals.append(str(val))
        lines.append(" | ".join(vals))

    lines.append(f"\n{len(items)} rows. Time: {data.get('timeCost', '?')}ms")
    return "\n".join(lines)


@mcp.tool()
def list_databases(instance_id: str) -> str:
    """
    List all databases accessible by the current logged-in account
    on the specified instance.

    Args:
        instance_id: Instance ID (must be logged in)

    Returns:
        List of database names.
    """
    client = _get_client()
    dbs = client.list_databases(instance_id)
    return f"Databases on {instance_id}:\n" + "\n".join(f"  - {db}" for db in dbs)


@mcp.tool()
def list_tables(
    instance_id: str,
    db_name: str,
    search: str | None = None,
) -> str:
    """
    List tables in a database, optionally filtered by name pattern.

    Args:
        instance_id: Instance ID (must be logged in)
        db_name: Database name
        search: Optional table name filter (fuzzy match)

    Returns:
        Table list with row counts and sizes.
    """
    client = _get_client()
    tables = client.list_tables(instance_id, db_name, search)
    if not tables:
        return f"No tables found in '{db_name}'" + (
            f" matching '{search}'" if search else ""
        )

    lines = [f"Tables in {db_name} ({len(tables)} total):"]
    lines.append(f"{'TABLE_NAME':40s} {'ROWS':>10s} {'SIZE':>10s}  COMMENT")
    lines.append("-" * 90)
    for t in tables:
        name = str(t.get("TABLE_NAME", ""))
        rows = str(t.get("TABLE_ROWS", ""))
        size_val = t.get("DATA_LENGTH")
        size_kb = f"{int(size_val) // 1024}KB" if size_val and str(size_val).isdigit() else "N/A"
        comment = str(t.get("TABLE_COMMENT", ""))[:30]
        lines.append(f"{name:40s} {rows:>10s} {size_kb:>10s}  {comment}")

    return "\n".join(lines)


@mcp.tool()
def get_table_detail(
    instance_id: str,
    db_name: str,
    table_name: str,
    include_ddl: bool = False,
) -> str:
    """
    Get detailed schema of a table: columns and optionally DDL (CREATE TABLE statement).

    Args:
        instance_id: Instance ID (must be logged in)
        db_name: Database name
        table_name: Table name
        include_ddl: If True, also return the CREATE TABLE DDL. Default False
                     (columns only) to save context. Set True when you need exact
                     column definitions, index definitions, or charset info.

    Returns:
        Column details (and CREATE TABLE DDL if include_ddl=True).
    """
    client = _get_client()
    detail = client.get_table_detail(instance_id, db_name, table_name, include_ddl=include_ddl)

    columns = detail["columns"]
    lines = [f"Table: {db_name}.{table_name} ({len(columns)} columns)\n"]
    lines.append(
        f"{'COLUMN':30s} {'TYPE':20s} {'NULL':6s} {'KEY':6s} {'DEFAULT':15s} COMMENT"
    )
    lines.append("-" * 110)
    for col in columns:
        name = str(col.get("COLUMN_NAME", ""))
        col_type = str(col.get("COLUMN_TYPE", ""))
        nullable = str(col.get("IS_NULLABLE", ""))
        key = str(col.get("COLUMN_KEY", ""))
        default = str(col.get("COLUMN_DEFAULT", ""))[:15]
        comment = str(col.get("COLUMN_COMMENT", ""))[:40]
        lines.append(
            f"{name:30s} {col_type:20s} {nullable:6s} {key:6s} {default:15s} {comment}"
        )

    if include_ddl:
        lines.append(f"\n--- DDL ---\n{detail['ddl']}")

    return "\n".join(lines)


@mcp.tool()
def list_active_sessions() -> str:
    """
    List all currently active (logged-in) database instance sessions.
    Useful to check which instances are ready for querying.

    Returns:
        List of active instance IDs and their session status.
    """
    client = _get_client()
    instances = client.active_instances
    if not instances:
        return "No active sessions. Use login_instance to connect."

    lines = [f"Active sessions ({len(instances)}):"]
    for inst_id in instances:
        lines.append(f"  - {inst_id}")
    return "\n".join(lines)


@mcp.tool()
def find_instance_by_ip(ip: str, region: str = "ap-shanghai") -> str:
    """
    Find a database instance by its internal Vip (proxy IP).
    Searches both TDSQL-C (CynosDB) and TDSQL (DCDB) instances.

    Typical workflow:
      1. Read the JDBC URL from your config file (e.g. 10.0.0.1:3306)
      2. Call find_instance_by_ip("10.0.0.1", region="ap-guangzhou") to get the InstanceId
      3. Configure a private local profile and call login_instance with its alias
      4. Call execute_select

    Args:
        ip: Internal Vip address, e.g. "10.0.0.1"
        region: Tencent Cloud region string, e.g. "ap-shanghai" (default), "ap-beijing", "ap-guangzhou".
                Must match the region where your cluster is deployed.

    Returns:
        Matching instance info (InstanceId, Name, Vip, DbType) or "not found".
    """
    try:
        from .cluster_search import search_all_by_ip
    except ImportError:
        return "Cluster search module not available."

    client = _get_client()
    cookie = client._cookie
    mc_gtk = client._mc_gtk
    results = search_all_by_ip(cookie, ip, mc_gtk=mc_gtk, region=region)
    if not results:
        return f"No instance found with Vip '{ip}' in either TDSQL-C or TDSQL."

    lines = [f"Found {len(results)} instance(s) matching Vip '{ip}':"]
    for r in results:
        db_type = r.get("DbType", "unknown")
        lines.append(f"  InstanceId: {r['ClusterId']}")
        lines.append(f"  Name: {r.get('ClusterName', 'N/A')}")
        lines.append(f"  Vip: {r.get('Vip', 'N/A')}")
        lines.append(f"  DbType: {db_type}")
        if r.get("ShardCount"):
            lines.append(f"  ShardCount: {r['ShardCount']}")
        if r.get("Status"):
            lines.append(f"  Status: {r['Status']}")
        lines.append("")
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
