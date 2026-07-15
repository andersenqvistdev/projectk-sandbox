# /competitive-intelligence — Research and Document a Competitive Intelligence Brief

You are the Revenue & Sales Lead producing a structured competitive intelligence brief. This command replaces the manual "build competitive intelligence brief" task that has been run multiple times. Run it whenever a competitor analysis is needed.

## Input

$ARGUMENTS

Parse $ARGUMENTS as one of:
- A single competitor name: `"Devin"`, `"Cursor"`, `"Copilot"`
- A comma-separated list: `"Cursor, Devin, Copilot"`
- A category phrase: `"top 3 AI agent frameworks"`, `"Forge vs alternatives"`
- No argument: default to the three most commonly cited competitor categories

## Step 1: Load Forge Context

Read these files to ground the brief in Forge's actual position:

- `CLAUDE.md` — Core principles and structured autonomy positioning
- `SECURITY.md` — Security philosophy (key differentiator)
- `.claude/agents/company/product/marketing-lead.md` — Existing competitive positioning table

Extract: core value proposition, key differentiators, target audiences, and any existing competitor notes.

## Step 2: Identify Competitors

If specific competitors were named in $ARGUMENTS, use those.

If the input is a category phrase (e.g., "top 3 AI agent frameworks"), use WebSearch to identify the current top competitors in that category:

```
WebSearch: "top AI agent framework competitors 2024 developer tools"
WebSearch: "Forge framework alternatives site:github.com OR site:reddit.com"
```

Resolve to a concrete list (max 5 competitors). Log the list before proceeding.

## Step 3: Research Each Competitor

For each competitor, run WebSearch to gather:

**Core research queries:**
```
WebSearch: "[Competitor] product overview features 2024"
WebSearch: "[Competitor] pricing plans"
WebSearch: "[Competitor] vs alternatives site:reddit.com OR site:hackernews"
WebSearch: "[Competitor] target customers use cases"
```

**Capture for each:**
- What they do (one-sentence product summary)
- Positioning / core claim (how they describe themselves)
- Pricing model and tiers
- Key features
- Target customer (role, company size, tech stack)
- Notable weaknesses or complaints (from reviews, forums, GitHub issues)
- GitHub stars / adoption signals if available

## Step 4: Produce the Brief

For each competitor, produce a brief using this exact format:

```markdown
## [Competitor Name]

**Website:** [URL]
**Category:** [e.g., Full-automation coding agent / IDE AI assistant / Agent framework]
**Last Updated:** [YYYY-MM-DD]

### Product Overview

[2-3 sentence summary of what they do and their core claim]

### Positioning

**Their tagline / pitch:** "[Exact quote or close paraphrase]"

**Target audience:** [Role + company size + tech stack]

### Pricing

| Tier | Price | What's Included |
|------|-------|-----------------|
| [Tier] | $[X]/mo | [Features] |
| [Tier] | $[X]/mo | [Features] |

### Key Features

| Feature | Forge Has This? | Notes |
|---------|-----------------|-------|
| [Feature] | Yes / No / Partial | [Differentiation note] |
| [Feature] | Yes / No / Partial | [Differentiation note] |

### Weaknesses & Gaps

1. [Weakness from user feedback or structural limitation]
2. [Weakness]
3. [Weakness]

### Win/Loss Scenarios

**We win when:** [Context where Forge beats them]

**We lose when:** [Context where they beat Forge or are considered first]

### Counter-Messaging

| Their Claim | Our Counter |
|-------------|-------------|
| "[Their claim]" | "[Forge counter-argument]" |
| "[Their claim]" | "[Forge counter-argument]" |
```

## Step 5: Produce the Summary Comparison

After individual briefs, produce a comparison table:

```markdown
## Competitive Landscape Summary

| Competitor | Category | Price Entry | Key Strength | Key Weakness | Forge Wins When |
|------------|----------|-------------|--------------|--------------|-----------------|
| [Name] | [Category] | $[X]/mo | [Strength] | [Weakness] | [Scenario] |
| [Name] | [Category] | $[X]/mo | [Strength] | [Weakness] | [Scenario] |

## Forge's Defensible Position

**Against full-automation tools (Devin-style):**
[How Forge's structured autonomy wins vs. fully autonomous agents]

**Against IDE AI assistants (Copilot/Cursor-style):**
[How Forge's workflow-level integration wins vs. single-file AI]

**Against generic agent frameworks (LangChain-style):**
[How Forge's opinionated, safety-first design wins vs. DIY frameworks]

## Recommended Sales Talking Points

1. [Top talking point based on this research]
2. [Second talking point]
3. [Third talking point]

## Intelligence Gaps

- [ ] [What we couldn't find / needs manual follow-up]
- [ ] [What changes frequently and should be re-checked quarterly]
```

## Step 6: Save the Output

Save the complete brief to:

```
.company/sales/competitive/[slug]-[YYYY-MM-DD].md
```

Where `[slug]` is:
- Single competitor: `cursor-2024-07-12`
- Multiple competitors: `ai-agent-frameworks-2024-07-12`
- Category: `forge-vs-alternatives-2024-07-12`

Create the `.company/sales/competitive/` directory if it does not exist.

Also update `.company/sales/competitive/INDEX.md` — append a line:
```
- [YYYY-MM-DD] [Title] → [filename].md
```

## Rules

1. **Always read Forge context first.** Counter-messaging must be grounded in what Forge actually does, not generic claims.

2. **Use real prices.** If pricing is unavailable publicly, mark as `[Contact sales]` or `[Freemium, details unclear]` — never guess.

3. **Cite sources.** Each competitor section should note where data came from (website, Reddit, GitHub, etc.) at the bottom.

4. **One file per run.** Save a timestamped file each time. Do not overwrite past briefs — they are historical snapshots.

5. **Flag staleness.** Competitor pricing and features change. Note which items are most volatile and set a re-check reminder in the Intelligence Gaps section.

6. **No editorializing against competitors.** Factual weaknesses from user feedback are fair. Personal attacks or speculation are not. Keep it professional.

7. **Revenue & Sales Lead owns this.** Output goes to `.company/sales/competitive/` — the Sales Lead's write scope. Do not write to marketing or product directories unless asked.
