from __future__ import annotations

import json
import math
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import mssql_python
import pandas as pd
import requests
from azure.identity import AzureCliCredential

from app_config import settings


ROOT = Path(__file__).resolve().parent.parent
PARQUET_ROOT = ROOT / "generated" / "data" / "parquet"
OUTPUT_PATH = ROOT / "generated" / "fabric-sql-deployment.json"

WORKSPACE_ID = settings.fabric_workspace_id
DATABASE_NAME = settings.fabric_sql_database_name
DATABASE_DESCRIPTION = "Read-only Maximo operational replica for governed analytics."
FABRIC_API = "https://api.fabric.microsoft.com/v1"
DATE_COLUMNS = {
    "assets": ["commission_date"],
    "document_register": ["effective_date", "review_date"],
    "meter_readings": ["readingdate"],
    "preventive_maintenance": ["last_start_date", "next_due_date"],
    "work_orders": ["reportdate", "target_start", "target_finish", "actual_start", "actual_finish"],
    "work_order_labor": ["startdate"],
    "work_order_status_history": ["changedate"],
}
PRIMARY_KEYS = {
    "assets": "assetnum",
    "crews": "crewnum",
    "dim_date": "date",
    "document_register": "docnum",
    "inventory_items": "itemnum",
    "job_plans": "jpnum",
    "labor": "laborcode",
    "locations": "location",
    "preventive_maintenance": "pmnum",
    "sites": "siteid",
    "work_orders": "wonum",
}


def fabric_headers(credential: AzureCliCredential) -> dict[str, str]:
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    return {"Authorization": f"Bearer {token}"}


def wait_for_operation(
    credential: AzureCliCredential, response: requests.Response
) -> None:
    if response.status_code != 202:
        return
    operation_url = response.headers.get("Location")
    if not operation_url:
        raise RuntimeError("Fabric accepted the request without returning an operation URL.")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        operation = requests.get(
            operation_url, headers=fabric_headers(credential), timeout=60
        )
        operation.raise_for_status()
        payload = operation.json()
        if payload["status"] == "Succeeded":
            return
        if payload["status"] == "Failed":
            raise RuntimeError(f"Fabric operation failed: {payload.get('error')}")
        time.sleep(5)
    raise TimeoutError("Fabric operation did not complete within 15 minutes.")


def ensure_sql_database(
    credential: AzureCliCredential,
) -> tuple[str, str, str]:
    items_response = requests.get(
        f"{FABRIC_API}/workspaces/{WORKSPACE_ID}/items",
        headers=fabric_headers(credential),
        timeout=60,
    )
    items_response.raise_for_status()
    database = next(
        (
            item
            for item in items_response.json()["value"]
            if item["type"] == "SQLDatabase" and item["displayName"] == DATABASE_NAME
        ),
        None,
    )
    if database is None:
        create_response = requests.post(
            f"{FABRIC_API}/workspaces/{WORKSPACE_ID}/sqlDatabases",
            headers=fabric_headers(credential),
            json={
                "displayName": DATABASE_NAME,
                "description": DATABASE_DESCRIPTION,
                "creationPayload": {"creationMode": "New"},
            },
            timeout=60,
        )
        create_response.raise_for_status()
        wait_for_operation(credential, create_response)
        items_response = requests.get(
            f"{FABRIC_API}/workspaces/{WORKSPACE_ID}/items",
            headers=fabric_headers(credential),
            timeout=60,
        )
        items_response.raise_for_status()
        database = next(
            item
            for item in items_response.json()["value"]
            if item["type"] == "SQLDatabase" and item["displayName"] == DATABASE_NAME
        )

    database_id = database["id"]
    detail_response = requests.get(
        f"{FABRIC_API}/workspaces/{WORKSPACE_ID}/sqlDatabases/{database_id}",
        headers=fabric_headers(credential),
        timeout=60,
    )
    detail_response.raise_for_status()
    properties = detail_response.json()["properties"]
    return database_id, properties["connectionString"], properties["databaseName"]


def connect(connection_string: str) -> Any:
    properties = {}
    for component in connection_string.split(";"):
        if "=" in component:
            key, value = component.split("=", 1)
            properties[key.strip().lower()] = value.strip()
    server = properties["data source"]
    database = properties["initial catalog"]
    return mssql_python.connect(
        f"Server={server};Database={database};"
        "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no"
    )


def quote_identifier(value: str) -> str:
    return "[" + value.replace("]", "]]") + "]"


def sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series.dtype):
        return "bit"
    if pd.api.types.is_integer_dtype(series.dtype):
        return "bigint"
    if pd.api.types.is_float_dtype(series.dtype):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return "datetime2(6)"
    lengths = series.dropna().astype(str).str.len()
    maximum = int(lengths.max()) if not lengths.empty else 1
    if maximum > 4000:
        return "nvarchar(max)"
    size = max(32, min(4000, int(math.ceil(maximum / 32) * 32)))
    return f"nvarchar({size})"


def python_value(value: Any) -> Any:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(tzinfo=None).isoformat()
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def insert_rows(
    cursor: Any,
    quoted_table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
) -> None:
    rows_by_value_columns: dict[tuple[int, ...], list[tuple[Any, ...]]] = {}
    for row in rows:
        value_columns = tuple(
            index for index, value in enumerate(row) if value is not None
        )
        values = tuple(row[index] for index in value_columns)
        rows_by_value_columns.setdefault(value_columns, []).append(values)

    for value_columns, grouped_rows in rows_by_value_columns.items():
        column_list = ", ".join(
            quote_identifier(columns[index]) for index in value_columns
        )
        placeholders = ", ".join("?" for _ in value_columns)
        insert_sql = (
            f"INSERT INTO {quoted_table} ({column_list}) "
            f"VALUES ({placeholders})"
        )
        for batch in batches(grouped_rows, 250):
            cursor.executemany(insert_sql, batch)


def load_replica_tables(connection: Any) -> dict[str, int]:
    cursor = connection.cursor()
    table_counts: dict[str, int] = {}
    for parquet_path in sorted(PARQUET_ROOT.glob("*.parquet")):
        table_name = parquet_path.stem
        frame = pd.read_parquet(parquet_path)
        for column in DATE_COLUMNS.get(table_name, []):
            frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True).dt.tz_localize(None)

        quoted_table = f"dbo.{quote_identifier(table_name)}"
        cursor.execute(
            f"IF OBJECT_ID(N'{quoted_table}', N'U') IS NOT NULL DROP TABLE {quoted_table};"
        )
        candidate_key = PRIMARY_KEYS.get(table_name)
        primary_key = (
            candidate_key
            if candidate_key
            and frame[candidate_key].notna().all()
            and frame[candidate_key].is_unique
            else None
        )
        definitions = [
            (
                f"{quote_identifier(column)} {sql_type(frame[column])} "
                f"{'NOT NULL' if column == primary_key else 'NULL'}"
            )
            for column in frame.columns
        ]
        if primary_key:
            definitions.append(
                f"CONSTRAINT {quote_identifier(f'PK_{table_name}')} "
                f"PRIMARY KEY ({quote_identifier(primary_key)})"
            )
        cursor.execute(f"CREATE TABLE {quoted_table} ({', '.join(definitions)});")

        rows = [
            tuple(python_value(value) for value in row)
            for row in frame.itertuples(index=False, name=None)
        ]
        insert_rows(cursor, quoted_table, list(frame.columns), rows)
        connection.commit()
        table_counts[table_name] = len(frame)
    return table_counts


def configure_replica_metadata(connection: Any) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        IF SCHEMA_ID(N'integration') IS NULL EXEC(N'CREATE SCHEMA integration');

        IF OBJECT_ID(N'integration.oracle_extract_state', N'U') IS NULL
        BEGIN
            CREATE TABLE integration.oracle_extract_state (
                source_system nvarchar(64) NOT NULL PRIMARY KEY,
                extraction_mode nvarchar(32) NOT NULL,
                watermark_column nvarchar(64) NOT NULL,
                last_successful_watermark nvarchar(128) NULL,
                last_successful_extract datetime2(6) NULL
            );
        END;

        MERGE integration.oracle_extract_state AS target
        USING (
            SELECT
                N'MAXIMO_ORACLE' AS source_system,
                N'INCREMENTAL' AS extraction_mode,
                N'CHANGEDATE_OR_ROWSTAMP' AS watermark_column
        ) AS source
        ON target.source_system = source.source_system
        WHEN MATCHED THEN UPDATE SET
            extraction_mode = source.extraction_mode,
            watermark_column = source.watermark_column,
            last_successful_extract = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT (
            source_system, extraction_mode, watermark_column,
            last_successful_extract
        ) VALUES (
            source.source_system, source.extraction_mode, source.watermark_column,
            SYSUTCDATETIME()
        );
        """
    )
    connection.commit()


def remove_legacy_vector_objects(connection: Any) -> None:
    cursor = connection.cursor()
    connection.setautocommit(True)
    try:
        cursor.execute(
            """
            IF EXISTS (
                SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID(N'ai.work_order_search')
                  AND name = N'IX_work_order_search_embedding'
            )
                DROP INDEX IX_work_order_search_embedding ON ai.work_order_search;
            """
        )
    finally:
        connection.setautocommit(False)
    cursor.execute(
        """
        IF EXISTS (SELECT 1 FROM sys.external_models WHERE name = N'WorkOrderEmbeddingModel')
            DROP EXTERNAL MODEL WorkOrderEmbeddingModel;
        IF OBJECT_ID(N'ai.work_order_search', N'U') IS NOT NULL
            DROP TABLE ai.work_order_search;
        """
    )
    cursor.execute(
        """
        SELECT name
        FROM sys.database_scoped_credentials
        WHERE name LIKE N'https://%openai.azure.com'
           OR name LIKE N'https://%azurecontainerapps.io'
        """
    )
    for (credential_name,) in cursor.fetchall():
        cursor.execute(
            f"DROP DATABASE SCOPED CREDENTIAL {quote_identifier(credential_name)}"
        )
    connection.commit()


def main() -> None:
    if not PARQUET_ROOT.exists():
        raise FileNotFoundError("Generated Parquet data was not found. Run generate_demo.py first.")

    credential = AzureCliCredential()
    database_id, connection_string, physical_database_name = ensure_sql_database(credential)
    connection = connect(connection_string)
    try:
        table_counts = load_replica_tables(connection)
        configure_replica_metadata(connection)
        remove_legacy_vector_objects(connection)
    finally:
        connection.close()

    result = {
        "workspaceId": WORKSPACE_ID,
        "databaseId": database_id,
        "databaseName": DATABASE_NAME,
        "connectionString": connection_string,
        "physicalDatabaseName": physical_database_name,
        "sourceSystem": "MAXIMO_ORACLE_READ_ONLY",
        "replicaTableCounts": table_counts,
        "legacySqlVectorObjectsRemoved": True,
        "oneLakeReplication": "Automatic",
    }
    OUTPUT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
