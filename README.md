# OutageCover

Parametric downtime cover for trading venues, settled on GenLayer from the venue's own public status page.

## The trust problem

Exchanges go down, halt a market, or put pairs into cancel-only mode, and traders eat the loss. Downtime insurance exists in principle, but settling it needs someone to answer a question code alone cannot: *did this incident actually impair the thing I was covered for?*

A status feed is full of incidents that look alike and are not: "Delays with Ethereum deposits", "EGLD Funding and Trading Paused", "Krak Card degraded performance". Whether one of them triggers a policy on "BTC spot order placement" is a judgment call. Today that call is made by the insurer, the party that pays if the answer is yes.

OutageCover moves that judgment to GenLayer validators. Nobody has to trust the underwriter's backend or a single oracle.

## How it works

1. **Underwriter opens a pool.** It names the venue, the Statuspage feed (`https://<status-host>/api/v2/incidents.json`), a covered scope in plain language, a cover window, a trigger in minutes, and a payout multiple. The underwriter locks the full payout capital with the call, so every pool is fully collateralized.
2. **Traders buy cover** before the window starts. Each buyer's payout is premium × multiple, capped by the remaining capital.
3. **Anyone resolves** after the window ends:
   - **Deterministic step:** the leader fetches the feed. Code picks out the incidents that overlap the window and clips their start and end times to it. Open incidents are clipped at the window end. If the feed has rolled past the window, resolution fails with an `[EXTERNAL]` error instead of paying out on missing data.
   - **Judgment step:** an LLM decides for each candidate incident whether it impaired the covered scope. Incident text is passed in delimited `<incident>` blocks and treated as data.
   - **Deterministic step:** code merges overlapping intervals of the qualifying incidents into total impaired minutes.
4. **Validators verify independently.** Each validator re-runs the full fetch and classification. It accepts the leader's result only if:
   - it picked exactly the same set of qualifying incidents,
   - it reached the same trigger decision,
   - its impaired minutes are within max(5 min, 10%), because an incident can get resolved between two fetches.

   Errors are classified (`[EXPECTED]`, `[EXTERNAL]`, `[TRANSIENT]`, `[LLM_ERROR]`), so a bad LLM output forces a retry rather than a false agreement.
5. **Payout.**
   - If triggered, buyers `claim` their payout, and the underwriter withdraws capital + premiums − liability.
   - Otherwise the underwriter withdraws capital + premiums.

The LLM only makes the call it is needed for. Time arithmetic, window clipping and payout math stay deterministic.

## Live run on real data (Studionet, 2026-09-27)

Contract: `0x5aA5a3BC78f1753Be70c43308600bcE5474E53F3`

On the same Kraken feed, two pools covered the same 3-minute window with different scopes. At that time Kraken had seven unresolved incidents.

| Pool | Scope | Result | Evidence picked by validators |
|---|---|---|---|
| 0 | EGLD (MultiversX) spot trading on Kraken | **triggered**, 3 min | "MultiversX (EGLD) Funding and Trading Paused". Reason: EGLD pairs in cancel-only mode |
| 1 | BTC spot order placement or matching on Kraken | **not_triggered** | none. The ETH deposit delays, Krak Card issues and other asset halts were correctly ignored |

- Resolve txs: `0x989c569f…16d1` (pool 0), `0x8fdf9e73…1f32` (pool 1). Full output is in `demo_result.json`.
- To reproduce: `node scripts/live_demo.mjs <contract address>`.

## Contract API

| Method | Kind | Description |
|---|---|---|
| `open_pool(venue, feed_url, scope, cover_start, cover_end, trigger_minutes, multiple)` | payable write | Opens a pool. `msg.value` is the payout capital. Times are unix seconds. `multiple` is 2 to 100. |
| `buy_cover(pool_id)` | payable write | `msg.value` is the premium. Returns the payout amount. |
| `resolve(pool_id)` | write | Anyone can call it after `cover_end`. Returns `triggered` or `not_triggered`. |
| `claim(pool_id)` | write | Pays the caller's cover if the pool triggered. |
| `withdraw_capital(pool_id)` | write | For the underwriter, after resolution. |
| `get_pool(pool_id)` / `get_cover(pool_id, buyer)` / `get_pool_count()` | view | |

Works with any venue on Atlassian Statuspage, for example Kraken, Coinbase, Gemini, Bitstamp and dYdX. The same pattern covers non-trading services too, such as API uptime SLAs.

## Build and test

```bash
python -m venv .venv && .venv/Scripts/pip install genlayer-test   # Python 3.12+
pytest tests -v
```

The direct-mode tests cover:
- input validation
- capacity and buy-window rules
- triggered and not-triggered settlement, including claim and withdraw
- overlap merging and clipping of still-open incidents
- feed roll-over protection
- validators rejecting a leader that drops a qualifying incident or classifies differently

Deploy with the GenLayer CLI:

```bash
npm i -g genlayer
genlayer network set studionet
genlayer deploy --contract contracts/outage_cover.py
```

## Limits

- **Feed length.** Statuspage returns only the latest 50 incidents. Pools must be resolved while the feed still covers the window; otherwise resolution fails safely.
- **Self-reporting.** The venue writes its own status page. An outage it never posts cannot trigger a payout. The contract settles on the venue's own admissions, which is the least disputable source, but it is not a monitoring network.
- **Time precision.** Impaired time comes from the incident's `started_at` and `resolved_at`, so it is only as precise as the venue's reporting.
