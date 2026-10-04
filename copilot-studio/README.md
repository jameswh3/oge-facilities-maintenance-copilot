# Copilot Studio project

`settings.mcs.yml` contains the reusable agent identity and instructions.

Copilot Studio pull output includes tenant-specific Dataverse endpoints, environment
IDs, user IDs, bot IDs, connection IDs, and Fabric artifact IDs. For that reason, the
following paths are local-only:

- `.mcs/`
- `infrastructure/connections/`
- `translations/*.mcs.yml`

In a target environment:

1. Create an agent using the GitHub Copilot harness.
2. Apply the instructions in `settings.mcs.yml`.
3. Add **Fabric IQ Data MCP** as a tool.
4. Select the Fabric Data Agent created by `deployment/provision_data_agent.py`.
5. Run `deployment/provision_copilot_knowledge.ps1` after SharePoint provisioning.
   It idempotently creates or updates the SOP and technical-manual knowledge sources,
   populates their descriptions, and publishes the agent. When
   `ENABLE_WORK_ORDER_RAG=true`, it also creates or updates the standard Azure AI
   Search connector tool for the `completed-work-orders` index.
6. For optional work-order RAG, create the generated Azure AI Search connection using
   **Access Key** authentication, endpoint
   `https://<service-name>.search.windows.net`, and an administrator key retrieved
   during configuration. Power Platform stores the key in its encrypted connection
   store; do not write it to repository files or deployment JSON.
7. Use invoker authentication for Fabric and Azure AI Search, share the Search
   connection only with authorized agent users, and validate end-user permissions.
8. Pull the configured project locally with `pac copilot pull` if source synchronization
   is required. Keep the environment-specific pull output uncommitted.

The analogous-work-order path is a Microsoft connector action, not a custom MCP
server. The tool fixes the index name, vector field, completed-status filter, selected
citation fields, and five-result limit while allowing the agent to supply the user's
request as search text.
