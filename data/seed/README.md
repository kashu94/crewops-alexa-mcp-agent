# Seed operating week

A synthetic airline operation used for development, tests and the demo. All times are UTC
and all money is in Indian rupees.

| | |
|---|---|
| Week | 2026-09-14 to 2026-09-20 |
| Snapshot ("now") | 2026-09-14 18:00 UTC |
| Hub | BLR, with a smaller DEL base |
| Stations | BLR, BOM, CCU, COK, DEL, GOI, HYD, MAA |
| Fleet | 6 aircraft: 4 A320, 2 ATR72 |
| Crew | 150 (28 captains, 29 first officers, 93 cabin crew) |
| Flights | 147, grouped into 39 pairings |
| Reserves | 16 crew on call, each with a daily window |
| Passengers | 16,544 booked across 20,034 seats (70 to 96% full per flight) |
| Contacts | 21: two per station, five for the network |

| File | Contents |
|---|---|
| `crew.json` | Rank, base, aircraft ratings, status, how fast they can be reached |
| `flights.json` | Every leg with times, aircraft, seats and passengers booked |
| `rosters.json` | Pairings (duty days, legs, crew by role) and flagged exceptions |
| `reserve_pool.json` | On-call dates and windows |
| `certifications.json` | Licence, medical, recurrent training, dangerous goods |
| `duty_clocks.json` | 28 days of duty and flight hours per crew member |
| `risk_signals.json` | Provided disruption-risk scores (shown, never used to rank) |
| `rules.json` | The seven legality rules and their parameters |
| `costs.json` | Callout, positioning, delay and cancellation costs |
| `boarding_gates.json` | Gate held for each departure, from boarding (or the aircraft's arrival, when it parks overnight) until it leaves |
| `contacts.json` | The person on duty for each operational team: gate control and the duty manager at every station, and flight information, operations monitoring, maintenance control, training and crew records, and customer care for the network |

Check it loads and hangs together:

```bash
uv run python -c "from crewops.ingestion.seed import load_seed, validate; \
[print(i) for i in validate(load_seed())]"
```

The one expected warning is a licence whose `valid_from` is after its `valid_to`. The
certification rule checks expiry only, so this does not affect legality.
