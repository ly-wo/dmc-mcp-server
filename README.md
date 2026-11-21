# dmc-mcp-server (hardened fork)

通过腾讯云 DMC Web API 查询 TDSQL-C / TDSQL。此分支修复原版 SQL
前缀检查、跨账号会话复用与凭据进入 MCP 参数/结果的问题。

## 本地运行

```bash
uv sync --locked
uv run --frozen dmc-mcp-server
```

Codex 等客户端使用 stdio；将 command 设为本机 `uv` 的绝对路径，args 设为
`["run", "--frozen", "--directory", "<本仓库绝对路径>", "dmc-mcp-server"]`。
只配置 `DMC_CREDENTIALS_FILE` 的文件路径，不把 Cookie、密码放进客户端配置。
这是个人 fork，不要用 `uvx dmc-mcp-server`，它会安装上游 PyPI 版本。

## 凭据：本地私有文件

在仓库外用本地编辑器创建绝对路径 JSON 文件，文件必须归当前用户所有，
权限为 `600`，且不是符号链接。不要把真实值发到聊天、MCP 参数、Git 或日志。
以下均为占位符，不可直接用于连接：

```json
{
  "cookie": "<腾讯云控制台 Cookie>",
  "mc_gtk": 123456,
  "profiles": {
    "production-readonly": {
      "instance_id": "<实例 ID>",
      "user": "<只读数据库账号>",
      "password": "<数据库密码>",
      "db_type": "cynosdbmysql",
      "region_id": 4
    }
  }
}
```

`db_type` 支持 `cynosdbmysql` 和 `tdsql`。必须使用实际实例所在地域，默认
4 为上海。本分支尚未验证普通云 MySQL / `cdbro-` 实例和 DMS 快捷登录。
该文件是本地明文秘密，仅靠文件权限保护，备份时也须保护；进程运行期间会
持有凭据。SQL 查询结果仍会返回 MCP 客户端，应按授权范围查询必要字段。

本地更新文件后调用 `reload_credentials()`，然后 `login_instance(profile)`。
重新加载（包括失败时）会清空旧会话；登录结果不包含 Token。切换数据库账号、
密码、类型、地域均重新认证，失败后不会回退到旧身份。Cookie 过期仍需人工
更新，没有绕过腾讯云认证。正在服务端执行的请求不会因重载而被撤销。

## 工具

- `reload_credentials()`：从固定的本地路径读取配置，无凭据参数。
- `login_instance(profile)`：使用配置中的账号别名登录。
- `execute_select(instance_id, sql, db_name, page_size=50)`：有界返回查询结果。
- `list_databases` / `list_tables` / `get_table_detail`：查询元数据。
- `list_active_sessions`：查看已认证实例 ID，不返回 Token。
- `find_instance_by_ip`：搜索 TDSQL-C / TDSQL 实例。

相比上游，这是有意的认证接口变更：移除 `set_cookie`，`login_instance`
不再接受密码。凭据文件可以通过用户自己的安全管理方式生成，但不要让模型
读取真实内容。

## 只读边界和限制

- 统一检查所有 SQL 执行入口，按 MySQL 语法解析且只接受一条查询。
- 拒绝多语句、DML/DDL、文件输出、行锁、变量赋值、可执行注释、未知/有副作用
  函数、带库名或引号的函数调用；仅生成并发送通过检查的语法树。
- 内置函数采用小范围允许列表；普通 SELECT、JOIN、CTE、聚合、UNION、分页
  和常见日期查询保留。未覆盖函数会明确拒绝，不自动放宽。
- 反斜线转义因 SQL 模式差异而拒绝。元数据库表名限制为 ASCII 字母、数字、
  下划线、美元符号和连字符；搜索字符串允许单引号，不允许反斜线。
- 元数据入口只允许精确的 `SHOW DATABASES`、`SHOW CREATE TABLE`。
- 返回页大小为 1–1000；这不限制数据库扫描成本，也不是服务端超时控制。
- **必须使用数据库只读账号。** SQL 检查是附加保护，不能证明视图内部不调用
  存储函数，也不能保证昂贵 SELECT 不影响负载；实例/数据库权限由账号授予范围
  决定。本地管理员、凭据文件所有者和同用户进程仍属于可信边界。
- 依赖使用 `uv.lock` 固定；SQL 解析器版本额外固定，升级后需重新跑安全测试。
- 不修改宿主进程的代理环境变量；腾讯云 HTTP 客户端保持直接 HTTPS 连接。

## 验证

```bash
uv run --frozen python -m unittest discover -s tests -v
```

测试使用模拟凭据和 HTTP 响应，覆盖 SQL 绕过、元数据入口、会话切换及失败、
凭据文件权限、MCP 工具参数和 Token 输出，不连接生产数据库。

生产 DMS 接口、账号权限、实际 SQL 模式和普通 MySQL 兼容性须另外验证；本地
测试通过不代表生产连接或完整安全审计已通过。

## License

MIT；上游项目：https://github.com/yangfeng20/dmc-mcp-server
