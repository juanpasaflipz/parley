---
description: Start a named research experiment bound to a desk_config. Tags subsequent cycles as part of the experiment so results can be aggregated and compared. Use to A/B test configurations or measure a hypothesis.
argument-hint: "<name> [--config=<desk_config_name>] [--hypothesis=\"...\"]"
allowed-tools: Read, Bash, Write
---

# /new-experiment

An experiment is a named, time-bounded group of cycles sharing the same
configuration. This is how Parley answers research questions — not by
looking at a single cycle, but by comparing experiments.

Arguments: `$ARGUMENTS`

Parse as: `<name> [--config=<n>] [--hypothesis="..."]`

- `name` (required) — short slug, unique. E.g. `exp-2026-04-opus-pm`.
- `--config=<n>` — `desk_configs.name`. Must exist. Defaults to the
  currently active config.
- `--hypothesis="..."` — one-sentence statement of what you're testing.
  If omitted, prompt the operator.

## Procedure

1. Resolve the config by name. Fail if not found.

2. Require a hypothesis. If not provided as a flag, ask the operator:

   > What are you trying to learn from this experiment? State it as a
   > falsifiable prediction — e.g. "Opus-PM reduces churn by >20% vs
   > Sonnet-PM over 30 days" or "Tighter correlation cap improves
   > Sharpe in high-vol regimes."

   Do not accept vague hypotheses ("see what happens"). The
   hypothesis is what gives the experiment scientific value later.

3. If the target config is not currently active, prompt the operator
   to activate it before starting the experiment. An experiment whose
   config isn't active won't accumulate cycles.

4. Insert via `bash: python -m desk.cycle new-experiment --name $NAME
   --config $CONFIG_NAME --hypothesis "$HYPOTHESIS"` — inserts a row
   in `experiments` and begins tagging cycles via `experiment_cycles`
   automatically as long as this experiment is active and its config
   matches.

5. Confirm to the operator:

   ```
   Experiment started: <name> (id: <uuid>)
     Config:      <config_name>
     Hypothesis:  "<hypothesis>"
     Started at:  <utc>

   Subsequent cycles under this config will be tagged automatically.
   End the experiment when you have enough data: /end-experiment <name>
   ```

## Rules

- **Only one experiment active per config at a time.** If there's
  already an active experiment on this config, ask the operator
  whether to end the prior experiment before starting a new one.
- **Experiments are append-only.** You cannot retroactively include or
  exclude cycles — this is what makes the comparison statistically
  honest.
- **Hypothesis is mandatory.** No vague experiments, ever. "Learning by
  observation" is fine as an operator activity; it's not an experiment.
- **Recommend minimum duration.** For crypto, a meaningful cycle count
  is usually 50–100+ cycles. Surface this if the operator seems to be
  starting an experiment they'll end in a day.
