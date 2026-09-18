"""Thin client for Railway's public GraphQL API.

Endpoint and mutation/query names below are taken straight from
https://docs.railway.com/integrations/api/* — not guessed.
"""

import httpx

GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


class RailwayAPIError(Exception):
    pass


class RailwayClient:
    def __init__(self, token: str):
        self.token = token

    async def _call(self, query: str, variables: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables or {}}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GRAPHQL_URL, json=payload, headers=headers)
        try:
            body = resp.json()
        except Exception:
            raise RailwayAPIError(f"non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")

        if resp.status_code == 401:
            raise RailwayAPIError("unauthorized — token is invalid or revoked")
        if "errors" in body and body["errors"]:
            raise RailwayAPIError(body["errors"][0].get("message", str(body["errors"])))
        return body.get("data", {})

    async def validate(self) -> dict:
        """Returns account info if the token works. Raises RailwayAPIError if not."""
        data = await self._call("query { me { id name email } }")
        return data["me"]

    async def resolve_workspace_id(self) -> str | None:
        """projectCreate now requires a workspaceId for most accounts. There's
        no single documented field for "my default workspace", so try the
        shapes that are known to exist and take the first workspace found."""
        for query in (
            "query { me { workspaces { edges { node { id name } } } } }",
            "query { workspaces { edges { node { id name } } } }",
        ):
            try:
                data = await self._call(query)
            except RailwayAPIError:
                continue
            container = data.get("me", data).get("workspaces") if isinstance(data.get("me", data), dict) else None
            if container:
                edges = container.get("edges") or []
                if edges:
                    return edges[0]["node"]["id"]
        return None

    async def create_project(self, name: str, workspace_id: str | None = None) -> str:
        input_data = {"name": name}
        if workspace_id:
            input_data["workspaceId"] = workspace_id
        data = await self._call(
            "mutation projectCreate($input: ProjectCreateInput!) { projectCreate(input: $input) { id } }",
            {"input": input_data},
        )
        return data["projectCreate"]["id"]

    async def get_project(self, project_id: str) -> dict:
        data = await self._call(
            """
            query project($id: String!) {
              project(id: $id) {
                id
                name
                environments { edges { node { id name } } }
                services { edges { node { id name } } }
              }
            }
            """,
            {"id": project_id},
        )
        return data["project"]

    async def create_service_from_repo(self, project_id: str, name: str, repo: str, branch: str | None = None) -> str:
        source = {"repo": repo}
        variables = {"input": {"projectId": project_id, "name": name, "source": source}}
        if branch:
            variables["input"]["branch"] = branch
        data = await self._call(
            "mutation serviceCreate($input: ServiceCreateInput!) { serviceCreate(input: $input) { id } }",
            variables,
        )
        return data["serviceCreate"]["id"]

    async def set_variable(self, project_id: str, environment_id: str, service_id: str, name: str, value: str):
        await self._call(
            "mutation variableUpsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }",
            {"input": {"projectId": project_id, "environmentId": environment_id,
                       "serviceId": service_id, "name": name, "value": value}},
        )

    async def deploy(self, service_id: str, environment_id: str) -> str:
        data = await self._call(
            "mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!) { "
            "serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId) }",
            {"serviceId": service_id, "environmentId": environment_id},
        )
        return data["serviceInstanceDeployV2"]

    async def get_deployment_status(self, deployment_id: str) -> str:
        data = await self._call(
            "query deployment($id: String!) { deployment(id: $id) { id status } }",
            {"id": deployment_id},
        )
        return data["deployment"]["status"]

    async def create_domain(self, service_id: str, environment_id: str) -> str:
        data = await self._call(
            "mutation serviceDomainCreate($input: ServiceDomainCreateInput!) { "
            "serviceDomainCreate(input: $input) { id domain } }",
            {"input": {"serviceId": service_id, "environmentId": environment_id}},
        )
        return data["serviceDomainCreate"]["domain"]

    async def delete_project(self, project_id: str):
        await self._call(
            "mutation projectDelete($id: String!) { projectDelete(id: $id) }",
            {"id": project_id},
        )
