"""Sacramento County food-inspection lead scraper: pulls the county's public data,
scores it for the commercial rodent/pest wedge, and pushes leads into FieldRoutes.

See docs/lead-scraper-plan.md for the full design. This package is a sibling to
the MCP server, not part of it -- `fr-leads` is a separate console script so a
cron run never has to construct the MCPServer or its 31 tools.
"""
