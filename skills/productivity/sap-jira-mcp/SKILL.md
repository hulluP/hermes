---
name: sap-jira-mcp
description: "SAP Jira REST API via sap-auth-mcp: search, count, cross-project link queries."
version: 1.0.0
author: Hermes Agent
platforms: [macos]
metadata:
  hermes:
    tags: [Jira, SAP, MCP, REST]
    related_skills: [native-mcp]
---

# SAP Jira via MCP

Query and manipulate SAP's internal Jira instance (jira.tools.sap) exclusively through the `sap-auth-mcp` MCP server. Never use browser, terminal, or direct HTTP for Jira work — only the three registered MCP tools.

## When to Use

- Counting or listing open issues in any SAP Jira project
- Finding cross-project issue links (e.g. SAPBuild → MOBTECH)
- Searching with JQL filters, status, assignee, priority
- Any Jira query/read task against jira.tools.sap

## MCP Tools Available

The SAP auth MCP server exposes three tools:

```
mcp_sap_auth_mcp_sap_get_cookie_info   — check if stored auth cookies are valid
mcp_sap_auth_mcp_sap_authenticate      — open headless browser to authenticate; stores cookies
mcp_sap_auth_mcp_sap_make_request      — make authenticated HTTP requests to SAP systems
```

**Always use the fully-qualified names above.** Bare names like `sap_authenticate` do NOT work.

## Authentication Flow

1. Call `mcp_sap_auth_mcp_sap_get_cookie_info` first — if cookies are valid, skip auth.
2. If no cookies or expired, call `mcp_sap_auth_mcp_sap_authenticate` with:
   - `entry_url`: `https://jira.tools.sap/`
   - `store_path`: a writable directory (e.g. `/tmp/sap_cookies`)
3. Once authenticated, all `mcp_sap_auth_mcp_sap_make_request` calls to jira.tools.sap will use the stored cookies automatically.

## Jira REST API Base URL

```
https://jira.tools.sap/rest/api/2/
```

Key endpoints:
- `/search` — JQL search (GET with `?jql=...`)
- `/issue/{key}` — single issue detail
- `/issue/{key}/remotelink` — remote (cross-instance) links

## JQL: What Works and What Doesn't

### Counting open issues in a project

```
project = SAPBuild AND statusCategory != Done
```

Returns `total` in the response — reliable for counting all non-Done items.

### Finding issues with any issue links

```
project = SAPBuild AND issueLinkType is not EMPTY
```

### What DOES NOT work on this Jira instance

These JQL forms return HTTP 400 — **do not attempt them**:

```
# ScriptRunner / issueFunction — NOT installed
issueFunction in linkedIssuesOf("project = MOBTECH")

# Nonstandard cross-project JQL
issue in linkedIssues("project = MOBTECH")
linkedIssue in projectIssues("MOBTECH")

# Text search on link keys
project = SAPBuild AND text ~ "MOBTECH"  (combined with issueLinkType)
```

There is no single JQL that finds "SAPBuild issues linked to any MOBTECH issue" — you must page-scan.

## Cross-Project Link Scan Pattern

To find all issues in project A that have links to project B (e.g. SAPBuild → MOBTECH):

1. Fetch all open issues from project A in pages of 100, requesting `issuelinks` field.
2. For each issue, inspect `fields.issuelinks[]` — check both `inwardIssue.key` and `outwardIssue.key`.
3. Filter keys starting with the target project prefix (e.g. `MOBTECH-`).
4. Save each page result to a temp file (responses are large, 200KB–1.8MB per page).
5. Parse all files with `execute_code` after all pages are fetched.

### JQL for page scan

```
project = SAPBuild AND statusCategory != Done
```

With params: `maxResults=100`, `startAt=N`, `fields=summary,status,issuelinks`

### Python parser snippet

```python
import json

def find_cross_project_links(path, target_prefix="MOBTECH-"):
    with open(path) as f:
        raw = f.read()
    data = json.loads(raw)
    result = json.loads(data["result"])
    found = []
    for issue in result["issues"]:
        links = issue.get("fields", {}).get("issuelinks", []) or []
        for link in links:
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction)
                if linked and linked.get("key", "").startswith(target_prefix):
                    found.append({
                        "key": issue["key"],
                        "summary": issue["fields"]["summary"],
                        "status": issue["fields"]["status"]["name"],
                        "link_type": link.get("type", {}).get("name", ""),
                        "direction": direction,
                        "linked_key": linked["key"],
                        "linked_summary": (linked.get("fields") or {}).get("summary", "")
                    })
    return found, len(result["issues"])
```

## Rate Limiting

The MCP server rate-limits after ~4–5 consecutive calls that return HTTP 400 or errors. Symptoms:
- Error: `MCP server 'sap-auth-mcp' is unreachable after N consecutive failures. Auto-retry available in ~56s.`

**Fix**: `sleep 60` then retry. Change query strategy before retrying — never retry the exact same failing call.

## Response Size

Responses with `issuelinks` field are very large (200KB–1.8MB per 100 issues). The MCP framework saves them to temp files automatically. Use `read_file` or `execute_code` to parse them — never try to read the full content inline.

## Known Projects

- `SAPBuild` — SAP Build platform (1,776 open issues as of May 2026)
- `MOBTECH` — Mobile Technology (2,622 total issues as of May 2026)

## References

- `references/jql-patterns.md` — tested JQL patterns and known failures
