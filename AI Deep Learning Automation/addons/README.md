# AddOns — the strategy ↔ dashboard/ML bridge

Two custom NinjaTrader 8 **AddOns** (compiled alongside the strategies, but installed into `bin\Custom\AddOns\`). They're how the live strategies hand data to the Python side — not part of the strategies' own trading logic, but part of the running system.

| File | Role |
|------|------|
| **[`ChartDataExporter.cs`](ChartDataExporter.cs)** | Serves recent bar data on request via a file-based protocol — the "recent-bars bridge" the model service and the trade-chart dashboard both read, so a chart shows exactly the prices the strategy saw. |
| **[`DashboardTradeLogger.cs`](DashboardTradeLogger.cs)** | Emits per-strategy trade records the live dashboard auto-discovers, so a newly-added strategy shows up on the dashboard without any config change. |

**Install:** copy both into `Documents\NinjaTrader 8\bin\Custom\AddOns\` and compile with the strategies (see [../SETUP.md](../SETUP.md)).
