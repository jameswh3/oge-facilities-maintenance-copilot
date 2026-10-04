from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
from azure.identity import AzureCliCredential
from fabric.analytics.environment.credentials import SetFabricAnalyticsDefaultTokenCredentialsGlobally
from fabric.dataagent.client import FabricDataAgentManagement, create_data_agent
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app_config import required_env, settings


WORKSPACE_ID = settings.fabric_workspace_id
SEMANTIC_MODEL_ID = required_env("FABRIC_SEMANTIC_MODEL_ID")
SEMANTIC_MODEL_NAME = settings.fabric_semantic_model_name
AGENT_NAME = settings.fabric_data_agent_name
CAPACITY_ID = settings.fabric_capacity_id
CAPACITY_RESOURCE_ID = settings.fabric_capacity_resource_id
OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "generated"
    / "data-agent-deployment.json"
)
ANALYTICS_SMOKE_TEST_QUESTION = (
    "How many work orders are in the model, and how many are currently open?"
)
AGENT_INSTRUCTIONS = """You are the OGE Maintenance Operations Advisor for a synthetic utility facilities maintenance dataset. Use the OGE Maintenance & Reliability Analytics semantic model for metrics.

All records are synthetic demonstration data, not real OGE operational records. Prefer the semantic model's explicit measures over implicit aggregation. Work Order Count counts work orders. Open Work Order Count excludes COMP, CLOSE, and CAN statuses. Completed Work Order Count includes COMP and CLOSE. High Priority Backlog means open work orders with Priority 1 or 2. Completion Rate is completed work orders divided by all work orders. Maintenance Cost Variance is actual minus estimated cost. Labor Hour Variance is actual minus estimated labor hours. Average Completion Days measures report-to-finish elapsed days for completed work.

Sites filter work orders by Site ID. Assets filter work orders, meter readings, and preventive maintenance by Asset Number. Work orders filter labor transactions, material transactions, and status history by Work Order Number. The Date table filters work orders using Report Date Key. Monetary measures are USD.

For totals, rankings, trends, and comparisons, state the measure and filters used. For rankings, return the requested number of rows and include the ranked metric. Distinguish work-order header actual cost from transaction labor cost and material cost. Do not claim an unrecorded root cause, predict equipment failure, recommend unsafe maintenance, or treat the synthetic records as live operational telemetry. If the requested field or measure is unavailable, say what is missing rather than inventing it. Keep answers concise and mention that the data is synthetic when the result could be used for an operational decision."""

SOURCE_INSTRUCTIONS = """Use DAX against this Direct Lake semantic model. Prefer these measures when applicable: Work Order Count, Open Work Order Count, Completed Work Order Count, Completion Rate, Emergency Work Order Count, High Priority Backlog, Total Estimated Labor Hours, Total Actual Labor Hours, Labor Hour Variance, Estimated Maintenance Cost, Actual Maintenance Cost, Maintenance Cost Variance, Average Completion Days, Asset Count, Critical Asset Count, Total Replacement Value, Active PM Count, Overdue PM Count, Transaction Labor Hours, Transaction Labor Cost, Material Cost, and Material Quantity.

Use Sites[Description] for facility names, Assets[Description] for equipment names, Work Orders[Status] for workflow status, Work Orders[Work Type] for CM/PM/INSP/EM/CAL, Work Orders[Priority] for priority, and Date fields for report-date trends. Respect model relationships and do not manually join unrelated tables."""

def fabric_headers(credential: AzureCliCredential) -> dict[str, str]:
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    return {"Authorization": f"Bearer {token}"}


def ensure_capacity_active(credential: AzureCliCredential) -> None:
    headers = fabric_headers(credential)
    response = requests.get("https://api.fabric.microsoft.com/v1/capacities", headers=headers, timeout=60)
    response.raise_for_status()
    capacity = next(item for item in response.json()["value"] if item["id"] == CAPACITY_ID)
    if capacity["state"] == "Active":
        return
    subprocess.run(
        [
            "az",
            "resource",
            "invoke-action",
            "--ids",
            CAPACITY_RESOURCE_ID,
            "--action",
            "resume",
            "--api-version",
            "2023-11-01",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(10)
        headers = fabric_headers(credential)
        response = requests.get("https://api.fabric.microsoft.com/v1/capacities", headers=headers, timeout=60)
        response.raise_for_status()
        capacity = next(item for item in response.json()["value"] if item["id"] == CAPACITY_ID)
        if capacity["state"] == "Active":
            return
    raise TimeoutError("Fabric capacity did not become active within 10 minutes.")


def find_agent(credential: AzureCliCredential) -> dict[str, Any] | None:
    response = requests.get(
        f"https://api.fabric.microsoft.com/v1/workspaces/{WORKSPACE_ID}/items",
        headers=fabric_headers(credential),
        timeout=60,
    )
    response.raise_for_status()
    return next(
        (
            item
            for item in response.json()["value"]
            if item["type"] == "DataAgent" and item["displayName"] == AGENT_NAME
        ),
        None,
    )


def is_target_source(configuration: dict[str, Any], artifact_id: str) -> bool:
    return (
        configuration.get("artifactId") == artifact_id
        or configuration.get("id") == artifact_id
        or configuration.get("itemReference", {}).get("itemId") == artifact_id
    )


def wait_for_staging_source(
    agent: FabricDataAgentManagement,
    artifact_id: str,
    timeout_seconds: int = 120,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for candidate in agent.list_datasources(stage="staging"):
            configuration = candidate.get_configuration(stage="staging")
            if is_target_source(configuration, artifact_id):
                return candidate
        time.sleep(5)
    raise TimeoutError(
        f"Data Agent staging source {artifact_id} was not available within "
        f"{timeout_seconds} seconds."
    )


def configure_agent(credential: AzureCliCredential) -> tuple[str, FabricDataAgentManagement]:
    existing = find_agent(credential)
    if existing:
        agent_id = existing["id"]
        agent = FabricDataAgentManagement(agent_id, WORKSPACE_ID)
    else:
        agent = create_data_agent(data_agent_name=AGENT_NAME, workspace_id=WORKSPACE_ID)
        created = find_agent(credential)
        if not created:
            raise RuntimeError("The Data Agent create operation completed but the item was not found.")
        agent_id = created["id"]

    agent.update_settings(ai_instructions=AGENT_INSTRUCTIONS)
    data_sources = agent.list_datasources(stage="staging")
    source = None
    for candidate in data_sources:
        configuration = candidate.get_configuration(stage="staging")
        if is_target_source(configuration, SEMANTIC_MODEL_ID):
            source = candidate
            break
    if source is None:
        agent.add_staging_datasource(
            artifact_name_or_id=SEMANTIC_MODEL_ID,
            workspace_id_or_name=WORKSPACE_ID,
            type="semantic_model",
        )
        source = wait_for_staging_source(agent, SEMANTIC_MODEL_ID)
    source.update_configuration(
        instructions=SOURCE_INSTRUCTIONS,
        description=(
            "Direct Lake semantic model for synthetic facilities work orders, assets, "
            "preventive maintenance, labor, materials, meter readings, and maintenance costs."
        ),
    )

    if settings.fabric_sql_database_id:
        for candidate in agent.list_datasources(stage="staging"):
            configuration = candidate.get_configuration(stage="staging")
            if is_target_source(configuration, settings.fabric_sql_database_id):
                candidate.delete()
                break
    agent.publish_staging(
        description=(
            "Published maintenance analytics advisor grounded in the OGE Maintenance & "
            "Reliability Analytics Direct Lake semantic model."
        )
    )
    return agent_id, agent


async def ask_agent(credential: AzureCliCredential, agent_id: str, question: str) -> str:
    mcp_url = (
        f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{WORKSPACE_ID}"
        f"/dataagents/{agent_id}/agent"
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        headers = fabric_headers(credential)
        try:
            async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if not tools.tools:
                        raise RuntimeError("The published Data Agent exposes no MCP tools.")
                    tool = tools.tools[0]
                    question_arg = next(iter(tool.inputSchema["properties"]))
                    result = await session.call_tool(tool.name, {question_arg: question})
                    answers = [block.text for block in result.content if block.type == "text"]
                    answer = "\n".join(answers)
                    if not answer:
                        raise RuntimeError("The Data Agent returned an empty MCP response.")
                    return answer
        except (requests.ConnectionError, TimeoutError, RuntimeError):
            await asyncio.sleep(15)
    raise TimeoutError("The published Data Agent MCP endpoint was not ready within 5 minutes.")


def validate_analytics_smoke_test(response: str) -> None:
    normalized = response.replace(",", "")
    if "1500" not in normalized or "549" not in normalized:
        raise RuntimeError(
            "The analytics smoke test did not return the expected deterministic "
            "counts (1,500 total and 549 open work orders). Response: "
            f"{response}"
        )


def main() -> None:
    credential = AzureCliCredential()
    SetFabricAnalyticsDefaultTokenCredentialsGlobally(credential)
    ensure_capacity_active(credential)
    agent_id, agent = configure_agent(credential)
    published_settings = agent.get_settings(stage="published")
    analytics_response = asyncio.run(
        ask_agent(
            credential,
            agent_id,
            ANALYTICS_SMOKE_TEST_QUESTION,
        )
    )
    validate_analytics_smoke_test(analytics_response)
    result = {
        "agentId": agent_id,
        "agentName": AGENT_NAME,
        "workspaceId": WORKSPACE_ID,
        "semanticModelId": SEMANTIC_MODEL_ID,
        "semanticModelName": SEMANTIC_MODEL_NAME,
        "published": True,
        "publishedSettings": published_settings,
        "fewShotCount": 0,
        "mcpUrl": (
            f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{WORKSPACE_ID}"
            f"/dataagents/{agent_id}/agent"
        ),
        "smokeTestQuestion": ANALYTICS_SMOKE_TEST_QUESTION,
        "smokeTestResponse": analytics_response,
    }
    OUTPUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
