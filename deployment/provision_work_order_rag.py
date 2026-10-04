from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from azure.identity import AzureCliCredential

from app_config import required_env, settings


ROOT = Path(__file__).resolve().parent.parent
PARQUET_ROOT = ROOT / "generated" / "data" / "parquet"
OUTPUT_PATH = ROOT / "generated" / "work-order-rag-deployment.json"
MANAGEMENT_API = "https://management.azure.com"
SEARCH_API_VERSION = "2024-07-01"
EMBEDDING_API_VERSION = settings.azure_openai_api_version
EMBEDDING_DIMENSIONS = settings.azure_openai_embedding_dimensions
AZURE_CLI = shutil.which("az")


def batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def az(*arguments: str) -> str:
    if not AZURE_CLI:
        raise RuntimeError("Azure CLI was not found.")
    result = subprocess.run(
        [AZURE_CLI, *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def management_headers(credential: AzureCliCredential) -> dict[str, str]:
    token = credential.get_token("https://management.azure.com/.default").token
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


def ensure_search_service(credential: AzureCliCredential) -> tuple[str, str]:
    subscription_id = required_env("AZURE_SUBSCRIPTION_ID")
    resource_group = (
        settings.azure_ai_search_resource_group
        or settings.azure_openai_resource_group
    )
    if not resource_group:
        raise RuntimeError("AZURE_AI_SEARCH_RESOURCE_GROUP is required.")
    service_name = settings.azure_ai_search_service or (
        "ogeworkorders" + settings.fabric_workspace_id.replace("-", "")[:8]
    )
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Search/searchServices/{service_name}"
    )
    uri = f"{MANAGEMENT_API}{resource_id}?api-version=2023-11-01"
    response = requests.put(
        uri,
        headers=management_headers(credential),
        json={
            "location": settings.azure_ai_search_location,
            "sku": {"name": "basic"},
            "identity": {"type": "SystemAssigned"},
            "properties": {
                "replicaCount": 1,
                "partitionCount": 1,
                "hostingMode": "default",
                "semanticSearch": "standard",
                "disableLocalAuth": False,
                "authOptions": {
                    "aadOrApiKey": {
                        "aadAuthFailureMode": "http401WithBearerChallenge"
                    }
                },
            },
        },
        timeout=120,
    )
    response.raise_for_status()
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = requests.get(
            uri, headers=management_headers(credential), timeout=60
        )
        current.raise_for_status()
        payload = current.json()
        if payload["properties"]["status"] == "running":
            principal_id = payload["identity"]["principalId"]
            break
        time.sleep(10)
    else:
        raise TimeoutError("Azure AI Search did not become ready within 15 minutes.")

    openai_id = az(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        required_env("AZURE_OPENAI_RESOURCE"),
        "--resource-group",
        required_env("AZURE_OPENAI_RESOURCE_GROUP"),
        "--query",
        "id",
        "-o",
        "tsv",
    )
    assignment = az(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        principal_id,
        "--role",
        "Cognitive Services OpenAI User",
        "--scope",
        openai_id,
        "--query",
        "[0].id",
        "-o",
        "tsv",
    )
    if not assignment:
        az(
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            principal_id,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            "Cognitive Services OpenAI User",
            "--scope",
            openai_id,
            "--output",
            "none",
        )
    return service_name, resource_group


def build_documents() -> list[dict[str, Any]]:
    work_orders = pd.read_parquet(PARQUET_ROOT / "work_orders.parquet")
    assets = pd.read_parquet(PARQUET_ROOT / "assets.parquet")[
        ["assetnum", "description", "asset_class", "manufacturer", "model"]
    ].rename(columns={"description": "asset_description"})
    sites = pd.read_parquet(PARQUET_ROOT / "sites.parquet")[
        ["siteid", "description", "facility_type"]
    ].rename(columns={"description": "site_description"})
    records = (
        work_orders[work_orders["status"].isin({"COMP", "CLOSE"})]
        .merge(assets, on="assetnum", how="left")
        .merge(sites, on="siteid", how="left")
    )
    documents: list[dict[str, Any]] = []
    for record in records.to_dict(orient="records"):
        fields = [
            ("Work order", record["wonum"]),
            ("Problem", record["description"]),
            ("Details", record.get("long_description")),
            ("Asset", record.get("asset_description")),
            ("Asset class", record.get("asset_class")),
            ("Manufacturer", record.get("manufacturer")),
            ("Model", record.get("model")),
            ("Site", record.get("site_description")),
            ("Facility type", record.get("facility_type")),
            ("Work type", record.get("worktype")),
            ("Failure class", record.get("failure_class")),
            ("Problem code", record.get("problem_code")),
            ("Cause code", record.get("cause_code")),
            ("Remedy code", record.get("remedy_code")),
        ]
        content = "\n".join(
            f"{label}: {value}"
            for label, value in fields
            if value is not None and not pd.isna(value) and str(value).strip()
        )
        document = {
            "id": record["wonum"],
            "wonum": record["wonum"],
            "title": f"{record['wonum']} - {record['description']}",
            "content": content,
        }
        for field in (
            "siteid",
            "assetnum",
            "asset_description",
            "asset_class",
            "status",
            "worktype",
            "failure_class",
            "problem_code",
            "cause_code",
            "remedy_code",
        ):
            value = record.get(field)
            document[field] = None if value is None or pd.isna(value) else str(value)
        actual_finish = record.get("actual_finish")
        if isinstance(actual_finish, pd.Timestamp):
            actual_finish = actual_finish.to_pydatetime()
        if isinstance(actual_finish, (datetime, date)):
            document["actual_finish"] = actual_finish.isoformat()
        else:
            document["actual_finish"] = None
        documents.append(document)
    return documents


def generate_embeddings(
    credential: AzureCliCredential, texts: list[str]
) -> list[list[float]]:
    endpoint = (
        f"https://{required_env('AZURE_OPENAI_RESOURCE')}.openai.azure.com/"
        f"openai/deployments/{required_env('AZURE_OPENAI_EMBEDDING_DEPLOYMENT')}/"
        f"embeddings?api-version={EMBEDDING_API_VERSION}"
    )
    embeddings: list[list[float]] = []
    for batch in batches(texts, 16):
        for attempt in range(10):
            token = credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                json={"input": batch, "dimensions": EMBEDDING_DIMENSIONS},
                timeout=120,
            )
            if response.status_code != 429:
                response.raise_for_status()
                break
            retry_after = int(response.headers.get("Retry-After", "10"))
            time.sleep(max(retry_after, min(60, 5 * (attempt + 1))))
        else:
            raise RuntimeError("Azure OpenAI embedding requests remained throttled.")
        embeddings.extend(
            item["embedding"]
            for item in sorted(response.json()["data"], key=lambda item: item["index"])
        )
    return embeddings


def search_headers(service_name: str) -> dict[str, str]:
    key = az(
        "search",
        "admin-key",
        "show",
        "--resource-group",
        settings.azure_ai_search_resource_group
        or settings.azure_openai_resource_group,
        "--service-name",
        service_name,
        "--query",
        "primaryKey",
        "-o",
        "tsv",
    )
    return {"api-key": key, "Content-Type": "application/json"}


def create_index(service_name: str) -> None:
    endpoint = f"https://{service_name}.search.windows.net"
    index_name = settings.azure_ai_search_index
    fields = [
        {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
        {
            "name": "wonum",
            "type": "Edm.String",
            "searchable": True,
            "filterable": True,
            "retrievable": True,
        },
        {"name": "title", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
    ]
    for name in (
        "siteid",
        "assetnum",
        "asset_description",
        "asset_class",
        "status",
        "worktype",
        "failure_class",
        "problem_code",
        "cause_code",
        "remedy_code",
    ):
        fields.append(
            {
                "name": name,
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            }
        )
    fields.extend(
        [
            {
                "name": "actual_finish",
                "type": "Edm.DateTimeOffset",
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": EMBEDDING_DIMENSIONS,
                "vectorSearchProfile": "work-order-vector-profile",
            },
        ]
    )
    payload = {
        "name": index_name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "work-order-hnsw",
                    "kind": "hnsw",
                    "hnswParameters": {"metric": "cosine"},
                }
            ],
            "profiles": [
                {
                    "name": "work-order-vector-profile",
                    "algorithm": "work-order-hnsw",
                    "vectorizer": "work-order-openai-vectorizer",
                }
            ],
            "vectorizers": [
                {
                    "name": "work-order-openai-vectorizer",
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": (
                            f"https://{settings.azure_openai_resource}.openai.azure.com"
                        ),
                        "deploymentId": settings.azure_openai_embedding_deployment,
                        "modelName": settings.azure_openai_embedding_model,
                    },
                }
            ],
        },
        "semantic": {
            "configurations": [
                {
                    "name": "work-order-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "wonum"},
                            {"fieldName": "asset_description"},
                            {"fieldName": "problem_code"},
                            {"fieldName": "cause_code"},
                            {"fieldName": "remedy_code"},
                        ],
                    },
                }
            ]
        },
    }
    response = requests.put(
        f"{endpoint}/indexes/{index_name}?api-version={SEARCH_API_VERSION}",
        headers=search_headers(service_name),
        json=payload,
        timeout=120,
    )
    response.raise_for_status()


def upload_documents(
    service_name: str, documents: list[dict[str, Any]], embeddings: list[list[float]]
) -> None:
    endpoint = f"https://{service_name}.search.windows.net"
    headers = search_headers(service_name)
    enriched = [
        {**document, "content_vector": embedding, "@search.action": "mergeOrUpload"}
        for document, embedding in zip(documents, embeddings, strict=True)
    ]
    for batch in batches(enriched, 100):
        response = requests.post(
            f"{endpoint}/indexes/{settings.azure_ai_search_index}/docs/index"
            f"?api-version={SEARCH_API_VERSION}",
            headers=headers,
            json={"value": batch},
            timeout=120,
        )
        response.raise_for_status()
        failures = [item for item in response.json()["value"] if not item["status"]]
        if failures:
            raise RuntimeError(f"Azure AI Search indexing failed: {failures[:3]}")


def smoke_test(
    credential: AzureCliCredential, service_name: str
) -> list[dict[str, Any]]:
    query = "pump with low discharge pressure and mechanical problems"
    query_vector = generate_embeddings(credential, [query])[0]
    response = requests.post(
        f"https://{service_name}.search.windows.net/indexes/"
        f"{settings.azure_ai_search_index}/docs/search"
        f"?api-version={SEARCH_API_VERSION}",
        headers=search_headers(service_name),
        json={
            "search": query,
            "queryType": "semantic",
            "semanticConfiguration": "work-order-semantic",
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": query_vector,
                    "fields": "content_vector",
                    "k": 25,
                }
            ],
            "filter": "status eq 'COMP' or status eq 'CLOSE'",
            "select": (
                "wonum,assetnum,asset_description,description,asset_class,"
                "problem_code,cause_code,remedy_code"
            ).replace(",description", ",title"),
            "top": 5,
        },
        timeout=120,
    )
    response.raise_for_status()
    return [
        {
            "wonum": item["wonum"],
            "assetnum": item.get("assetnum"),
            "assetDescription": item.get("asset_description"),
            "title": item.get("title"),
            "problemCode": item.get("problem_code"),
            "causeCode": item.get("cause_code"),
            "remedyCode": item.get("remedy_code"),
            "score": item.get("@search.score"),
            "rerankerScore": item.get("@search.rerankerScore"),
        }
        for item in response.json()["value"]
    ]


def main() -> None:
    if not settings.enable_work_order_rag:
        result = {"enabled": False}
    else:
        credential = AzureCliCredential()
        service_name, resource_group = ensure_search_service(credential)
        documents = build_documents()
        embeddings = generate_embeddings(
            credential, [document["content"] for document in documents]
        )
        create_index(service_name)
        upload_documents(service_name, documents, embeddings)
        results = smoke_test(credential, service_name)
        result = {
            "enabled": True,
            "resourceGroup": resource_group,
            "serviceName": service_name,
            "endpoint": f"https://{service_name}.search.windows.net",
            "indexName": settings.azure_ai_search_index,
            "documentCount": len(documents),
            "embeddingModel": settings.azure_openai_embedding_model,
            "embeddingDimensions": EMBEDDING_DIMENSIONS,
            "smokeTestResults": results,
        }
    OUTPUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
