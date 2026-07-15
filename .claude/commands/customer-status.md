# /customer-status — Customer Self-Service Portal

Check work status using a customer license key. No authentication required beyond the license key.

## Input

$ARGUMENTS

Required:
- `<license_key>` — Customer license key (from their purchase)

Optional:
- `--json` — Output as JSON instead of formatted display
- `--feedback "<message>"` — Submit feedback instead of reading status. Optionally pair with `--request-id <task-id>` to tie feedback to a specific work item. Feedback lands in `.company/customers/<customer_id>/feedback.json` and will be surfaced to the assigned project lead.

## Step 1: Validate License

```bash
# Read-only status
uv run .claude/hooks/company/customer_portal.py status $LICENSE_KEY

# Submit feedback
uv run .claude/hooks/company/customer_portal.py feedback $LICENSE_KEY "Need dark mode" --request-id task-20260419-abc
```

If invalid:
```
═══════════════════════════════════════════════════════════════════════════════
 CUSTOMER STATUS                                                    [ERROR]
═══════════════════════════════════════════════════════════════════════════════

 Invalid license key.

 If you believe this is an error, contact support.

═══════════════════════════════════════════════════════════════════════════════
```

## Step 2: Display Status

For valid license, show:

```
═══════════════════════════════════════════════════════════════════════════════
 CUSTOMER STATUS                                       [YYYY-MM-DD HH:MM UTC]
═══════════════════════════════════════════════════════════════════════════════

 Customer: [Customer Name]
 Plan: [plan]
 Status: [active/trial/suspended]

───────────────────────────────────────────────────────────────────────────────
 SUMMARY
───────────────────────────────────────────────────────────────────────────────
 Total Requests: [N]
   Pending:     [N]
   In Progress: [N]
   Completed:   [N]
   Blocked:     [N]

───────────────────────────────────────────────────────────────────────────────
 RECENT REQUESTS
───────────────────────────────────────────────────────────────────────────────
 ● [Completed task title] → PR #123
 ◐ [In progress task title]
 ○ [Pending task title]

───────────────────────────────────────────────────────────────────────────────
 RECENT NOTIFICATIONS
───────────────────────────────────────────────────────────────────────────────
 • Your feature "Add auth" was deployed (PR #123)
 • Work started on "API endpoint"

═══════════════════════════════════════════════════════════════════════════════
```

## Legend

| Icon | Meaning |
|------|---------|
| ● | Completed |
| ◐ | In Progress |
| ○ | Pending |
| ✗ | Blocked |

## Rules

- **License key = access** — No additional auth required
- **Hide sensitive data** — Don't expose internal task IDs or employee assignments
- **Show PR links** — Customers can see their PRs directly
- **Recent items only** — Show last 10 requests, last 5 notifications
