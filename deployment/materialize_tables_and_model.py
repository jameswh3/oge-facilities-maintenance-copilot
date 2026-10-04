from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
from deltalake import write_deltalake

from app_config import settings


ROOT = Path(__file__).resolve().parent.parent
PARQUET_ROOT = ROOT / "generated" / "data" / "parquet"
TABLES_ROOT = ROOT / "generated" / "tables"
MODEL_ROOT = ROOT / "generated" / "semantic-model"

DATE_COLUMNS = {
    "assets": ["commission_date"],
    "document_register": ["effective_date", "review_date"],
    "meter_readings": ["readingdate"],
    "preventive_maintenance": ["last_start_date", "next_due_date"],
    "work_orders": ["reportdate", "target_start", "target_finish", "actual_start", "actual_finish"],
    "work_order_labor": ["startdate"],
    "work_order_status_history": ["changedate"],
}

DISPLAY_NAMES = {
    "assets": "Assets",
    "crews": "Crews",
    "dim_date": "Date",
    "document_register": "Document Register",
    "inventory_items": "Inventory Items",
    "job_plans": "Job Plans",
    "labor": "Labor",
    "locations": "Locations",
    "meter_readings": "Meter Readings",
    "preventive_maintenance": "Preventive Maintenance",
    "sites": "Sites",
    "work_orders": "Work Orders",
    "work_order_labor": "Work Order Labor",
    "work_order_materials": "Work Order Materials",
    "work_order_status_history": "Work Order Status History",
}

COLUMN_NAMES = {
    "actual_cost_usd": "Actual Cost",
    "actual_finish": "Actual Finish",
    "actual_labor_hours": "Actual Labor Hours",
    "actual_start": "Actual Start",
    "active": "Active",
    "asset_class": "Asset Class",
    "assetnum": "Asset Number",
    "catalogcode": "Catalog Code",
    "cause_code": "Cause Code",
    "changedate": "Change Date",
    "changeby": "Changed By",
    "city": "City",
    "commission_date": "Commission Date",
    "commodity_group": "Commodity Group",
    "condition_enabled": "Condition Enabled",
    "craft": "Craft",
    "crew_size": "Crew Size",
    "crewnum": "Crew Number",
    "criticality": "Criticality",
    "data_classification": "Data Classification",
    "date": "Date",
    "day": "Day",
    "day_name": "Day Name",
    "day_of_week": "Day of Week",
    "description": "Description",
    "displayname": "Display Name",
    "docnum": "Document Number",
    "docx_file": "DOCX File",
    "effective_date": "Effective Date",
    "estimated_cost_usd": "Estimated Cost",
    "estimated_hours": "Estimated Hours",
    "estimated_labor_hours": "Estimated Labor Hours",
    "facility_type": "Facility Type",
    "failure_class": "Failure Class",
    "fiscal_quarter": "Fiscal Quarter",
    "fiscal_year": "Fiscal Year",
    "frequency": "Frequency",
    "frequency_unit": "Frequency Unit",
    "home_siteid": "Home Site",
    "inspector": "Inspector",
    "is_weekend": "Is Weekend",
    "issueunit": "Issue Unit",
    "itemnum": "Item Number",
    "jpnum": "Job Plan Number",
    "labor_rate_usd": "Labor Rate",
    "labor_sequence": "Labor Sequence",
    "laborcode": "Labor Code",
    "last_start_date": "Last Start Date",
    "lead": "Lead",
    "lead_craft": "Lead Craft",
    "library": "Library",
    "linecost_usd": "Line Cost",
    "location": "Location",
    "long_description": "Long Description",
    "manufacturer": "Manufacturer",
    "material_sequence": "Material Sequence",
    "memo": "Memo",
    "metername": "Meter Name",
    "model": "Model",
    "month": "Month",
    "month_name": "Month Name",
    "next_due_date": "Next Due Date",
    "orderunit": "Order Unit",
    "orgid": "Organization",
    "owner": "Owner",
    "parent": "Parent",
    "pdf_file": "PDF File",
    "pmnum": "PM Number",
    "premium_hours": "Premium Hours",
    "priority": "Priority",
    "problem_code": "Problem Code",
    "procedure_docnum": "Procedure Document Number",
    "quarter": "Quarter",
    "quantity": "Quantity",
    "reading": "Reading",
    "readingdate": "Reading Date",
    "region": "Region",
    "regular_hours": "Regular Hours",
    "remedy_code": "Remedy Code",
    "replacement_value_usd": "Replacement Value",
    "reportdate": "Report Date",
    "report_date": "Report Date Key",
    "reportedby": "Reported By",
    "review_date": "Review Date",
    "revision": "Revision",
    "sequence": "Sequence",
    "serialnum": "Serial Number",
    "shift": "Shift",
    "siteid": "Site ID",
    "skilllevel": "Skill Level",
    "startdate": "Start Date",
    "state": "State",
    "status": "Status",
    "storeroom": "Storeroom",
    "supervisor": "Supervisor",
    "synthetic": "Synthetic",
    "synthetic_source": "Synthetic Source",
    "target_finish": "Target Finish",
    "target_start": "Target Start",
    "timezone": "Time Zone",
    "title": "Title",
    "type": "Type",
    "unit": "Unit",
    "unitcost_usd": "Unit Cost",
    "use_target_date": "Use Target Date",
    "week_of_year": "Week of Year",
    "wonum": "Work Order Number",
    "worktype": "Work Type",
    "year": "Year",
}

KEY_COLUMNS = {
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

MEASURES = {
    "Assets": [
        ("Asset Count", "DISTINCTCOUNT('Assets'[Asset Number])", "#,##0"),
        ("Critical Asset Count", 'CALCULATE([Asset Count], \'Assets\'[Criticality] = "1-CRITICAL")', "#,##0"),
        ("Total Replacement Value", "SUM('Assets'[Replacement Value])", "$#,##0"),
    ],
    "Preventive Maintenance": [
        ("Active PM Count", 'CALCULATE(COUNTROWS(\'Preventive Maintenance\'), \'Preventive Maintenance\'[Status] = "ACTIVE")', "#,##0"),
        ("Overdue PM Count", "CALCULATE(COUNTROWS('Preventive Maintenance'), 'Preventive Maintenance'[Next Due Date] < TODAY(), 'Preventive Maintenance'[Status] = \"ACTIVE\")", "#,##0"),
    ],
    "Work Order Labor": [
        ("Transaction Labor Hours", "SUM('Work Order Labor'[Regular Hours]) + SUM('Work Order Labor'[Premium Hours])", "#,##0.0"),
        ("Transaction Labor Cost", "SUMX('Work Order Labor', ('Work Order Labor'[Regular Hours] + 'Work Order Labor'[Premium Hours]) * 'Work Order Labor'[Labor Rate])", "$#,##0"),
    ],
    "Work Order Materials": [
        ("Material Cost", "SUM('Work Order Materials'[Line Cost])", "$#,##0"),
        ("Material Quantity", "SUM('Work Order Materials'[Quantity])", "#,##0"),
    ],
    "Work Orders": [
        ("Work Order Count", "COUNTROWS('Work Orders')", "#,##0"),
        ("Open Work Order Count", 'CALCULATE([Work Order Count], NOT(\'Work Orders\'[Status] IN {"COMP", "CLOSE", "CAN"}))', "#,##0"),
        ("Completed Work Order Count", 'CALCULATE([Work Order Count], \'Work Orders\'[Status] IN {"COMP", "CLOSE"})', "#,##0"),
        ("Completion Rate", "DIVIDE([Completed Work Order Count], [Work Order Count])", "0.0%"),
        ("Emergency Work Order Count", 'CALCULATE([Work Order Count], \'Work Orders\'[Work Type] = "EM")', "#,##0"),
        ("High Priority Backlog", 'CALCULATE([Open Work Order Count], \'Work Orders\'[Priority] <= 2)', "#,##0"),
        ("Total Estimated Labor Hours", "SUM('Work Orders'[Estimated Labor Hours])", "#,##0.0"),
        ("Total Actual Labor Hours", "SUM('Work Orders'[Actual Labor Hours])", "#,##0.0"),
        ("Labor Hour Variance", "[Total Actual Labor Hours] - [Total Estimated Labor Hours]", "#,##0.0;(#,##0.0)"),
        ("Estimated Maintenance Cost", "SUM('Work Orders'[Estimated Cost])", "$#,##0"),
        ("Actual Maintenance Cost", "SUM('Work Orders'[Actual Cost])", "$#,##0"),
        ("Maintenance Cost Variance", "[Actual Maintenance Cost] - [Estimated Maintenance Cost]", "$#,##0;($#,##0)"),
        ("Average Completion Days", "AVERAGEX(FILTER('Work Orders', NOT ISBLANK('Work Orders'[Actual Finish])), DATEDIFF('Work Orders'[Report Date], 'Work Orders'[Actual Finish], DAY))", "#,##0.0"),
    ],
}

RELATIONSHIPS = [
    ("WorkOrders_Assets", "Work Orders", "Asset Number", "Assets", "Asset Number"),
    ("WorkOrders_Sites", "Work Orders", "Site ID", "Sites", "Site ID"),
    ("WorkOrders_Locations", "Work Orders", "Location", "Locations", "Location"),
    ("WorkOrders_Crews", "Work Orders", "Crew Number", "Crews", "Crew Number"),
    ("WorkOrders_Labor", "Work Orders", "Lead", "Labor", "Labor Code"),
    ("WorkOrders_JobPlans", "Work Orders", "Job Plan Number", "Job Plans", "Job Plan Number"),
    ("WorkOrders_ReportDate", "Work Orders", "Report Date Key", "Date", "Date"),
    ("WOLabor_WorkOrders", "Work Order Labor", "Work Order Number", "Work Orders", "Work Order Number"),
    ("WOMaterials_WorkOrders", "Work Order Materials", "Work Order Number", "Work Orders", "Work Order Number"),
    ("WOMaterials_Items", "Work Order Materials", "Item Number", "Inventory Items", "Item Number"),
    ("WOHistory_WorkOrders", "Work Order Status History", "Work Order Number", "Work Orders", "Work Order Number"),
    ("PM_Assets", "Preventive Maintenance", "Asset Number", "Assets", "Asset Number"),
    ("PM_JobPlans", "Preventive Maintenance", "Job Plan Number", "Job Plans", "Job Plan Number"),
    ("MeterReadings_Assets", "Meter Readings", "Asset Number", "Assets", "Asset Number"),
]


def tmdl_name(value: str) -> str:
    return f"'{value}'" if " " in value or value in {"Date"} else value


def quote_string(value: str) -> str:
    return value.replace("'", "''")


def data_type(dtype: Any) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "int64"
    if pd.api.types.is_float_dtype(dtype):
        return "double"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "dateTime"
    return "string"


def format_string(source_name: str, dtype: Any) -> str | None:
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "yyyy-mm-dd" if source_name in {"date", "commission_date", "effective_date", "review_date", "last_start_date", "next_due_date"} else "yyyy-mm-dd hh:mm:ss"
    if source_name.endswith("_usd") or source_name in {"linecost_usd"}:
        return "$#,##0.00"
    if pd.api.types.is_float_dtype(dtype):
        return "#,##0.00"
    if pd.api.types.is_integer_dtype(dtype):
        return "#,##0"
    return None


def prepare_tables() -> dict[str, pd.DataFrame]:
    if TABLES_ROOT.exists():
        shutil.rmtree(TABLES_ROOT)
    TABLES_ROOT.mkdir(parents=True)
    frames: dict[str, pd.DataFrame] = {}
    for parquet_path in sorted(PARQUET_ROOT.glob("*.parquet")):
        name = parquet_path.stem
        frame = pd.read_parquet(parquet_path)
        for column in DATE_COLUMNS.get(name, []):
            frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True).dt.tz_localize(None)
        if name == "work_orders":
            frame["report_date"] = frame["reportdate"].dt.normalize()
        for column in frame.columns:
            if frame[column].dtype == object:
                frame[column] = frame[column].astype("string")
        frames[name] = frame

    start = pd.Timestamp("2023-01-01")
    end = pd.Timestamp("2028-12-31")
    date_values = pd.date_range(start, end, freq="D")
    date_frame = pd.DataFrame({"date": date_values})
    date_frame["year"] = date_frame["date"].dt.year.astype("int64")
    date_frame["quarter"] = "Q" + date_frame["date"].dt.quarter.astype(str)
    date_frame["month"] = date_frame["date"].dt.month.astype("int64")
    date_frame["month_name"] = date_frame["date"].dt.month_name()
    date_frame["day"] = date_frame["date"].dt.day.astype("int64")
    date_frame["day_of_week"] = (date_frame["date"].dt.dayofweek + 1).astype("int64")
    date_frame["day_name"] = date_frame["date"].dt.day_name()
    date_frame["week_of_year"] = date_frame["date"].dt.isocalendar().week.astype("int64")
    date_frame["is_weekend"] = date_frame["date"].dt.dayofweek >= 5
    date_frame["fiscal_year"] = (date_frame["date"].dt.year + (date_frame["date"].dt.month >= 7)).astype("int64")
    date_frame["fiscal_quarter"] = "FQ" + (((date_frame["date"].dt.month - 7) % 12 // 3) + 1).astype(str)
    frames["dim_date"] = date_frame

    for name, frame in frames.items():
        arrow_table = pa.Table.from_pandas(frame, preserve_index=False)
        write_deltalake(TABLES_ROOT / name, arrow_table, mode="overwrite", schema_mode="overwrite")
    return frames


def table_tmdl(source_name: str, frame: pd.DataFrame) -> str:
    display_name = DISPLAY_NAMES[source_name]
    lines = [f"table {tmdl_name(display_name)}", ""]
    for measure_name, expression, format_value in MEASURES.get(display_name, []):
        lines.extend(
            [
                f"\tmeasure {tmdl_name(measure_name)} = {expression}",
                f"\t\tformatString: {format_value}",
                "",
            ]
        )
    for column in frame.columns:
        display_column = COLUMN_NAMES.get(column, column.replace("_", " ").title())
        dtype = frame[column].dtype
        lines.append(f"\tcolumn {tmdl_name(display_column)}")
        lines.append(f"\t\tdataType: {data_type(dtype)}")
        if KEY_COLUMNS.get(source_name) == column:
            lines.append("\t\tisKey")
        column_format = format_string(column, dtype)
        if column_format:
            lines.append(f"\t\tformatString: {column_format}")
        summarize = "sum" if pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype) else "none"
        lines.append(f"\t\tsummarizeBy: {summarize}")
        lines.append(f"\t\tsourceColumn: {column}")
        lines.append("")
    lines.extend(
        [
            f"\tpartition {tmdl_name(display_name)} = entity",
            "\t\tmode: directLake",
            "\t\tsource",
            f"\t\t\tentityName: {source_name}",
            "\t\t\texpressionSource: DatabaseQuery",
            "",
        ]
    )
    return "\n".join(lines)


def write_model(frames: dict[str, pd.DataFrame]) -> None:
    if not settings.fabric_sql_connection_string:
        raise RuntimeError(
            "FABRIC_SQL_CONNECTION_STRING is required. Provision Fabric SQL before "
            "materializing the semantic model."
        )
    connection_parts = {}
    for component in settings.fabric_sql_connection_string.split(";"):
        if "=" in component:
            key, value = component.split("=", 1)
            connection_parts[key.strip().casefold()] = value.strip()
    server = connection_parts.get("data source") or connection_parts.get("server")
    if not server:
        raise RuntimeError(
            "FABRIC_SQL_CONNECTION_STRING does not contain a Data Source or Server."
        )
    if ".database.fabric.microsoft.com" not in server:
        raise RuntimeError(
            "FABRIC_SQL_CONNECTION_STRING does not reference a Fabric SQL "
            "transactional endpoint."
        )
    server = server.split(",", 1)[0].replace(
        ".database.fabric.microsoft.com",
        ".datawarehouse.fabric.microsoft.com",
    )
    database = settings.fabric_sql_database_name

    if MODEL_ROOT.exists():
        shutil.rmtree(MODEL_ROOT)
    tables_root = MODEL_ROOT / "definition" / "tables"
    tables_root.mkdir(parents=True)

    model_lines = [
        "model Model",
        "\tculture: en-US",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tdiscourageImplicitMeasures",
        "\tsourceQueryCulture: en-US",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
    ]
    for source_name in DISPLAY_NAMES:
        model_lines.append(f"ref table {tmdl_name(DISPLAY_NAMES[source_name])}")
    model_lines.append("")
    (MODEL_ROOT / "definition" / "model.tmdl").write_text("\n".join(model_lines), encoding="utf-8")
    (MODEL_ROOT / "definition" / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1604\n", encoding="utf-8")
    (MODEL_ROOT / "definition" / "expressions.tmdl").write_text(
        "expression DatabaseQuery =\n"
        "\t\tlet\n"
        f'\t\t    Source = Sql.Database("{server}", "{database}")\n'
        "\t\tin\n"
        "\t\t    Source\n",
        encoding="utf-8",
    )

    relationship_lines: list[str] = []
    for name, from_table, from_column, to_table, to_column in RELATIONSHIPS:
        relationship_lines.extend(
            [
                f"relationship {name}",
                f"\tfromColumn: {tmdl_name(from_table)}.{tmdl_name(from_column)}",
                f"\ttoColumn: {tmdl_name(to_table)}.{tmdl_name(to_column)}",
                "",
            ]
        )
    (MODEL_ROOT / "definition" / "relationships.tmdl").write_text("\n".join(relationship_lines), encoding="utf-8")

    for source_name, frame in frames.items():
        (tables_root / f"{source_name}.tmdl").write_text(table_tmdl(source_name, frame), encoding="utf-8")

    definition = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2",
        "settings": {"qnaEnabled": False},
    }
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {
            "type": "SemanticModel",
            "displayName": "OGE Maintenance & Reliability Analytics",
            "description": "Direct Lake semantic model for facilities work management, preventive maintenance, labor, materials, asset reliability, and maintenance cost analysis.",
        },
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }
    (MODEL_ROOT / "definition.pbism").write_text(json.dumps(definition, indent=2), encoding="utf-8")
    (MODEL_ROOT / ".platform").write_text(json.dumps(platform, indent=2), encoding="utf-8")


def main() -> None:
    frames = prepare_tables()
    write_model(frames)
    summary = {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "tables": {name: len(frame) for name, frame in frames.items()},
        "table_count": len(frames),
        "semantic_model": "OGE Maintenance & Reliability Analytics",
        "relationship_count": len(RELATIONSHIPS),
        "measure_count": sum(len(measures) for measures in MEASURES.values()),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
