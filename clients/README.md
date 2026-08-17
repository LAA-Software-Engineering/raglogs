# OpenAPI and generated clients

Canonical HTTP routes live under `/v1/`. Export the spec with:

```bash
make openapi
# writes clients/openapi.json
```

`clients/openapi.json` is the published contract. CI uploads it as a workflow
artifact on every push/PR and attaches it to GitHub Releases on tags.

## JSON Schema (`/v1/query/*`)

Versioned response bodies (`schema_version` 1.0) are published as JSON Schema:

```bash
make jsonschema
# writes clients/jsonschema/explain.v1.json (and timeline/compare/ask/clusters/similar)
```

## Python

A thin typed httpx client ships in-tree:

```python
from src.clients.v1 import RaglogsClient

with RaglogsClient("http://localhost:8000", token="rlk_…") as client:
    explanation = client.explain(since="30m", no_llm=True)
```

`make client-python` optionally runs `openapi-python-client` into
`clients/python/generated/` when that tool is installed. Generated trees are
gitignored; prefer the committed client for day-to-day use.

```bash
pip install openapi-python-client   # optional
make client-python
```

## Go

```bash
go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest
make client-go
```

`make client-go` writes `clients/go/client.go` when `oapi-codegen` is on
`PATH`. If the binary is missing it prints the install command above and
exits 0 so CI is not required to have Go.
