# NivaWatt research and evidence

[Repository overview](../README.md) · [Project plan and architecture](PROJECT_PLAN.md)

NivaWatt asks three questions. Can historical load help identify Karnataka's peak hours? Can an apartment shift flexible loads while preserving water and EV service? Under a specified tariff, when does a lower connection peak reduce charges?

## Literature that informed the design

| Study | What it contributed to NivaWatt |
| --- | --- |
| Inoue and Gupta, *Weak Control for Human-in-the-loop Systems* (2019) | Informed the choice to offer a schedule that people can accept or decline. NivaWatt adds its own workflow for recording manual actions and checking them against readings. [Paper](https://arxiv.org/abs/1809.02770) |
| Lefebure et al., *Distributed Model Predictive Control of Buildings and Energy Hubs* (2022) | Demonstrates how demand and storage constraints matter for coordination, including a full-scale energy-hub experiment. Our four-building demo uses separate building plans and a shared clock. [Paper](https://arxiv.org/abs/2110.01734) |
| Kruger, Mueen and de Souza, *Peak Prediction in Time Series: Comparing Approaches for Energy High-Load Prediction* (2024) | Informed scoring the time and magnitude of peaks separately from average forecast error. Their load-prediction task differs from our Karnataka state-grid dataset and forecast horizon. [Paper](https://doi.org/10.1109/IJCNN60899.2024.10651140) |
| Li et al., *Optimal BESS Management for Peak Load Shaving and Battery Health Under Prediction Uncertainty* (2026 issue, online 2025) | Motivates testing peak outcomes under uncertainty and reporting equipment trade-offs. Our flexible resources are water storage and EV scheduling. [Paper](https://publikationen.bibliothek.kit.edu/1000183390) |

## Karnataka demand forecasting

The app uses a historical archive of hourly Karnataka state-grid load in MW, interpreted in Indian Standard Time. Of the archive's 137,041 expected hours, 6,611 are missing, and 277 duplicate timestamps have conflicting values. The pipeline keeps the next day's hour-zero row when it duplicates the preceding day's hour 24, and excludes incomplete target days. The recorded backtest uses load lags of at least 48 hours. Actual publication lag remains unvalidated. [KPTCL-derived archive and terms](https://github.com/Vonter/kptcl-load-curve) · [Backtest artifact](../results/forecast_evaluation.json)

**Released 14-day backtest.** Seasonal naive uses the same hour one week earlier. Ridge uses 48-, 168- and 336-hour load lags plus hour-of-day and day-of-week indicators. On this split, seasonal naive scored **580.9 MW** mean absolute hourly error and **90.5%** top-three overlap. Ridge scored **540.0 MW** and **92.9%**, respectively. A later diagnostic using daily refits and a different training procedure should be read separately from these release figures.

**Earlier frozen-candidate test.** On 180 chronological days held out before this evaluation, daily-refit Ridge scored **512.4 MW MAE**, **70.0% exact peak hour** and **77.0% top-three overlap**. Frozen histogram gradient boosting (HGB) scored **432.3 MW**, **68.9%** and **77.8%**, respectively. In a downstream simulation with five apartments per day, the Ridge and HGB advisories produced 1.50 and 1.47 kWh in the actual peak hours per apartment-run, on average. Both produced about 2.35 kW in the two-hour rebound window versus 1.47 kW in normal operation. The lower forecast MAE made little difference to that scheduling outcome.

**Expanded feature research.** The following runs used six chronological development folds covering 336 days, with a two-day gap. These folds had already been used for development. The results describe exploratory comparisons. The daily-refit Ridge reference and the normalized, expanding-training Ridge variants below use different fitting procedures.

| Model and inputs | Raw features | Hourly MAE (MW) ↓ | Exact peak hour ↑ | Top-three overlap ↑ | Centred three-hour coverage ↑ |
| --- | ---: | ---: | ---: | ---: | ---: |
| Daily-refit Ridge reference | — | 644.2 | 70.5% | 79.9% | 90.2% |
| HGB base | 17 | 524.6 | 72.9% | 80.6% | 90.5% |
| HGB + holidays | 25 | 517.0 | 73.2% | **81.5%** | 90.2% |
| HGB + weather | 24 | 509.3 | 73.5% | 80.8% | 89.6% |
| HGB + weather and holidays | 32 | 498.7 | 72.6% | 79.7% | 89.9% |
| HGB expanded | 41 | **492.8** | 72.9% | 81.2% | **91.1%** |
| Expanded-training Ridge base | 17 | 558.5 | 75.6% | 79.7% | 89.9% |
| Ridge + holidays | 25 | 552.9 | 75.6% | 79.8% | 89.9% |
| Ridge + weather | 24 | 547.1 | 75.6% | 80.0% | 89.9% |
| Ridge + weather and holidays | 32 | 539.6 | 75.6% | 80.0% | 89.9% |
| Ridge expanded | 41 | 540.2 | **76.2%** | 79.9% | 89.9% |
| Ridge expanded + interactions | 209 | 513.6 | **76.2%** | 80.4% | 89.9% |

Exact peak hour is the share of days with the highest hour predicted correctly. Top-three overlap averages the fraction of the three actual highest hours recovered. Centred three-hour coverage counts days when the actual peak is within one hour of the predicted peak.

- The 17-feature base uses 48/72/168/336-hour load lags, a seven-day hourly profile, D−2 daily mean, minimum, maximum and standard deviation, D−2 peak hour, recent median peak hour and distance from it, week-over-week trend, hour, weekday, month and season. Load features use a past-only level to normalize across long-term demand growth.
- Eight holiday inputs encode notification, weekday/Sunday/second-Saturday overlaps, second Saturday, neighboring days and whether the calendar was known. Seven weather inputs cover temperature, humidity, cloud cover and precipitation, plus daily temperature maximum and mean and precipitation total.
- The expanded models add temperature minimum, variability, relative shape, change across adjacent forecast hours, cyclic hour and annual phase. Ridge also tested explicit temperature–hour and weekday–hour interactions. Raw counts precede one-hot expansion. Feature names were checked for uniqueness. Complete redundancy pruning was outside this comparison.
- HGB delivered the lowest hourly MAE, while expanded Ridge found the exact peak hour more often on these folds. Earlier Ridge/HGB blends gave no stable improvement, and a Sunday/second-Saturday closure proxy changed overall MAE little. The expanded variants remain research results.
- The archived JMA weather uses Bengaluru as a proxy for statewide demand. Pre-2018 weather is missing in these inputs. Archived wind was unavailable. The weather issue-time and retrospective holiday availability assumptions need further checks. An ERCOT study informed comparison methods. Karnataka model inputs and reported scores are from the Karnataka experiment. [Open-Meteo archived forecast documentation](https://open-meteo.com/en/docs/previous-runs-api)

## Apartment scheduling and billing

### How the experiment evolved

1. **Circuit-energy study.** The original LT-1 case showed ₹0 from moving the same energy between hours at a flat rate. The team corrected an artificial tank overflow and carried pump state and minimum on/off history across midnight. The raw difference from pumping overflow was excluded from savings. [Circuit study artifact](../results/billing_scenario/billing-scenario-v2/summary.json)
2. **100-flat reference.** The team modelled a fictional Bengaluru building with 100 flats, 400 residents, a 30,000 L tank, about 54,000 L/day of withdrawals, a 5.5 kW pump, and a 7.4 kW charging circuit with a 15 kWh overnight request. The normal baseline uses float-switch pumping and charging on arrival. The comparison uses matched exogenous inputs and more detailed tank physics, including a high-level stop. This is an illustrative building profile.
3. **Demand-aware scheduling.** The initial perfect-mean profile was kept as a benchmark. The causal candidate uses only prior whole-connection history and readings from closed intervals. Five matched seeds used 30 history days and 30 scored days. The 70% case draws participation once per device and day. Nonparticipants use normal operation. Replanning does not redraw the participation decision.

Earlier stress tests treated ignored switching instructions as persistent. An ignored EV start could leave a session short, and a pump switch left on could restart repeatedly around the float cutoff. Those runs helped identify the service and wear risks. The matched schedule-participation study used normal operation for nonparticipants, and a bill comparison is reported only when the modeled services match.

### Causal study results

The evaluated cases below matched the normal baseline for water service and EV energy across all five seeds. The rupee values are reported as avoided charges. Ranges describe outcomes in the selected seeds.

| Policy | Estimated saving per 30 days | Mean | Pump starts per month | Manual switch operations per month |
| --- | ---: | ---: | ---: | ---: |
| Causal demand-aware, 100% participation | ₹3,480–₹6,960 | ₹5,220 | 104–111 | 364–400 |
| Causal demand-aware, 70% participation | ₹1,160–₹3,480 | ₹2,668 | 87–99 | 222–280 |
| Perfect-mean benchmark, 100% participation | ₹6,380–₹8,120 | ₹7,192 | — | — |
| Advisory-only scheduler, 100% participation | ₹0–₹1,740 | ₹696 | — | — |
| Normal operation | Reference | — | 60 | Automatic baseline in the model |

The earlier **₹6,380** result came from one tight-contract, perfect-mean benchmark. The estimates depend on the tight 168 kVA HT-4 contract, synthetic background demand, power factor, matching service and the tariff rules. A separate load spike can set the month's maximum for both policies. In the stale-reading sensitivity, fallback coincided with a higher maximum and reduced the avoided charge.

**Workload study.** A follow-up compared longer pump/EV blocks, demand-triggered revisions and a simple controller on three held seeds. At 100% participation, the longer-block candidate increased average modeled peak reduction from 8.19 to 9.81 kVA, but starts rose from 100.3 to 115.0. Switches fell from 348.7 to 290.0 and plan adoptions rose from 54.7 to 83.0. At 70%, peak reduction fell from 4.80 to 3.64 kVA relative to the original causal policy. The registered goals included at least 25% fewer starts and switches, which the candidate missed. This study is separate from the public v4 scheduler.

### How the bill estimate works

The replay forms 30-minute averages from two aligned 15-minute intervals, then finds the monthly maximum for each connection. In the cited HT-4 schedule for the replay period, ordinary demand charging uses a floor at 90% of contract demand and a cap at the contract demand for the ordinary rate. Above the contract demand, the excess is charged once at twice the demand rate. Counting how many intervals crossed the limit is useful diagnostically. The bill calculation uses the monthly maximum. [KERC tariff order, Annexure 9](https://kerc.karnataka.gov.in/uploads/96731743148968.pdf) · [Tariff summary](https://kerc.karnataka.gov.in/uploads/85821743074692.pdf) · [Published Conditions of Supply, clause 2.38](https://www.bescom.co.in/bescom/assets/pdfs/ConditionsofSupplyofElectricitywithIandIIAmendments.pdf) · [Rules used by the demo](../billing/tariffs.json)

The modeled LT-1 and HT-4 energy rates are flat across the day. Billed-demand and excess-demand effects depend on the tariff and contract of the actual connection. The cited supply conditions allow a shorter averaging interval for approved classes. For a real estimate, confirm the effective rules, the meter's interval and billing quantity, the site's tariff and contract, fuel adjustment (FPPCA), taxes and any applicable surcharges.

### Four-building replay in v4

The site chart sums each building's kW at the same half-hour timestamp before taking the maximum. The resulting site peak is **690.90 → 650.40 kW**, a reduction of **40.50 kW**. Individual monthly maxima use kVA, and the site rupee total sums the four individually billed estimates. All four comparisons pass the model's equal-service check. [Replay data](../web/campus-billing-data.json) · [Campus screenshot](screenshots/monthly-campus.png)

| Building | Normal → scheduled maximum demand (kVA) | Excess half-hours avoided | New excess half-hours | Estimated monthly saving |
| --- | ---: | ---: | ---: | ---: |
| APT_1 | 185.02 → 176.29 | 50 | 0 | ₹5,219.99 |
| APT_2 | 184.81 → 175.49 | 64 | 2 | ₹5,799.99 |
| APT_3 | 186.83 → 178.79 | 60 | 0 | ₹4,640.00 |
| APT_4 | 185.96 → 179.82 | 54 | 0 | ₹3,480.01 |
| Four bills combined | — | 228 | 2 | **₹19,139.99** |

An avoided window here means normal demand exceeded the threshold and scheduled demand fell to or below it at the same timestamp. A new window is the reverse. Utility savings for two, three or four buildings have yet to be estimated. That requires the actual shared feeder trace, timing of the feeder peak and rebound, marginal procurement or network costs, and program costs. Apartment bill estimates address a different question.

## Testing and reproducibility

| Evidence | What was verified |
| --- | --- |
| Released v4 snapshot | 89 Python checks passed, two raw-archive checks skipped. UI unit checks passed on the staged public snapshot. |
| Campus browser verification | The implementation was exercised through four separate manager sign-ins, a shared clock, accepted plans, verification and revisions, demand comparison, fast-forward, and month-end estimates. [Browser workflow](../tests/ui/campus_flow.cjs) |
| Meter evidence | Raw energy counters and model values are labelled separately where available. Electrical and water scale factors are separate. A missing channel stays unknown. Group-level charging evidence cannot confirm individual EV energy. |
| State and plan evidence | Revisions require acceptance, stale responses are ignored, and an already-sent action can complete at the service after sign-out. The scene displays observed equipment state. |
| Physical validation | Telemetry tests used synthetic readings and an emulator. A calibrated physical bench and a field trial remain part of the [project plan](PROJECT_PLAN.md#validation-plan). |

Use a fresh checkout and follow the [local setup](../README.md#run-locally). The historical archive is downloaded separately using the pinned source and verified hash. The deployed-model backtest, causal study and campus replay can be run from the repository root.

```sh
python scripts/fetch_kptcl.py
python -m forecasting.evaluate
python -m billing.causal
python -m billing.campus_replay
python -m pytest tests/core -q
node --test tests/ui/ui.test.js
```

The billing commands regenerate local study results and the bundled campus replay. The expanded Ridge and HGB experiments and workload study are documented here as historical evaluations. The public application reproduces the released backtest and the synthetic billing path with its included code.
