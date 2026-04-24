---
description: Create a new desk_config row — a named, versioned snapshot of agent models, prompts, and parameters. Use at the start of a new experiment or when changing how the desk reasons.
argument-hint: "<name> [--base=<existing_config_name>]"
allowed-tools: Read, Bash, Write, Grep
---

# /new-config

A `desk_config` is a snapshot of how the desk is configured: which
models each agent uses, hashes of the agent prompt files, and any
tunable parameters. Every cycle is tied to a config, so you can compare
experiments cleanly.

Arguments: `$ARGUMENTS`

Parse as: `<name> [--base=<existing>]` where:

- `name` (required) — short slug, unique. E.g. `baseline-v1`,
  `opus-pm-sonnet-quant`, `aggressive-risk`.
- `--base=<n>` — copy this existing config and mutate. If omitted,
  build from sensible defaults.

## Procedure

1. Verify the name is unique: `bash: psql $DATABASE_URL -tAc "SELECT
   COUNT(*) FROM desk_configs WHERE name='$NAME';"` — must be 0.

2. Build the config JSON. Include:
   ```json
   {
     "research":  { "model": "claude-opus-4-7",    "prompt_hash": "<sha256 of .claude/agents/research-analyst.md>" },
     "quant":     { "model": "claude-sonnet-4-6",  "prompt_hash": "<sha256>" },
     "pm":        { "model": "claude-opus-4-7",    "prompt_hash": "<sha256>" },
     "risk":      { "model": "claude-opus-4-7",    "prompt_hash": "<sha256>",
                    "soft_rules": ["high_vol_pullback","drawdown_defense","correlation_cap","regime_mismatch","recent_event_cooldown","cash_reserve_breach"] },
     "execution": { "model": "claude-sonnet-4-6",  "prompt_hash": "<sha256>" },
     "risk_limits_snapshot": { ... current contents of risk_limits table ... },
     "universe": ["BTCUSDT","ETHUSDT","SOLUSDT"],
     "timeframes": { "signals": "1h", "storage": "1m" },
     "notes": "<operator-provided explanation or auto-generated 'default baseline'>"
   }
   ```

3. If `--base` was provided, fetch that config and apply the operator's
   requested mutations. Surface a diff so the operator can confirm.

4. Prompt the operator to confirm the final config before writing.
   Show the JSON in a readable form.

5. On confirmation, `bash: python -m desk.cycle new-config --name
   $NAME --json-path /tmp/parley-new-config-$UUID.json` which inserts
   the row and, if requested, sets it active (deactivating the prior
   active config).

6. Output the new `config_id` and ask whether to start an experiment
   bound to it.

## Rules

- **Configs are immutable once created.** If you want to change
  something, create a new config. This preserves cycle attribution.
- **Only one config can be active at a time.** Setting one active
  deactivates others — the supervisor confirms this before writing.
- **Prompt hashes are computed fresh** each time a config is created.
  If you later edit a subagent prompt, that config is out of sync — you
  should create a new config rather than silently drift.
- **Never modify `desk_configs` rows by hand.** Use this command.
