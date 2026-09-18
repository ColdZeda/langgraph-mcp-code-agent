import sys
from typing import Optional, Dict, Any, Annotated, List

import pymysql
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from app.code_agent.config import MYSQL_CHARSET, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

mcp = FastMCP()

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

class Response(BaseModel):
    success: bool
    database: str
    table: str
    data: Optional[dict] | Optional[list]
    rowcount: Optional[int] = None

MYSQL_CONFIG = {
    "host": MYSQL_HOST,
    "port": MYSQL_PORT,
    "user": MYSQL_USER,
    "password": MYSQL_PASSWORD,
    "charset": MYSQL_CHARSET
}


def _safe_ident(identifier: str) -> str:
    """用反引号包裹 MySQL 标识符（表名/数据库名），防止注入。"""
    return "`" + identifier.replace("`", "``") + "`"


def get_connection(db):
    config = MYSQL_CONFIG.copy()
    if db:
        config['database'] = db

    try:
        connection = pymysql.connect(**config)
        return connection
    except Exception as e:
        msg = f'mysql connection error: {str(e)}'
        return msg


def execute_query(command, database = None, params = None, commit = False):
    try:
        connection = get_connection(database)
        if not isinstance(connection, pymysql.Connection):
            return connection
        else:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(command, params)

                result = cursor.fetchall()

                if commit:
                    connection.commit()

                return result, cursor.rowcount
    except Exception as e:
        raise


@mcp.tool(name="mysql_list_databases", description="列举MySQL中包含哪些数据库")
def mysql_list_databases():
    try:
        result, rowcount = execute_query("show databases")
        databases = [row['Database'] for row in result]
        return Response(
            success=True,
            database="",
            table="",
            data=databases,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'list databases error: {str(e)}'
        return msg


@mcp.tool(name="mysql_list_tables", description="获取指定数据库中的所有表")
def mysql_list_tables(database: Annotated[str, Field(description="数据库名", examples=["agent_test"])]):
    try:
        result, rowcount = execute_query("show tables", database=database)
        tables = [list(row.values())[0] for row in result]
        return Response(
            success=True,
            database=database,
            table='',
            data=tables,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'list tables error: {str(e)}'
        return msg


@mcp.tool(name="mysql_describe_tables", description="获取表结构信息")
def mysql_describe_tables(
    database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
    table: Annotated[str, Field(description="表名", examples=["evals"])],
):
    try:
        result, rowcount = execute_query(f"describe {_safe_ident(table)}", database=database)
        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'describe tables error: {str(e)}'
        return msg

@mcp.tool(name="mysql_execute_query", description="执行 SQL 查询语句（只读 SELECT 类查询，不提交写操作）")
def mysql_execute_query(
    command: Annotated[str, Field(description="SQL 查询语句，应为 SELECT/SHOW/DESCRIBE 等只读查询", examples=["SELECT * FROM evals"])],
    database: Annotated[Optional[str], Field(description="数据库名（可选）", examples=["agent_test"])] = None,
    params: Annotated[Optional[list], Field(description="查询参数列表（可选），对应 SQL 中的 %s 占位符")] = None,
):
    try:
        params_tuple = tuple(params) if params else None
        result, rowcount = execute_query(command, database=database, params=params_tuple)
        return Response(
            success=True,
            database=database,
            table='',
            data=result,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'query tables error: {str(e)}'
        return msg

@mcp.tool(name="mysql_insert_data", description="向表里插入数据")
def mysql_insert_data(
    database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
    table: Annotated[str, Field(description="表名", examples=["evals"])],
    data: Annotated[Dict[str, str], Field(description="要插入的字段-值映射", examples=[{"task_name": "test", "score": "100"}])],
):
    columns = list(data.keys())
    values = list(data.values())
    values_wrapper = ', '.join(['%s'] * len(values))
    command = f"INSERT INTO {_safe_ident(table)} ({','.join(columns)}) VALUES ({values_wrapper})"

    try:
        result, rowcount = execute_query(command, database=database, params=tuple(values), commit=True)
        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount
        )
    except Exception as e:
        msg = f'insert tables error: {str(e)}'
        return msg

@mcp.tool(name="mysql_update_data", description="向表里更新数据")
def mysql_update_data(
    database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
    table: Annotated[str, Field(description="表名", examples=["evals"])],
    data: Annotated[Dict[str, str], Field(description="要更新的字段-新值映射", examples=[{"score": "90"}])],
    where: Annotated[Dict[str, str], Field(description="WHERE 条件字段-值映射（等值匹配）", examples=[{"task_name": "test"}])],
):
    set_clause = ', '.join([f"{k} = %s" for k in data.keys()])
    where_clause = " and ".join([f"{k} = %s" for k in where.keys()])
    command = f"UPDATE {_safe_ident(table)} SET {set_clause} WHERE {where_clause}"

    set_params = list(data.values())
    where_params = list(where.values())
    params = set_params + where_params
    try:
        result, rowcount = execute_query(command, database=database, params=tuple(params), commit=True)
        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount
        )
    except Exception as e:
        msg = f'update tables error: {str(e)}'
        return msg

@mcp.tool(name="mysql_delete_data", description="向表里删除数据")
def mysql_delete_data(
    database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
    table: Annotated[str, Field(description="表名", examples=["evals"])],
    where: Annotated[Dict[str, str], Field(description="WHERE 条件字段-值映射（等值匹配）", examples=[{"task_name": "test"}])],
):
    where_clause = " and ".join([f"{k} = %s" for k in where.keys()])
    command = f"DELETE FROM {_safe_ident(table)} WHERE {where_clause}"

    params = list(where.values())
    try:
        result, rowcount = execute_query(command, database=database, params=tuple(params), commit=True)
        return Response(
            success=True,
            database=database,
            table=table,
            data=result,
            rowcount=rowcount
        )
    except Exception as e:
        msg = f'delete tables error: {str(e)}'
        return msg


@mcp.tool(name="mysql_create_database", description="创建新数据库")
def mysql_create_database(
    database_name: Annotated[str, Field(description="新数据库名", examples=["agent_test"])],
    charset: Annotated[str, Field(description="字符集（默认 utf8mb4）", examples=["utf8mb4"])] = "utf8mb4",
):
    command = f"CREATE DATABASE {_safe_ident(database_name)} CHARACTER SET {charset}"

    try:
        result, rowcount = execute_query(command)
        return Response(
            success=True,
            database=database_name,
            table="",
            data=result,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'create database error: {str(e)}'
        return msg


@mcp.tool(name="mysql_create_table", description="创建新表")
def mysql_create_table(
        database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
        table_name: Annotated[str, Field(description="新表名", examples=["evals"])],
        table_columns: Annotated[str, Field(description="建表语句中的字段部分", examples=["`id` int NOT NULL AUTO_INCREMENT,`name` varchar(255) COLLATE utf8mb4_general_ci NOT NULL,PRIMARY KEY (`id`)"])],
        table_schema: Annotated[str, Field(description="建表语句中的补充部分", examples=["ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"])]
):
    """
    建表语句示例：
    CREATE TABLE `user` (
        `id` int NOT NULL AUTO_INCREMENT,
        `name` varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
        PRIMARY KEY (`id`)
    ) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
    :param database:
    :param table_name:
    :param table_columns:
    :param table_schema:
    :return:
    """
    command = f"CREATE TABLE {_safe_ident(table_name)} ({table_columns}) {table_schema}"

    try:
        result, rowcount = execute_query(command, database=database)
        return Response(
            success=True,
            database=database,
            table=table_name,
            data=result,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'create table error: {str(e)}'
        return msg


@mcp.tool(name="mysql_execute_command", description="执行特定的 SQL 语句（写操作：变更表结构、增减字段等 DDL/DML）")
def mysql_execute_command(
    database: Annotated[str, Field(description="数据库名", examples=["agent_test"])],
    command: Annotated[str, Field(description="要执行的 SQL 语句（ALTER/DROP/TRUNCATE 等），执行后自动提交", examples=["ALTER TABLE evals ADD COLUMN note VARCHAR(100)"])],
):
    try:
        result, rowcount = execute_query(command, database=database, commit=True)
        return Response(
            success=True,
            database=database,
            table="",
            data=result,
            rowcount=rowcount,
        )
    except Exception as e:
        msg = f'execute command error: {str(e)}'
        return msg




if __name__ == '__main__':
    mcp.run(transport="stdio")
