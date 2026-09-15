# Combat defense v2 work log

Baseline: zayx1228/agent-game main 176c961.

Implementation: preserve initial-state ballistics; add bounded combat scoring,
clear feasibility and latched survival mode; evaluate repair and operator alternatives;
stage walls, persist a daytime L1 gate and repair/rebuild jobs; revise upgrade priorities.
No task/LLM/news/mining algorithm rewrite. Update deploy archive after validation.

Baseline checks: 183 passed, 1 failed (test_night_moves_to_inner_side_when_adjacent_outside).
That test assumes every detour step strictly approaches the base; inspect against
actual reachability rather than delete it. This result predates all implementation.

Rule boundaries: one controller per weapon per round; construction daytime only;
rocket cooldown three empty rounds; start-of-round ballistic HP. Robot routing and
same-round incoming attack order remain conservative benchmark assumptions.

## Resumed implementation, 2026-09-15

Resumed the 12:43 combat implementation at commit `53754e5`; did not restore
the older `d432fb9` deployment archive over these sources. The initial resumed
test run had 226 passing tests and one failing full-ring scenario.

Fixed:
- Idle pioneers yield construction cells, including intermediate wall cells;
  workers can yield temporary blocking positions during daylight.
- Gate return budgets ignore transient friendly occupancy, avoiding premature
  all-day recall/oscillation. Closed gates only open when a trip is needed.
- Rocket history is populated on the next consecutive observed turn, requiring
  successful action feedback and matching HP loss. Other weapon overlap,
  possible enemy fire, missing units, failed actions and unexplained HP loss
  are excluded. Predictions never masquerade as confirmed hits.
- Dense high-HP chip damage alone no longer authorizes a second rocket.
- Another worker can inherit a rebuild when its owner dies or loses stone;
  stale or unreachable upgrade deliveries no longer postpone rebuilding.
- The full-ring test now uses connected internal gun positions, and covers
  both sides. Its former rail position isolated an interior pocket, so forcing
  20 walls would violate the required connectivity invariant. Existing upgraded
  guns remain intact; an incompatible layout can retain a real gap.

## Validation

`python -m pytest Demo/CoreGeek/tests -q`: 235 passed after the mirrored
construction regression was added (final rerun recorded before delivery).
The six standalone scripts `test_engine.py`, `test_validator.py`,
`test_news.py`, `test_orchestrator.py`, `test_llmproxy.py`, and
`test_judge_loop.py` all passed with Python UTF-8 mode.

Whole-repository pytest collection is not portable to this Windows host:
`test_competitor.py` reads a UTF-8 match file with the default GBK decoder,
while `test_sandbox.py` and `test_taskagent.py` depend on Linux shell/sandbox
commands. These failures were not hidden or counted as passing.

## Fixed-wave benchmark

Reports: `docs/combat-defense-v2/{baseline,revised}-{left,right}.json`.
Baseline is a detached Git worktree of `176c961`, using the same revised
benchmark driver and exact initial map/resources as the new strategy. Each
side runs Day1–7 with three repair packs and again with zero packs. Upgrade
levels are capability fixtures, not achieved economic outcomes. Both sides
have 14/14 base survivals for each strategy; no survival regression. Revised
maximum decision times: left 547.46 ms, right 598.19 ms, below the 5 s limit.

Three-pack results (zero-pack runs have the same kills in these fixtures):

| Day | Left kills old/new | Right kills old/new | Left clear round old/new | Right clear round old/new |
|---|---|---|---|---|
| 1 | 34/34 | 34/34 | 87/91 | 87/91 |
| 2 | 45/45 | 45/45 | 91/95 | 91/95 |
| 3 | 58/58 | 58/58 | 113/119 | 112/122 |
| 4 | 69/69 | 69/69 | 115/121 | 115/122 |
| 5 | 75/75 | 75/74 | 119/127 | 119/— |
| 6 | 86/85 | 86/84 | 128/— | 128/— |
| 7 | 83/88 | 82/86 | —/— | —/— |

This is not an across-the-board efficiency improvement: the new kill-first,
single-rocket policy clears some earlier waves later and wastes more damage.
It improves Day7 kill counts while all bases survive. New Day1 bases lose
10 HP in both layouts versus zero in the old policy. No seven-day clear or
official win-rate claim is made. Robot routing, same-round attack settlement,
and the nine-per-column advanced-robots-first formation need official replay
validation. Observed robot cooldown in this local fixture is three rounds;
production risk estimates conservatively assume every-round incoming damage.

The benchmark records kills, remaining robots, clear round, each weapon's
shots, effective damage, overkill, wall damage/destruction, station HP/damage,
repair consumption, mode-switch round and decision latency. It asserts
controller legality, cooldown/range, no nighttime construction, and survival
against the matching baseline via `--compare`.

Deployment: `python Demo/CoreGeek/tests/build_deploy.py` rebuilds
`Demo/CoreGeek.tar.gz` from tracked files, excludes cache/macOS metadata,
and checks every extracted file byte-for-byte against local source.
