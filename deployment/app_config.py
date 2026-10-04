from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "Copy .env.example to .env and configure it."
        )
    return value


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from error


def bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be a boolean.")


@dataclass(frozen=True)
class Settings:
    fabric_workspace_id: str
    fabric_workspace_name: str
    fabric_capacity_id: str
    fabric_capacity_resource_id: str
    fabric_lakehouse_id: str
    fabric_lakehouse_name: str
    fabric_sql_endpoint_id: str
    fabric_sql_database_id: str
    fabric_sql_database_name: str
    fabric_sql_connection_string: str
    enable_work_order_rag: bool
    fabric_semantic_model_id: str
    fabric_semantic_model_name: str
    fabric_data_agent_name: str
    azure_subscription_id: str
    azure_openai_resource: str
    azure_openai_resource_group: str
    azure_openai_embedding_deployment: str
    azure_openai_embedding_model: str
    azure_openai_embedding_dimensions: int
    azure_openai_api_version: str
    azure_ai_search_resource_group: str
    azure_ai_search_service: str
    azure_ai_search_location: str
    azure_ai_search_index: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            fabric_workspace_id=required_env("FABRIC_WORKSPACE_ID"),
            fabric_workspace_name=os.getenv(
                "FABRIC_WORKSPACE_NAME", "Facilities Maintenance & Reliability"
            ),
            fabric_capacity_id=required_env("FABRIC_CAPACITY_ID"),
            fabric_capacity_resource_id=required_env("FABRIC_CAPACITY_RESOURCE_ID"),
            fabric_lakehouse_id=os.getenv("FABRIC_LAKEHOUSE_ID", ""),
            fabric_lakehouse_name=os.getenv(
                "FABRIC_LAKEHOUSE_NAME", "FacilitiesMaintenanceLakehouse"
            ),
            fabric_sql_endpoint_id=os.getenv("FABRIC_SQL_ENDPOINT_ID", ""),
            fabric_sql_database_id=os.getenv("FABRIC_SQL_DATABASE_ID", ""),
            fabric_sql_database_name=os.getenv(
                "FABRIC_SQL_DATABASE_NAME", "FacilitiesMaintenanceOperational"
            ),
            fabric_sql_connection_string=os.getenv(
                "FABRIC_SQL_CONNECTION_STRING", ""
            ),
            enable_work_order_rag=bool_env("ENABLE_WORK_ORDER_RAG"),
            fabric_semantic_model_id=os.getenv("FABRIC_SEMANTIC_MODEL_ID", ""),
            fabric_semantic_model_name=os.getenv(
                "FABRIC_SEMANTIC_MODEL_NAME",
                "Maintenance & Reliability Analytics",
            ),
            fabric_data_agent_name=os.getenv(
                "FABRIC_DATA_AGENT_NAME", "Maintenance Operations Advisor"
            ),
            azure_subscription_id=required_env("AZURE_SUBSCRIPTION_ID"),
            azure_openai_resource=os.getenv("AZURE_OPENAI_RESOURCE", ""),
            azure_openai_resource_group=os.getenv(
                "AZURE_OPENAI_RESOURCE_GROUP", ""
            ),
            azure_openai_embedding_deployment=os.getenv(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", ""
            ),
            azure_openai_embedding_model=os.getenv(
                "AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"
            ),
            azure_openai_embedding_dimensions=int_env(
                "AZURE_OPENAI_EMBEDDING_DIMENSIONS", 1536
            ),
            azure_openai_api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION", "2024-10-21"
            ),
            azure_ai_search_resource_group=os.getenv(
                "AZURE_AI_SEARCH_RESOURCE_GROUP", ""
            ),
            azure_ai_search_service=os.getenv("AZURE_AI_SEARCH_SERVICE", ""),
            azure_ai_search_location=os.getenv(
                "AZURE_AI_SEARCH_LOCATION", "westus2"
            ),
            azure_ai_search_index=os.getenv(
                "AZURE_AI_SEARCH_INDEX", "completed-work-orders"
            ),
        )


settings = Settings.from_env()
