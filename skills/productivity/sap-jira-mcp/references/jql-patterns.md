# JQL Patterns for jira.tools.sap

Verified against jira.tools.sap in May 2026.

## Working JQL

### Count all open issues in a project
```
project = SAPBuild AND statusCategory != Done
```
Returns `total` field in JSON. Reliable count of all To Do + In Progress + any non-Done category.

### Get all issues in a project (for scanning)
```
project = SAPBuild AND statusCategory != Done
```
With `fields=summary,status,issuelinks` and `maxResults=100&startAt=N`

### All issues in a project (including Done)
```
project = MOBTECH
```

### Issues with any links
```
project = SAPBuild AND issueLinkType is not EMPTY
```
(Returns issues that have at least one link of any type — but doesn't filter by linked project)

## Broken JQL (HTTP 400 on this instance)

ScriptRunner plugin is NOT installed — all issueFunction JQL fails:
```
issueFunction in linkedIssuesOf("project = MOBTECH")        # 400
issueFunction in linkedIssuesOf("project = MOBTECH", "is")  # 400
```

Standard Jira advanced JQL that doesn't exist:
```
issue in linkedIssues("project = MOBTECH")           # 400
linkedIssue in projectIssues("MOBTECH")              # 400
project = SAPBuild AND issue in linkedIssues("project = MOBTECH")  # 400
project = SAPBuild AND issueLinkType is not EMPTY AND text ~ "MOBTECH"  # 400
```

## Link Directions in API Response

Each issue's `issuelinks` array contains objects with:
- `type.name` — e.g. "Parent-Child", "Resolution", "Relation", "Dependency"
- `inwardIssue` — the issue on the "inward" end of the link
- `outwardIssue` — the issue on the "outward" end of the link

Only one of `inwardIssue` / `outwardIssue` is present per link entry. Always check both.

## Link Types Observed in SAPBuild ↔ MOBTECH

- `Parent-Child` (outward) — most common; SAPBuild is the parent
- `Relation` (inward) — bidirectional relation
- `Resolution` (outward) — resolution/dependency tracking
- `Dependency` (outward) — explicit dependency

## Pagination

Total issues in SAPBuild (open): 1,776 as of May 2026
Pages needed at 100/page: 18
Approx response size per page with issuelinks: 200KB–1.8MB (varies by link density)

## Cross-Project Link Results (May 2026 snapshot)

SAPBuild open issues with links to MOBTECH: **22 unique SAPBuild issues**, **54 link entries total**

Top linked SAPBuild issues by link count:
- SAPBUILD-87 (Enhance Joule Mobile Client) — 9 MOBTECH links
- SAPBUILD-155 (Provide tablestakes for Differentiator) — 6 MOBTECH links
- SAPBUILD-68 (Vibe coding in Build Studio) — 4 MOBTECH links
- SAPBUILD-158 (Ongoing innovation) — 3 MOBTECH links
