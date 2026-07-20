#region Using declarations
using System;
using System.Globalization;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Strategies {
    public class temalimit : ActiveStopVisualStrategyBase {
        private readonly Dictionary<int, SessionIterator> sessionIteratorsByBarsInProgress = new Dictionary<int, SessionIterator>();
        private static readonly object dashboardTradeLogLock = new object();
        // RunWithContext swaps per-symbol state (entryPrice, activeContext, etc.) through shared instance
        // fields with no synchronization. If NinjaTrader ever delivers OnExecutionUpdate/OnOrderUpdate for
        // two different symbol contexts close together in wall-clock time (observed across accounts on
        // near-simultaneous fills), one context's fields can get overwritten by another's mid-callback --
        // corrupting or silently dropping that trade's dashboard log row (entryPrice<=0 guard in
        // AppendDashboardTradeOutcome). Serializing the whole load/run/save cycle closes that window.
        private readonly object contextSwitchLock = new object();

        private bool dashboardTradeLogHeaderChecked;
        private readonly object mlHttpErrorPrintLock = new object();
        private DateTime lastMlHttpErrorPrintUtc = DateTime.MinValue;
        private const int MlHttpErrorPrintThrottleSeconds = 30;

        // === Durable delivery for the TERMINAL exit sample (label 0) ===
        // A trade emits ~2,000 redundant label-1 "hold" rows but exactly ONE label-0 row at
        // the close, and that single row carries all the exit evidence. Both used to go out
        // through FireAndForgetPostJson, which drops the payload on any failure -- so one
        // missed POST (service restarting, 2.5s timeout under load) silently costs that
        // trade's entire exit record. 613 trades July 1-17 lost theirs to the dead Flat-check,
        // and 5 more on July 19 AFTER that fix -- the delivery path is the remaining hole.
        // Hold rows deliberately keep using fire-and-forget: they are redundant by design and
        // spooling thousands of them would be pure waste.
        // Trades whose terminal label-0 exit sample has already been written.
        // NOT part of SymbolContext on purpose: trade ids are globally unique
        // (yyyyMMdd_HHmmss_SYMBOL), so one set is correct across every symbol
        // context and adding it to the context would only confuse the parity
        // checker. Cleared wholesale past a cap rather than pruned -- worst case
        // that permits one duplicate after thousands of trades (weeks), which is
        // far cheaper than the bookkeeping.
        private readonly HashSet<string> _exitSampleLoggedTradeIds = new HashSet<string>();
        private const int ExitSampleLoggedIdsMax = 2000;

        private static readonly object pendingExitSampleLock = new object();
        private const int MlExitSamplePostAttempts = 4;
        private const int MlExitSamplePostBackoffMs = 750;
        private const int MlExitSampleReplayThrottleSeconds = 60;
        private const int MlExitSampleSpoolMaxLines = 5000;
        private static DateTime lastExitSampleReplayUtc = DateTime.MinValue;
        private static bool exitSampleReplayRunning;

        // Alert credentials are read at runtime from UserDataDir\temalimit_alerts.config, which lives
        // OUTSIDE this git repo, so the strategy source can be shared without carrying them. They used
        // to be hard-coded here; the ntfy topic is effectively a password (anyone holding it can read
        // every trade alert and publish spoofed ones) and the account list is a real broker account
        // number, so neither belongs in version control. Same shared topic the maintenance watchdogs
        // push to (see ML_SYSTEM_GUIDE.txt, "PHONE ALERTING") -- one subscription to manage.
        //
        // Format, one key=value per line ('#' comments allowed):
        //     topic=<ntfy topic>
        //     accounts=<comma-separated account names>
        //
        // Missing file or blank topic disables phone alerts and says so at startup rather than failing
        // silently -- these alerts cover naked positions, so quietly-off is the dangerous outcome.
        private const string NtfyConfigFileName = "temalimit_alerts.config";
        private static readonly object ntfyConfigLock = new object();
        private static readonly HashSet<string> NtfyNotifiableAccountNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private static string ntfyTopicValue = string.Empty;
        private static bool ntfyConfigLoaded;

        private static string NtfyTopic {
            get {
                EnsureNtfyConfigLoaded();
                return ntfyTopicValue;
            }
        }

        private static bool NtfyConfigured {
            get { return !string.IsNullOrEmpty(NtfyTopic); }
        }

        private static void EnsureNtfyConfigLoaded() {
            if (ntfyConfigLoaded)
                return;

            lock (ntfyConfigLock) {
                if (ntfyConfigLoaded)
                    return;

                // Set first: a malformed config must not make every caller retry the parse.
                ntfyConfigLoaded = true;

                try {
                    string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, NtfyConfigFileName);
                    if (!File.Exists(path))
                        return;

                    foreach (string rawLine in File.ReadAllLines(path)) {
                        string line = rawLine.Trim();
                        if (line.Length == 0 || line.StartsWith("#"))
                            continue;

                        int separator = line.IndexOf('=');
                        if (separator <= 0)
                            continue;

                        string key = line.Substring(0, separator).Trim();
                        string value = line.Substring(separator + 1).Trim();

                        if (string.Equals(key, "topic", StringComparison.OrdinalIgnoreCase)) {
                            ntfyTopicValue = value;
                        }
                        else if (string.Equals(key, "accounts", StringComparison.OrdinalIgnoreCase)) {
                            foreach (string rawAccount in value.Split(',')) {
                                string account = rawAccount.Trim();
                                if (account.Length > 0)
                                    NtfyNotifiableAccountNames.Add(account);
                            }
                        }
                    }
                }
                catch {
                    // An unreadable config must never take the strategy down; alerts just stay off,
                    // which the startup notice reports.
                }
            }
        }
        private readonly HashSet<Order> _ntfyNotifiedPendingOrders = new HashSet<Order>();
        private readonly HashSet<string> _ntfyNotifiedExecutionIds = new HashSet<string>();
        private NinjaTrader.NinjaScript.Indicators.StochRSI stochRsi;
        private TEMA temaIndicator;
        private Bollinger bb;
        private MFI mfi;
        private RSI rsi;
        private MACD macd;
        private ATR atr;
        // Snapshot of pullback ATR metrics used for the active trade.
        private double _lastPullbackAtr;
        private double _lastPullbackAtrAvg;
        private double _lastPullbackAtrRatio = 1.0;
        // Unclamped atr[0]/average ratio from the same AtrBoundPullbackTicks call that produced
        // _lastPullbackAtrRatio -- logged into the no-fill TSV so the dashboard can tell when the
        // clamp band (AtrClampMin/Max) was the binding constraint on the pullback distance.
        private double _lastPullbackAtrRatioRaw = 1.0;
        private Series<double> sessionVwapSeries;
        private Order entryOrder;
        private DateTime entryOrderSubmittedTime = DateTime.MinValue;
        private double entryOrderSubmittedMarketPrice = 0.0;
        // Closest the market got to entryOrder's limit price while it was working (lowest tick price for a
        // LONG limit, highest for a SHORT limit; per-tick closes, not bar extremes -- see
        // UpdateEntryOrderClosestApproach). Updated per tick in OnBarUpdateCore.
        // Lets AppendNoFillLog report how many ticks short a cancelled order actually missed by, instead of
        // approximating from the placement price.
        private double entryOrderClosestApproachPrice = 0.0;

        private double entryPrice;
        private DateTime entryFillTime = DateTime.MinValue;
        private double oneRPoints;
        private double currentStopPrice;
        private double maxFavorableExcursionPoints;
        private double maxAdverseExcursionPoints;
        private int entryBar = int.MinValue;
        private double tradeHighSinceEntry = double.NaN;
        private double tradeLowSinceEntry = double.NaN;
        // ML Template Selection (restored 2026-07-17 after a bad merge wiped it from the source file --
        // see ML_SYSTEM_GUIDE.txt). _activeTemplateSetByMl mirrors _activeTemplateWasWinner's naming.
        private int _lastTemplateMlSelectionBar = int.MinValue;
        private string _templateMlStatus = "warming_up";
        private bool _activeTemplateSetByMl;
        // Cached once per context-bar so shadow trades and a live entry submitted on the same bar
        // share an identical setup_timestamp -- see GetCurrentBarSetupTimestamp(). Context-mirrored
        // (SymbolContext.CurrentBarSetupTimestamp*): as shared instance fields, another context's
        // interleaved bar update (different CurrentContextBar) invalidated the cache between ticks,
        // so a live entry submitted on a later tick of the same bar got a DIFFERENT timestamp than
        // that bar's shadow trades -- exactly the grouping mismatch this cache exists to prevent.
        private DateTime currentBarSetupTimestamp = DateTime.MinValue;
        private int currentBarSetupTimestampBar = int.MinValue;
        private DateTime pendingSetupTimestamp = DateTime.MinValue;
        private DateTime activeSetupTimestamp = DateTime.MinValue;
        private double lastSubmittedStopPrice = double.NaN;
        // Price of a stop submission still in flight (NaN when none). lastSubmittedStopPrice only
        // holds stops NT has actually Accepted, so a rejected one can't suppress its own retry.
        private double pendingStopSubmitPrice = double.NaN;
        private double cumulativeTypicalVolume;
        private double cumulativeVolume;
        private double sessionVwap;
        private string lastSubmittedStopSignal = string.Empty;
        private string activeEntrySignal = string.Empty;
        private bool startupEntrySignalsClear;
        private string pendingMlWindowJson = string.Empty;
        private string pendingMlTrigger = string.Empty;
        private string pendingMlPrediction = string.Empty;
        private double pendingMlConfidence;
        private string pendingMlSetupDirection = string.Empty;
        private string pendingMlSignal = string.Empty;
        private bool pendingMlReversal;
        private string _lastNoTradeSource = string.Empty;
        private DateTime _lastNoTradeLogTime = DateTime.MinValue;
        private string activeMlWindowJson = string.Empty;
        private string activeMlTrigger = string.Empty;
        private string activeMlPrediction = string.Empty;
        private double activeMlConfidence;
        private string activeMlSetupDirection = string.Empty;
        private string activeMlSignal = string.Empty;
        private bool activeMlReversal;
        private bool activeMlIsLong;
        private bool activeMlSampleLogged;
        private int mlBackfillSamplesSent;
        private int lastStopUpdateBar = -999999;
        private DateTime lastStopSubmitUtc = DateTime.MinValue;
        // Round-trip time for a stop ChangeOrder to reach the broker/sim and settle. Found 2026-07-17:
        // Calculate.OnEachTick + MinStopMoveTicks=1 let TryUpdateStopSafely fire 3 stop changes in 6.6s
        // (last two 42ms apart) for account Simtema/NQ -- the last one was computed against a bid
        // snapshot that was already stale by the time NT processed it, landing below the live market
        // and getting rejected ("Stop price can't be changed below the market"), which force-closed the
        // strategy. This wall-clock gate is independent of MinStopMoveTicks (a price-distance gate) and
        // of bar structure (irrelevant on OnEachTick) -- it directly targets the round-trip race.
        private const int MinStopSubmitIntervalMs = 300;
        private int lastExitBar = -999999;
        private int reentryBlockedUntilBar = int.MinValue;
        private bool stopInitialized;
        private bool takeProfitExitPending;
        private bool dailyLossLimitHit;
        private bool dailyLossExitPending;
        // Combined daily loss limit state.
        private bool combinedLossLimitHit;
        // Shared daily reset clock.
        private DateTime combinedBaselineDate = DateTime.MinValue;
        private bool protectiveStopWorking;
        // Set when a stop placement is rejected outright; EnsureProtectiveStopArmed() re-arms or
        // flattens on the next tick. Latched separately so a working flatten isn't re-submitted.
        private bool protectiveStopRearmPending;
        private bool protectiveStopFlattenPending;
        private int watchdogTradeDirection;
        private int watchdogTradeQuantity;
        private double watchdogEntryPrice;
        private string watchdogEntrySignal = string.Empty;
        private int watchdogMismatchBar = int.MinValue;
        private int watchdogLastManagedBar = int.MinValue;
        private bool hasDailyLossBaseline;
        // Instrument-specific realized PnL.
        private double dailyRealizedPnLDollars;
        private DateTime dailyBaselineDate = DateTime.MinValue;
        private SimpleFont labelFont;
        private int nearMissLogBar = -1;
        private int gateBlockLogBar = -1;
        // Post-cancel expire watch: after an entry limit expires and is cancelled, keep watching whether
        // the market later touches the old limit price. Logged to TemaLimit_expire_log.tsv as measured
        // evidence for whether a longer EntryOrderExpireMinutes would have turned this no-fill into a fill
        // (drives the Entry Gate Reassess card's expire suggestion). One watch slot per context; a new
        // expire-cancel resolves the previous watch as untouched before starting its own.
        private bool expireWatchIsLong;
        private double expireWatchLimitPrice;
        private DateTime expireWatchSubmittedTime = DateTime.MinValue;
        private int expireWatchTemplateNumber;
        private int expireWatchExpireMinutes;
        private bool flattenOnEnablePending;
        private string _exitTradeId = string.Empty;
        private int _exitBarsHeld;
        private double _exitEntryPrice;
        private double _exitOneRPoints;
        private string _exitDirection = string.Empty;
        // Per-trade feature history for ML exit decisions.
        private List<double[]> _exitFeatureHistory = new List<double[]>();
        private string _lastExitReason = "unknown";
        private int _lastMlExitSampleBar = int.MinValue;
        // Throttle ML exit sample requests to avoid bursts.
        private static readonly TimeSpan MlExitSampleMinInterval = TimeSpan.FromMilliseconds(200);
        private DateTime _lastMlExitSampleLogTime = DateTime.MinValue;
        private int _lastMlExitPredictionBar = int.MinValue;
        private int _lastMlExitControlBar = int.MinValue;
        private bool _mlExitSubmitted;
        private bool _mlExitPhaseCheckPending;
        private DateTime _mlExitPhaseCheckDate = DateTime.MinValue;
        // Server-reported ML exit phase.
        private int _mlExitRecommendedPhase;
        private bool _mlExitPhase3Unlocked;
        private bool _mlExitArmedPrinted;

        private static readonly object templateStateLock = new object();
        private static readonly object templateUsageLock = new object();
        private static readonly object templatePnlLock = new object();
        private int _activeTemplateNumber = 1;
        private bool _templateStateLoaded;
        // Mode 3: templates with a completed trade for this instrument, shared per-instrument on disk. Reloaded before each rotation decision.
        private HashSet<int> _usedTemplateNumbers = new HashSet<int>();
        // Modes 4/5: cumulative realized dollars per template for this instrument, shared per-instrument on disk.
        // Accumulated on every closed trade regardless of mode (so history exists before switching to 4/5); reloaded before each rotation decision.
        private Dictionary<int, double> _templatePnl = new Dictionary<int, double>();
        // Rotation counts only eligible trading time; excludes the 2-3 PM closure, cutoff windows, weekends, and data gaps.
        private TimeSpan _templateEligibleElapsed = TimeSpan.Zero;
        private DateTime _templateLastEligibleClock = DateTime.MinValue;

        private List<int> _customTemplateList;
        private string _customTemplateListSource;
        private bool _activeTemplateWasWinner;
        private bool _templateTimerInitialized;

        private static readonly TimeSpan RegularNormalNoFillWindow = TimeSpan.FromMinutes(10.125);
        private static readonly TimeSpan RegularWinnerNoFillWindow = TimeSpan.FromMinutes(20.25);
        private static readonly TimeSpan OvernightNormalNoFillWindow = TimeSpan.FromMinutes(23.25);
        private static readonly TimeSpan OvernightWinnerNoFillWindow = TimeSpan.FromMinutes(46.5);
        private int _lastShadowSessionMinTemplate = int.MinValue;
        private int _lastShadowSessionMaxTemplate = int.MinValue;
        private int _lastLiveSessionMinTemplate = int.MinValue;
        private int _lastLiveSessionMaxTemplate = int.MinValue;

        // Preserve active template across sessions.


        // One indicator set per selectivity value; templates sharing a selectivity share periods (10 sets for 40 templates).
        private const int FirstTemplateNumber = 1;
        private const int AbsoluteMaxTemplateNumber = 40;
        private const int TemplateSlotCount = AbsoluteMaxTemplateNumber + 1; // 1-based template slots; index 0 unused.
        private static readonly double[] BandSelectivities = { 0.00, 0.10, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00 };
        private TEMA[] bandTema;
        private Bollinger[] bandBb;
        private MFI[] bandMfi;
        private NinjaTrader.NinjaScript.Indicators.StochRSI[] bandStochRsi;

        private class ShadowTrade {
            public int Template;
            public bool IsLong;
            public double LimitPrice;
            public DateTime SubmittedTime;
            public bool Filled;
            public int FillBar;
            public double EntryPrice;
            public double StopPrice;
            public double TargetPrice;
            public string WindowJson;
            public string Trigger;
            // Instrument that opened this shadow trade. Logging at resolve time re-reads
            // CurrentInstrument, so a context-mirroring regression (July 9-10 cross-symbol
            // sample bleed) would attribute this trade's window/prices to the wrong symbol
            // -- the resolve path drops the sample if this doesn't match the active context.
            public string Symbol;
        }

        private ShadowTrade[] _shadowTrades;
        private int[] _shadowCooldownUntilBar;
        private int _lastShadowProcessedBar = int.MinValue;

        private void ConfigurePrintOutput() {
            // Output 2 is reserved for template-change notifications and compile confirmations; everything else prints to Output 1.
            PrintTo = PrintTo.OutputTab1;
        }
        protected override void OnStateChange() {
            try {
                OnStateChangeCore();
            }
            catch (Exception ex) {
                // Ignore transient data-loading exceptions during backfill.
                Print(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " | OnStateChange EXCEPTION " + Name + " (State=" + State + "): " + ex.Message);
            }
        }

        private void OnStateChangeCore() {
            if (State == State.SetDefaults) {
                OnStateSetDefaults();
            }
            else if (State == State.Configure) {
                OnStateConfigure();
            }
            else if (State == State.DataLoaded) {
                OnStateDataLoaded();
            }
            else if (State == State.Realtime) {
                OnStateRealtime();
            }
        }

        private void OnStateSetDefaults() {
            // Increase HTTP connection limit for ML request bursts.
            ServicePointManager.DefaultConnectionLimit = Math.Max(ServicePointManager.DefaultConnectionLimit, 64);
            ServicePointManager.Expect100Continue = false;

            Description = "TEMA/Bollinger limit entry with InterpolationV2-style split-risk stop ladder.";
            Name = "tema limit";
            PrintTo = PrintTo.OutputTab1;
            Calculate = Calculate.OnEachTick;
            EntriesPerDirection = 1;
            EntryHandling = EntryHandling.AllEntries;
            StopTargetHandling = StopTargetHandling.PerEntryExecution;
            StartBehavior = StartBehavior.ImmediatelySubmit;
            IsExitOnSessionCloseStrategy = false;
            IncludeCommission = true;
            IncludeTradeHistoryInBacktest = true;
            IsInstantiatedOnEachOptimizationIteration = false;
            BarsRequiredToTrade = 30;
            DefaultQuantity = 1;

            TemaLength = 7;
            BBLength = 20;
            BBStdDev = 1.6;
            EnableMfiFilter = true;
            MfiPeriod = 14;
            MfiPriorBars = 0;
            MfiLongMax = 50;
            MfiShortMin = 50;
            EnableRsiFilter = true;
            RsiLongMax = 50;
            RsiShortMin = 50;
            EnableTemaVwapMidBbCrossEntry = true;
            ShowStrategyVwap = false;
            ShowStrategyValues = false;
            EnableMlDirectionService = true;
            EnableMlTradeLogging = true;
            MlServiceUrl = "http://localhost:8765";
            MlMinConfidence = 0.60;
            MlWindowBars = 50;
            MlHttpTimeoutMs = 2500;
            NoTradeLogIntervalSeconds = 15;
            EnableMlHistoricalBackfill = false;
            EnableMlExitSampleLogging = true;
            MlExitServerUrl = "http://localhost:8765";
            EnableMlExitRecommendations = true;
            EnableMlExitControl = true;
            MlExitHoldThreshold = 0.45;
            MinBarsBeforeMlExit = 3;
            MinUnrealizedRForMlExit = 0.0;
            MlExitSignalCooldownBars = 2;
            MlBackfillHorizonBars = 10;
            MlBackfillMinMoveTicks = 8;
            MlBackfillMaxSamples = 500;
            RiskDollars1R = 4000;
            LadderRiskDollars1R = 1000;
            Contracts = 1;
            DailyLossLimit = 0;
            EnableMaxDayMargin = false;
            MaxDayMarginDollars = 0;
            DailyEntryRiskDollars = 900;
            DailyEntrySlippageDollars = 100;
            ReentryCooldownBars = 2;
            PullbackTicks = 0;
            EntryOrderExpireMinutes = 1;
            RequireFreshSignalAfterEnable = true;
            EnableStochRsiCrossFilter = true;
            StochRsiPeriod = 14;
            StochRsiLowerLine = 0.20;
            StochRsiUpperLine = 0.80;
            StochRsiCrossLookbackBars = 0;
            MinStopMoveTicks = 1;
            StopSafetyBufferTicks = 6;
            // NT's default (StopCancelClose) flattens AND disables the whole instance on any order
            // error -- including a rejected stop ChangeOrder where the old stop is still working,
            // which killed SimRenko twice on 2026-07-19 (once per side). We handle the two stop-loss
            // failure modes ourselves in OnOrderUpdateCore: change-reject -> resync to the still-
            // working old stop; placement-reject -> flatten immediately (position is unprotected).
            RealtimeErrorHandling = RealtimeErrorHandling.IgnoreAllErrors;

            EnableTimeWindow = true;
            SessionExitTime = 134300;
            ResumeTime = 140000;

            DebugMode = false;
            ShowEntryLabels = true;

            TemplateNumber = 1;
            TemplateMode = 1;
            MaxTemplateNumber = 40;
            PrintTemplateChanges = true;

            CustomTemplateRanges = "";
            UseCustomRotationTiming = false;
            CustomNoFillWindowMinutes = 34.5;
            CustomWinnerWindowMinutes = 69.0;
            EnableMlTemplateSelection = false;

            EnableSessionBasedTemplateRange = true;
            RegularMinTemplateNumber = 1;
            RegularMaxTemplateNumber = 20;
            OvernightMinTemplateNumber = 20;
            OvernightMaxTemplateNumber = 40;
            RegularSessionStartTimeLocal = 63000;  // 6:30 AM PT approx 9:30 AM ET
            RegularSessionEndTimeLocal = 131500;   // 1:15 PM PT approx 4:15 PM ET

            EnableShadowEvaluation = true;
            ShadowFillThroughTicks = 1;
            ShadowMaxHoldBars = 50;

            EnableMultiSymbolMode = true;
            EnableSymbol1 = true;
            EnableSymbol2 = true;
            EnableSymbol3 = true;
            EnableSymbol4 = true;
            Symbol2Name = "ES";
            Symbol3Name = "YM";
            Symbol4Name = "RTY";
            Symbol2BarsPeriodType = BarsPeriodType.Tick;
            Symbol2BarsPeriodValue = 500;
            Symbol3BarsPeriodType = BarsPeriodType.Tick;
            Symbol3BarsPeriodValue = 500;
            Symbol4BarsPeriodType = BarsPeriodType.Tick;
            Symbol4BarsPeriodValue = 500;
        }

        private void OnStateConfigure() {
            StartBehavior = StartBehavior.ImmediatelySubmit;

            // Stamped here (first state after Enable) so the Realtime line below can report how long
            // the historical load actually took. See PrintLifecycleState.
            enableStartTime = DateTime.Now;

            if (EnableMultiSymbolMode) {
                if (EnableSymbol2)
                    AddDataSeries(ResolveQuarterlyContractSymbol(Symbol2Name), Symbol2BarsPeriodType, Math.Max(1, Symbol2BarsPeriodValue));
                if (EnableSymbol3)
                    AddDataSeries(ResolveQuarterlyContractSymbol(Symbol3Name), Symbol3BarsPeriodType, Math.Max(1, Symbol3BarsPeriodValue));
                if (EnableSymbol4)
                    AddDataSeries(ResolveQuarterlyContractSymbol(Symbol4Name), Symbol4BarsPeriodType, Math.Max(1, Symbol4BarsPeriodValue));
            }
        }

        private void OnStateDataLoaded() {
            ConfigurePrintOutput();

            labelFont = new SimpleFont("Arial", 12);

            ExportTemplateReferenceIfNeeded();
            PrintCompileNotificationIfNeeded();

            if (EnableMultiSymbolMode) {
                BuildMultiSymbolContexts();
            }
            else {
                // Pre-build every selectivity band's indicators so mid-session rotation switches periods via ApplyTemplate().
                BuildBandIndicators();

                // Runs after BuildBandIndicators so ApplyTemplate can point active fields at the template's band set.
                InitializeTemplateRotation();

                rsi = RSI(14, 3);
                macd = MACD(5, 13, 3);
                atr = ATR(14);
                sessionVwapSeries = new Series<double>(this);

                temaIndicator.Plots[0].Brush = new SolidColorBrush(Color.FromArgb(0xFF, 0x99, 0x3A, 0x00));
                temaIndicator.Plots[0].Width = 1;

                if (ChartControl != null) {
                    AddChartIndicator(temaIndicator);
                    AddChartIndicator(bb);
                    AddChartIndicator(mfi);
                }
            }

            PrintLifecycleState("LOADING HISTORY - not trading yet");
        }

        // Enable -> Realtime is invisible in the Output window otherwise: the "Template N applied" line
        // prints at DataLoaded, so an instance that has not reached Realtime looks identical to a live
        // one. Prints regardless of DebugMode for the same reason LogDayMarginBlock does: it is the one
        // line that distinguishes "working on it" from "wedged", and once per state change is not spam.
        //
        // The elapsed figure is Configure -> here, i.e. the real historical-load time. Measured on
        // account <account> (Tradovate) it reads 0.0 min: the load is sub-second, NOT the 19-37 minutes
        // an earlier 2026-07-20 diagnosis claimed. That claim was wrong -- see the 12th-pass entry in
        // ML_SYSTEM_GUIDE.txt. What is still unexplained is the gap between Realtime and the FIRST
        // heartbeat write (~2 min on 2026-07-20 11:42, ~18 min on 09:04), which happens after this
        // point and is therefore not a load-time effect. Do not re-derive "slow history load" from
        // a stale heartbeat file; the two are separate.
        private void PrintLifecycleState(string status) {
            string elapsed = enableStartTime == DateTime.MinValue
                ? string.Empty
                : " after " + (DateTime.Now - enableStartTime).TotalMinutes.ToString("0.0", CultureInfo.InvariantCulture) + " min";

            Print(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture)
                + " | [" + ResolveAccountLabel() + "] " + Name + " " + status + elapsed);
        }

        private void OnStateRealtime() {
            ConfigurePrintOutput();
            realtimeStartTime = DateTime.Now;
            PrintLifecycleState("*** REALTIME - LIVE AND TRADING ***");
            if (EnableMultiSymbolMode) {
                foreach (SymbolContext ctx in symbolContexts) {
                    RunWithContext(ctx.BarsInProgressIndex, () => {
                        ResetDailyBudgetOnEnable();
                        ReseedRankedTemplateAtRealtime();
                    });
                    ctx.FlattenOnEnablePending = true;
                }
            }
            else {
                ResetDailyBudgetOnEnable();
                ReseedRankedTemplateAtRealtime();
                flattenOnEnablePending = true;
            }
        }

        // InitializeTemplateRotation runs at State.DataLoaded, where Account can still be unresolved -- the
        // ledger path then reads as "UNKNOWN", the file misses, the ranking comes back empty and modes 4/5
        // silently keep the seeded TemplateNumber (observed: NQ mode 5 starting on T1 despite net-positive
        // history). Re-seed once here, where Account is guaranteed live, so the rank-0 pick actually applies.
        private void ReseedRankedTemplateAtRealtime() {
            if (TemplateMode != 4 && TemplateMode != 5)
                return;

            List<int> ranked = BuildRankedTemplateList();
            if (ranked.Count == 0 || ranked[0] == _activeTemplateNumber)
                return;

            if (EffectiveMarketPosition() != MarketPosition.Flat || HasWorkingEntryOrder())
                return;

            if (PrintTemplateChanges)
                Print(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture)
                    + " | Mode " + TemplateMode + " re-seed at realtime: T" + _activeTemplateNumber + " -> T" + ranked[0]
                    + " (ledger " + Path.GetFileName(TemplatePnlStatePath()) + ")");

            _activeTemplateNumber = ranked[0];
            ApplyTemplate(_activeTemplateNumber);
            SaveTemplateState();
        }



        private const int RolloverDaysBeforeExpiry = 8;

        private string ResolveQuarterlyContractSymbol(string root) {
            if (string.IsNullOrWhiteSpace(root))
                return string.Empty;

            root = root.Trim();
            if (root.IndexOf(' ') >= 0)
                return root;

            DateTime today = DateTime.Now.Date;
            int year = today.Year;
            int month = today.Month;

            for (int i = 0; i < 36; i++) {
                DateTime candidate = new DateTime(year, month, 1).AddMonths(i);
                if (candidate.Month != 3 && candidate.Month != 6 && candidate.Month != 9 && candidate.Month != 12)
                    continue;

                DateTime rolloverDate = ThirdFridayOf(candidate.Year, candidate.Month).AddDays(-RolloverDaysBeforeExpiry);
                if (today < rolloverDate)
                    return root + " " + candidate.ToString("MM-yy", CultureInfo.InvariantCulture);
            }

            DateTime fallback = today.AddMonths(1);
            return root + " " + fallback.ToString("MM-yy", CultureInfo.InvariantCulture);
        }

        private DateTime ThirdFridayOf(int year, int month) {
            DateTime day = new DateTime(year, month, 1);
            while (day.DayOfWeek != DayOfWeek.Friday)
                day = day.AddDays(1);
            return day.AddDays(14);
        }

        private class SymbolContext {
            public int BarsInProgressIndex;
            public string Symbol = string.Empty;
            public string InstrumentFullName = string.Empty;
            public string SignalPrefix = string.Empty;

            public NinjaTrader.NinjaScript.Indicators.StochRSI StochRsi;
            public TEMA TemaIndicator;
            public Bollinger Bb;
            public MFI Mfi;
            public RSI Rsi;
            public MACD Macd;
            public ATR Atr;
            public double LastPullbackAtr;
            public double LastPullbackAtrAvg;
            public double LastPullbackAtrRatio = 1.0;
            public double LastPullbackAtrRatioRaw = 1.0;
            public Series<double> SessionVwapSeries;
            public Order EntryOrder;
            public DateTime EntryOrderSubmittedTime = DateTime.MinValue;
            public double EntryOrderSubmittedMarketPrice = 0.0;
            public double EntryOrderClosestApproachPrice = 0.0;
            public double EntryPrice;
            public DateTime EntryFillTime = DateTime.MinValue;
            public double OneRPoints;
            public double CurrentStopPrice;
            public double MaxFavorableExcursionPoints;
            public double MaxAdverseExcursionPoints;
            public int EntryBar = int.MinValue;
            public double TradeHighSinceEntry = double.NaN;
            public double TradeLowSinceEntry = double.NaN;
            public int LastTemplateMlSelectionBar = int.MinValue;
            public string TemplateMlStatus = "warming_up";
            public bool ActiveTemplateSetByMl;
            public DateTime PendingSetupTimestamp = DateTime.MinValue;
            public DateTime ActiveSetupTimestamp = DateTime.MinValue;
            public DateTime CurrentBarSetupTimestamp = DateTime.MinValue;
            public int CurrentBarSetupTimestampBar = int.MinValue;
            public double LastSubmittedStopPrice = double.NaN;
            public double PendingStopSubmitPrice = double.NaN;
            public double CumulativeTypicalVolume;
            public double CumulativeVolume;
            public double SessionVwap;
            public string LastSubmittedStopSignal = string.Empty;
            public string ActiveEntrySignal = string.Empty;
            public bool StartupEntrySignalsClear;
            public string PendingMlWindowJson = string.Empty;
            public string PendingMlTrigger = string.Empty;
            public string PendingMlPrediction = string.Empty;
            public double PendingMlConfidence;
            public string PendingMlSetupDirection = string.Empty;
            public string PendingMlSignal = string.Empty;
            public bool PendingMlReversal;
            public string LastNoTradeSource = string.Empty;
            public DateTime LastNoTradeLogTime = DateTime.MinValue;
            public string ActiveMlWindowJson = string.Empty;
            public string ActiveMlTrigger = string.Empty;
            public string ActiveMlPrediction = string.Empty;
            public double ActiveMlConfidence;
            public string ActiveMlSetupDirection = string.Empty;
            public string ActiveMlSignal = string.Empty;
            public bool ActiveMlReversal;
            public bool ActiveMlIsLong;
            public bool ActiveMlSampleLogged;
            public int MlBackfillSamplesSent;
            public int LastStopUpdateBar = -999999;
            public int LastExitBar = -999999;
            public int ReentryBlockedUntilBar = int.MinValue;
            public bool StopInitialized;
            public bool TakeProfitExitPending;
            public bool DailyLossLimitHit;
            public bool DailyLossExitPending;
            public bool ProtectiveStopWorking;
            public bool ProtectiveStopRearmPending;
            public bool ProtectiveStopFlattenPending;
            public int WatchdogTradeDirection;
            public int WatchdogTradeQuantity;
            public double WatchdogEntryPrice;
            public string WatchdogEntrySignal = string.Empty;
            public int WatchdogMismatchBar = int.MinValue;
            public int WatchdogLastManagedBar = int.MinValue;
            public bool HasDailyLossBaseline;
            public double DailyRealizedPnLDollars;
            public DateTime DailyBaselineDate = DateTime.MinValue;
            public int NearMissLogBar = -1;
            public int GateBlockLogBar = -1;
            public bool ExpireWatchIsLong;
            public double ExpireWatchLimitPrice;
            public DateTime ExpireWatchSubmittedTime = DateTime.MinValue;
            public int ExpireWatchTemplateNumber;
            public int ExpireWatchExpireMinutes;
            public bool FlattenOnEnablePending;
            public string ExitTradeId = string.Empty;
            public int ExitBarsHeld;
            public double ExitEntryPrice;
            public double ExitOneRPoints;
            public string ExitDirection = string.Empty;
            public List<double[]> ExitFeatureHistory = new List<double[]>();
            public string LastExitReason = "unknown";
            public int LastMlExitSampleBar = int.MinValue;
            public DateTime LastMlExitSampleLogTime = DateTime.MinValue;
            public int LastMlExitPredictionBar = int.MinValue;
            public int LastMlExitControlBar = int.MinValue;
            public bool MlExitSubmitted;
            public bool MlExitPhaseCheckPending;
            public DateTime MlExitPhaseCheckDate = DateTime.MinValue;
            public int MlExitRecommendedPhase;
            public bool MlExitPhase3Unlocked;
            public bool MlExitArmedPrinted;
            public int ActiveTemplateNumber = 1;
            public bool TemplateStateLoaded;
            public TimeSpan TemplateEligibleElapsed = TimeSpan.Zero;
            public DateTime TemplateLastEligibleClock = DateTime.MinValue;
            public bool ActiveTemplateWasWinner;
            public bool TemplateTimerInitialized;
            public TEMA[] BandTema;
            public Bollinger[] BandBb;
            public MFI[] BandMfi;
            public NinjaTrader.NinjaScript.Indicators.StochRSI[] BandStochRsi;
            public ShadowTrade[] ShadowTrades;
            public int[] ShadowCooldownUntilBar;
            public int LastShadowProcessedBar = int.MinValue;
            public int LastShadowSessionMinTemplate = int.MinValue;
            public int LastShadowSessionMaxTemplate = int.MinValue;
            public int LastLiveSessionMinTemplate = int.MinValue;
            public int LastLiveSessionMaxTemplate = int.MinValue;

            public int TemaLength;
            public int BBLength;
            public double BBStdDev;
            public bool EnableStochRsiCrossFilter;
            public int StochRsiPeriod;
            public double StochRsiLowerLine;
            public double StochRsiUpperLine;
            public int StochRsiCrossLookbackBars;
            public bool EnableMfiFilter;
            public int MfiPeriod;
            public int MfiPriorBars;
            public double MfiLongMax;
            public double MfiShortMin;
            public bool EnableRsiFilter;
            public double RsiLongMax;
            public double RsiShortMin;
            public bool EnableTemaVwapMidBbCrossEntry;
            public double MlMinConfidence;
            public double MlExitHoldThreshold;
            public int MinBarsBeforeMlExit;
            public double MinUnrealizedRForMlExit;
            public int PullbackTicks;
            public int EntryOrderExpireMinutes;
            public double RiskDollars1R;
            public double LadderRiskDollars1R;
            public int Contracts;
            public double DailyEntryRiskDollars;
            public double DailyEntrySlippageDollars;
            public int ReentryCooldownBars;
        }

        private readonly List<SymbolContext> symbolContexts = new List<SymbolContext>();
        private readonly Dictionary<int, SymbolContext> contextsByBarsInProgress = new Dictionary<int, SymbolContext>();
        private readonly Dictionary<string, SymbolContext> contextsByInstrumentFullName = new Dictionary<string, SymbolContext>(StringComparer.OrdinalIgnoreCase);
        private SymbolContext activeContext;

        private int CurrentBarsInProgressIndex() {
            return activeContext != null ? activeContext.BarsInProgressIndex : BarsInProgress;
        }

        // Active context bar index.
        private int CurrentContextBar {
            get { return CurrentBars[CurrentBarsInProgressIndex()]; }
        }

        private bool IsActiveContextBarsInProgress() {
            return activeContext == null ? BarsInProgress == 0 : BarsInProgress == activeContext.BarsInProgressIndex;
        }

        private Instrument CurrentInstrument {
            get {
                int bip = CurrentBarsInProgressIndex();
                if (BarsArray != null && bip >= 0 && bip < BarsArray.Length && BarsArray[bip] != null && BarsArray[bip].Instrument != null)
                    return BarsArray[bip].Instrument;
                return Instrument;
            }
        }

        private BarsPeriod CurrentBarsPeriod {
            get {
                int bip = CurrentBarsInProgressIndex();
                if (BarsArray != null && bip >= 0 && bip < BarsArray.Length && BarsArray[bip] != null && BarsArray[bip].BarsPeriod != null)
                    return BarsArray[bip].BarsPeriod;
                return BarsPeriod;
            }
        }

        // Session state for the active context.
        private bool IsCurrentContextFirstBarOfSession {
            get {
                int bip = CurrentBarsInProgressIndex();
                if (BarsArray != null && bip >= 0 && bip < BarsArray.Length && BarsArray[bip] != null)
                    return BarsArray[bip].IsFirstBarOfSession;
                return Bars.IsFirstBarOfSession;
            }
        }

        private Position CurrentPosition {
            get {
                int bip = CurrentBarsInProgressIndex();
                if (Positions != null && bip >= 0 && bip < Positions.Length && Positions[bip] != null)
                    return Positions[bip];
                return Position;
            }
        }

        private string ContextSignalName(string signalName) {
            if (!EnableMultiSymbolMode || activeContext == null || string.IsNullOrEmpty(signalName) || signalName.IndexOf('_') >= 0)
                return signalName;

            return activeContext.SignalPrefix + "_" + signalName;
        }

        private string BaseSignalName(string signalName) {
            if (string.IsNullOrEmpty(signalName))
                return string.Empty;
            int index = signalName.LastIndexOf('_');
            return index >= 0 && index < signalName.Length - 1 ? signalName.Substring(index + 1) : signalName;
        }

        private void BuildMultiSymbolContexts() {
            symbolContexts.Clear();
            contextsByBarsInProgress.Clear();
            contextsByInstrumentFullName.Clear();

            Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": EnableMultiSymbolMode=" + EnableMultiSymbolMode
                + " EnableSymbol1=" + EnableSymbol1
                + " EnableSymbol2=" + EnableSymbol2 + " Symbol2Name='" + Symbol2Name + "'"
                + " EnableSymbol3=" + EnableSymbol3 + " Symbol3Name='" + Symbol3Name + "'"
                + " EnableSymbol4=" + EnableSymbol4 + " Symbol4Name='" + Symbol4Name + "'"
                + " BarsArray.Length=" + (BarsArray == null ? -1 : BarsArray.Length));

            int bip = 0;
            if (EnableSymbol1)
                AddSymbolContext(bip);
            else
                Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": Symbol1 (primary) DISABLED, skipped");

            bip = 1;
            if (EnableSymbol2 && !string.IsNullOrWhiteSpace(Symbol2Name))
                AddSymbolContext(bip++);
            else
                Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": Symbol2 SKIPPED (EnableSymbol2=" + EnableSymbol2 + " Symbol2Name='" + Symbol2Name + "')");
            if (EnableSymbol3 && !string.IsNullOrWhiteSpace(Symbol3Name))
                AddSymbolContext(bip++);
            else
                Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": Symbol3 SKIPPED (EnableSymbol3=" + EnableSymbol3 + " Symbol3Name='" + Symbol3Name + "')");
            if (EnableSymbol4 && !string.IsNullOrWhiteSpace(Symbol4Name))
                AddSymbolContext(bip++);
            else
                Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": Symbol4 SKIPPED (EnableSymbol4=" + EnableSymbol4 + " Symbol4Name='" + Symbol4Name + "')");

            Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": total contexts registered=" + symbolContexts.Count
                + " bips=[" + string.Join(",", contextsByBarsInProgress.Keys) + "]");

            // Phone alerts silently off is the unsafe failure (they cover naked positions), so the
            // missing-config case is announced here rather than discovered when an alert never arrives.
            EnsureNtfyConfigLoaded();
            if (!NtfyConfigured)
                Print(OutputTimePrefix() + "PHONE ALERTS DISABLED " + Name + ": no ntfy topic in "
                    + Path.Combine(NinjaTrader.Core.Globals.UserDataDir, NtfyConfigFileName)
                    + " (expected 'topic=' and 'accounts=' lines)");
        }

        private void AddSymbolContext(int bip) {
            if (BarsArray == null || bip < 0 || bip >= BarsArray.Length || BarsArray[bip] == null) {
                Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": AddSymbolContext BAILED for bip=" + bip
                    + " (BarsArray==null:" + (BarsArray == null) + " BarsArray.Length=" + (BarsArray == null ? -1 : BarsArray.Length) + ")");
                return;
            }

            SymbolContext ctx = new SymbolContext();
            ctx.BarsInProgressIndex = bip;
            ctx.InstrumentFullName = BarsArray[bip].Instrument != null ? BarsArray[bip].Instrument.FullName : ("BIP" + bip.ToString(CultureInfo.InvariantCulture));
            Print(OutputTimePrefix() + "CONTEXT SETUP " + Name + ": AddSymbolContext SUCCEEDED for bip=" + bip + " instrument=" + ctx.InstrumentFullName);
            ctx.Symbol = BarsArray[bip].Instrument != null && BarsArray[bip].Instrument.MasterInstrument != null
                ? BarsArray[bip].Instrument.MasterInstrument.Name
                : ctx.InstrumentFullName;
            ctx.SignalPrefix = MakeSignalPrefix(ctx.Symbol, bip);
            InitializeContextRuntimeDefaults(ctx);
            BuildBandIndicatorsForContext(ctx);
            ctx.Rsi = RSI(Closes[bip], 14, 3);
            ctx.Macd = MACD(Closes[bip], 5, 13, 3);
            ctx.Atr = ATR(Closes[bip], 14);
            // Synced to THIS context's bars via ctx.Atr (whose input is Closes[bip]), not to the primary
            // chart series. new Series<double>(this) synced to the primary, while every read/write indexes
            // in context-bar space -- which threw out-of-range whenever this context had a bar the primary
            // didn't (2026-07-19: bar-0 crash on the first Sunday-open tick when a secondary symbol ticked
            // before the primary on a history-less chart, and bar-33/34 crashes when the ML window walked
            // 33 bars back in context space past the primary's bar count). It also silently misaligned the
            // vwap values in ML feature windows for secondary symbols whenever bar counts diverged.
            ctx.SessionVwapSeries = new Series<double>(ctx.Atr);

            symbolContexts.Add(ctx);
            contextsByBarsInProgress[bip] = ctx;
            if (!string.IsNullOrEmpty(ctx.InstrumentFullName))
                contextsByInstrumentFullName[ctx.InstrumentFullName] = ctx;

            RunWithContext(bip, () => {
                InitializeTemplateRotation();
            });
        }

        private string MakeSignalPrefix(string symbol, int bip) {
            string raw = string.IsNullOrWhiteSpace(symbol) ? ("BIP" + bip.ToString(CultureInfo.InvariantCulture)) : symbol.ToUpperInvariant();
            StringBuilder builder = new StringBuilder();
            for (int i = 0; i < raw.Length; i++) {
                char c = raw[i];
                if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))
                    builder.Append(c);
            }
            if (builder.Length == 0)
                builder.Append("BIP").Append(bip.ToString(CultureInfo.InvariantCulture));
            return builder.ToString();
        }

        private void InitializeContextRuntimeDefaults(SymbolContext ctx) {
            ctx.LastStopUpdateBar = -999999;
            ctx.LastExitBar = -999999;
            ctx.ReentryBlockedUntilBar = int.MinValue;
            ctx.EntryBar = int.MinValue;
            ctx.LastSubmittedStopPrice = double.NaN;
            ctx.TradeHighSinceEntry = double.NaN;
            ctx.TradeLowSinceEntry = double.NaN;
            ctx.WatchdogMismatchBar = int.MinValue;
            ctx.WatchdogLastManagedBar = int.MinValue;
            ctx.DailyBaselineDate = DateTime.MinValue;
            ctx.NearMissLogBar = -1;
            ctx.GateBlockLogBar = -1;
            ctx.CurrentBarSetupTimestamp = DateTime.MinValue;
            ctx.CurrentBarSetupTimestampBar = int.MinValue;
            ctx.ExpireWatchLimitPrice = 0.0;
            ctx.ExpireWatchSubmittedTime = DateTime.MinValue;
            ctx.LastMlExitSampleBar = int.MinValue;
            ctx.LastMlExitPredictionBar = int.MinValue;
            ctx.LastMlExitControlBar = int.MinValue;
            ctx.MlExitRecommendedPhase = 0;
            int initMin, initMax;
            GetActiveSessionTemplateBounds(out initMin, out initMax);
            ctx.ActiveTemplateNumber = Math.Max(initMin, Math.Min(initMax, TemplateNumber));
            ctx.ActiveTemplateWasWinner = false;
            ctx.TemplateTimerInitialized = false;
            ctx.LastShadowProcessedBar = int.MinValue;
            ctx.LastShadowSessionMinTemplate = int.MinValue;
            ctx.LastShadowSessionMaxTemplate = int.MinValue;
            ctx.LastLiveSessionMinTemplate = int.MinValue;
            ctx.LastLiveSessionMaxTemplate = int.MinValue;
        }

        private void BuildBandIndicatorsForContext(SymbolContext ctx) {
            int bands = BandSelectivities.Length;
            ctx.BandTema = new TEMA[bands];
            ctx.BandBb = new Bollinger[bands];
            ctx.BandMfi = new MFI[bands];
            ctx.BandStochRsi = new NinjaTrader.NinjaScript.Indicators.StochRSI[bands];

            for (int i = 0; i < bands; i++) {
                double s = BandSelectivities[i];
                ctx.BandTema[i] = TEMA(Closes[ctx.BarsInProgressIndex], DerivedTemaLength(s));
                ctx.BandBb[i] = Bollinger(Closes[ctx.BarsInProgressIndex], DerivedBbStdDev(s), DerivedBbLength(s));
                ctx.BandMfi[i] = MFI(Closes[ctx.BarsInProgressIndex], DerivedMfiPeriod(s));
                ctx.BandStochRsi[i] = StochRSI(Closes[ctx.BarsInProgressIndex], DerivedStochRsiPeriod(s));
            }

            ctx.ShadowTrades = new ShadowTrade[TemplateSlotCount];
            ctx.ShadowCooldownUntilBar = new int[TemplateSlotCount];
            for (int t = 0; t < ctx.ShadowCooldownUntilBar.Length; t++)
                ctx.ShadowCooldownUntilBar[t] = int.MinValue;
            ctx.LastShadowSessionMinTemplate = int.MinValue;
            ctx.LastShadowSessionMaxTemplate = int.MinValue;
        }

        private SymbolContext ContextForOrder(Order order) {
            if (order != null && order.Instrument != null && contextsByInstrumentFullName.ContainsKey(order.Instrument.FullName))
                return contextsByInstrumentFullName[order.Instrument.FullName];
            return null;
        }

        private SymbolContext ContextForExecution(Execution execution) {
            if (execution != null && execution.Order != null)
                return ContextForOrder(execution.Order);
            return null;
        }

        private void RunWithContext(int barsInProgressIndex, Action action) {
            lock (contextSwitchLock) {
                SymbolContext ctx;
                if (!contextsByBarsInProgress.TryGetValue(barsInProgressIndex, out ctx))
                    return;

                SymbolContext previous = activeContext;
                activeContext = ctx;
                LoadContext(ctx);
                try {
                    action();
                }
                finally {
                    SaveContext(ctx);
                    activeContext = previous;
                    if (previous != null)
                        LoadContext(previous);
                }
            }
        }

        private void LoadContext(SymbolContext ctx) {
            stochRsi = ctx.StochRsi;
            temaIndicator = ctx.TemaIndicator;
            bb = ctx.Bb;
            mfi = ctx.Mfi;
            rsi = ctx.Rsi;
            macd = ctx.Macd;
            atr = ctx.Atr;
            sessionVwapSeries = ctx.SessionVwapSeries;
            entryOrder = ctx.EntryOrder;
            entryOrderSubmittedTime = ctx.EntryOrderSubmittedTime;
            entryOrderSubmittedMarketPrice = ctx.EntryOrderSubmittedMarketPrice;
            entryOrderClosestApproachPrice = ctx.EntryOrderClosestApproachPrice;
            entryPrice = ctx.EntryPrice;
            entryFillTime = ctx.EntryFillTime;
            oneRPoints = ctx.OneRPoints;
            currentStopPrice = ctx.CurrentStopPrice;
            maxFavorableExcursionPoints = ctx.MaxFavorableExcursionPoints;
            maxAdverseExcursionPoints = ctx.MaxAdverseExcursionPoints;
            entryBar = ctx.EntryBar;
            tradeHighSinceEntry = ctx.TradeHighSinceEntry;
            tradeLowSinceEntry = ctx.TradeLowSinceEntry;
            _lastTemplateMlSelectionBar = ctx.LastTemplateMlSelectionBar;
            _templateMlStatus = ctx.TemplateMlStatus;
            _activeTemplateSetByMl = ctx.ActiveTemplateSetByMl;
            pendingSetupTimestamp = ctx.PendingSetupTimestamp;
            activeSetupTimestamp = ctx.ActiveSetupTimestamp;
            currentBarSetupTimestamp = ctx.CurrentBarSetupTimestamp;
            currentBarSetupTimestampBar = ctx.CurrentBarSetupTimestampBar;
            lastSubmittedStopPrice = ctx.LastSubmittedStopPrice;
            pendingStopSubmitPrice = ctx.PendingStopSubmitPrice;
            cumulativeTypicalVolume = ctx.CumulativeTypicalVolume;
            cumulativeVolume = ctx.CumulativeVolume;
            sessionVwap = ctx.SessionVwap;
            lastSubmittedStopSignal = ctx.LastSubmittedStopSignal; activeEntrySignal = ctx.ActiveEntrySignal; startupEntrySignalsClear = ctx.StartupEntrySignalsClear;
            pendingMlWindowJson = ctx.PendingMlWindowJson;
            pendingMlTrigger = ctx.PendingMlTrigger;
            pendingMlPrediction = ctx.PendingMlPrediction;
            pendingMlConfidence = ctx.PendingMlConfidence;
            pendingMlSetupDirection = ctx.PendingMlSetupDirection;
            pendingMlSignal = ctx.PendingMlSignal;
            pendingMlReversal = ctx.PendingMlReversal;
            _lastNoTradeSource = ctx.LastNoTradeSource;
            _lastNoTradeLogTime = ctx.LastNoTradeLogTime;
            activeMlWindowJson = ctx.ActiveMlWindowJson;
            activeMlTrigger = ctx.ActiveMlTrigger;
            activeMlPrediction = ctx.ActiveMlPrediction;
            activeMlConfidence = ctx.ActiveMlConfidence;
            activeMlSetupDirection = ctx.ActiveMlSetupDirection;
            activeMlSignal = ctx.ActiveMlSignal;
            activeMlReversal = ctx.ActiveMlReversal;
            activeMlIsLong = ctx.ActiveMlIsLong;
            activeMlSampleLogged = ctx.ActiveMlSampleLogged;
            mlBackfillSamplesSent = ctx.MlBackfillSamplesSent;
            lastStopUpdateBar = ctx.LastStopUpdateBar;
            lastExitBar = ctx.LastExitBar;
            reentryBlockedUntilBar = ctx.ReentryBlockedUntilBar;
            stopInitialized = ctx.StopInitialized;
            takeProfitExitPending = ctx.TakeProfitExitPending;
            dailyLossLimitHit = ctx.DailyLossLimitHit;
            dailyLossExitPending = ctx.DailyLossExitPending;
            protectiveStopWorking = ctx.ProtectiveStopWorking;
            protectiveStopRearmPending = ctx.ProtectiveStopRearmPending;
            protectiveStopFlattenPending = ctx.ProtectiveStopFlattenPending;
            watchdogTradeDirection = ctx.WatchdogTradeDirection;
            watchdogTradeQuantity = ctx.WatchdogTradeQuantity;
            watchdogEntryPrice = ctx.WatchdogEntryPrice;
            watchdogEntrySignal = ctx.WatchdogEntrySignal;
            watchdogMismatchBar = ctx.WatchdogMismatchBar;
            watchdogLastManagedBar = ctx.WatchdogLastManagedBar;
            hasDailyLossBaseline = ctx.HasDailyLossBaseline;
            dailyRealizedPnLDollars = ctx.DailyRealizedPnLDollars;
            dailyBaselineDate = ctx.DailyBaselineDate;
            nearMissLogBar = ctx.NearMissLogBar;
            gateBlockLogBar = ctx.GateBlockLogBar;
            expireWatchIsLong = ctx.ExpireWatchIsLong;
            expireWatchLimitPrice = ctx.ExpireWatchLimitPrice;
            expireWatchSubmittedTime = ctx.ExpireWatchSubmittedTime;
            expireWatchTemplateNumber = ctx.ExpireWatchTemplateNumber;
            expireWatchExpireMinutes = ctx.ExpireWatchExpireMinutes;
            flattenOnEnablePending = ctx.FlattenOnEnablePending;
            _exitTradeId = ctx.ExitTradeId;
            _exitBarsHeld = ctx.ExitBarsHeld;
            _exitEntryPrice = ctx.ExitEntryPrice;
            _exitOneRPoints = ctx.ExitOneRPoints;
            _exitDirection = ctx.ExitDirection;
            _exitFeatureHistory = ctx.ExitFeatureHistory;
            _lastExitReason = ctx.LastExitReason;
            _lastMlExitSampleBar = ctx.LastMlExitSampleBar;
            _lastMlExitSampleLogTime = ctx.LastMlExitSampleLogTime;
            _lastMlExitPredictionBar = ctx.LastMlExitPredictionBar;
            _lastMlExitControlBar = ctx.LastMlExitControlBar;
            _mlExitSubmitted = ctx.MlExitSubmitted;
            _mlExitPhaseCheckPending = ctx.MlExitPhaseCheckPending;
            _mlExitPhaseCheckDate = ctx.MlExitPhaseCheckDate;
            _mlExitRecommendedPhase = ctx.MlExitRecommendedPhase;
            _mlExitPhase3Unlocked = ctx.MlExitPhase3Unlocked;
            _mlExitArmedPrinted = ctx.MlExitArmedPrinted;
            _activeTemplateNumber = ctx.ActiveTemplateNumber;
            _templateStateLoaded = ctx.TemplateStateLoaded;
            _templateEligibleElapsed = ctx.TemplateEligibleElapsed;
            _templateLastEligibleClock = ctx.TemplateLastEligibleClock;
            _activeTemplateWasWinner = ctx.ActiveTemplateWasWinner;
            _templateTimerInitialized = ctx.TemplateTimerInitialized;
            bandTema = ctx.BandTema;
            bandBb = ctx.BandBb;
            bandMfi = ctx.BandMfi;
            bandStochRsi = ctx.BandStochRsi;
            _shadowTrades = ctx.ShadowTrades;
            _shadowCooldownUntilBar = ctx.ShadowCooldownUntilBar;
            _lastShadowProcessedBar = ctx.LastShadowProcessedBar;
            _lastShadowSessionMinTemplate = ctx.LastShadowSessionMinTemplate;
            _lastShadowSessionMaxTemplate = ctx.LastShadowSessionMaxTemplate;
            _lastLiveSessionMinTemplate = ctx.LastLiveSessionMinTemplate;
            _lastLiveSessionMaxTemplate = ctx.LastLiveSessionMaxTemplate;
            TemaLength = ctx.TemaLength;
            BBLength = ctx.BBLength;
            BBStdDev = ctx.BBStdDev;
            EnableStochRsiCrossFilter = ctx.EnableStochRsiCrossFilter;
            StochRsiPeriod = ctx.StochRsiPeriod;
            StochRsiLowerLine = ctx.StochRsiLowerLine;
            StochRsiUpperLine = ctx.StochRsiUpperLine;
            StochRsiCrossLookbackBars = ctx.StochRsiCrossLookbackBars;
            EnableMfiFilter = ctx.EnableMfiFilter;
            MfiPeriod = ctx.MfiPeriod;
            MfiPriorBars = ctx.MfiPriorBars;
            MfiLongMax = ctx.MfiLongMax;
            MfiShortMin = ctx.MfiShortMin;
            EnableRsiFilter = ctx.EnableRsiFilter;
            RsiLongMax = ctx.RsiLongMax;
            RsiShortMin = ctx.RsiShortMin;
            EnableTemaVwapMidBbCrossEntry = ctx.EnableTemaVwapMidBbCrossEntry;
            MlMinConfidence = ctx.MlMinConfidence;
            MlExitHoldThreshold = ctx.MlExitHoldThreshold;
            MinBarsBeforeMlExit = ctx.MinBarsBeforeMlExit;
            MinUnrealizedRForMlExit = ctx.MinUnrealizedRForMlExit;
            PullbackTicks = ctx.PullbackTicks;
            EntryOrderExpireMinutes = ctx.EntryOrderExpireMinutes;
            RiskDollars1R = ctx.RiskDollars1R;
            _lastPullbackAtr = ctx.LastPullbackAtr; _lastPullbackAtrAvg = ctx.LastPullbackAtrAvg; _lastPullbackAtrRatio = ctx.LastPullbackAtrRatio; _lastPullbackAtrRatioRaw = ctx.LastPullbackAtrRatioRaw;
            LadderRiskDollars1R = ctx.LadderRiskDollars1R;
            Contracts = ctx.Contracts;
            DailyEntryRiskDollars = ctx.DailyEntryRiskDollars;
            DailyEntrySlippageDollars = ctx.DailyEntrySlippageDollars;
            ReentryCooldownBars = ctx.ReentryCooldownBars;
        }

        private void SaveContext(SymbolContext ctx) {
            ctx.StochRsi = stochRsi;
            ctx.TemaIndicator = temaIndicator;
            ctx.Bb = bb;
            ctx.Mfi = mfi;
            ctx.Rsi = rsi;
            ctx.Macd = macd;
            ctx.Atr = atr;
            ctx.SessionVwapSeries = sessionVwapSeries;
            ctx.EntryOrder = entryOrder;
            ctx.EntryOrderSubmittedTime = entryOrderSubmittedTime;
            ctx.EntryOrderSubmittedMarketPrice = entryOrderSubmittedMarketPrice;
            ctx.EntryOrderClosestApproachPrice = entryOrderClosestApproachPrice;
            ctx.EntryPrice = entryPrice;
            ctx.EntryFillTime = entryFillTime;
            ctx.OneRPoints = oneRPoints;
            ctx.CurrentStopPrice = currentStopPrice;
            ctx.MaxFavorableExcursionPoints = maxFavorableExcursionPoints;
            ctx.MaxAdverseExcursionPoints = maxAdverseExcursionPoints;
            ctx.EntryBar = entryBar;
            ctx.TradeHighSinceEntry = tradeHighSinceEntry;
            ctx.TradeLowSinceEntry = tradeLowSinceEntry;
            ctx.LastTemplateMlSelectionBar = _lastTemplateMlSelectionBar;
            ctx.TemplateMlStatus = _templateMlStatus;
            ctx.ActiveTemplateSetByMl = _activeTemplateSetByMl;
            ctx.PendingSetupTimestamp = pendingSetupTimestamp;
            ctx.ActiveSetupTimestamp = activeSetupTimestamp;
            ctx.CurrentBarSetupTimestamp = currentBarSetupTimestamp;
            ctx.CurrentBarSetupTimestampBar = currentBarSetupTimestampBar;
            ctx.LastSubmittedStopPrice = lastSubmittedStopPrice;
            ctx.PendingStopSubmitPrice = pendingStopSubmitPrice;
            ctx.CumulativeTypicalVolume = cumulativeTypicalVolume;
            ctx.CumulativeVolume = cumulativeVolume;
            ctx.SessionVwap = sessionVwap;
            ctx.LastSubmittedStopSignal = lastSubmittedStopSignal; ctx.ActiveEntrySignal = activeEntrySignal; ctx.StartupEntrySignalsClear = startupEntrySignalsClear;
            ctx.PendingMlWindowJson = pendingMlWindowJson;
            ctx.PendingMlTrigger = pendingMlTrigger;
            ctx.PendingMlPrediction = pendingMlPrediction;
            ctx.PendingMlConfidence = pendingMlConfidence;
            ctx.PendingMlSetupDirection = pendingMlSetupDirection;
            ctx.PendingMlSignal = pendingMlSignal;
            ctx.PendingMlReversal = pendingMlReversal;
            ctx.LastNoTradeSource = _lastNoTradeSource;
            ctx.LastNoTradeLogTime = _lastNoTradeLogTime;
            ctx.ActiveMlWindowJson = activeMlWindowJson;
            ctx.ActiveMlTrigger = activeMlTrigger;
            ctx.ActiveMlPrediction = activeMlPrediction;
            ctx.ActiveMlConfidence = activeMlConfidence;
            ctx.ActiveMlSetupDirection = activeMlSetupDirection;
            ctx.ActiveMlSignal = activeMlSignal;
            ctx.ActiveMlReversal = activeMlReversal;
            ctx.ActiveMlIsLong = activeMlIsLong;
            ctx.ActiveMlSampleLogged = activeMlSampleLogged;
            ctx.MlBackfillSamplesSent = mlBackfillSamplesSent;
            ctx.LastStopUpdateBar = lastStopUpdateBar;
            ctx.LastExitBar = lastExitBar;
            ctx.ReentryBlockedUntilBar = reentryBlockedUntilBar;
            ctx.StopInitialized = stopInitialized;
            ctx.TakeProfitExitPending = takeProfitExitPending;
            ctx.DailyLossLimitHit = dailyLossLimitHit;
            ctx.DailyLossExitPending = dailyLossExitPending;
            ctx.ProtectiveStopWorking = protectiveStopWorking;
            ctx.ProtectiveStopRearmPending = protectiveStopRearmPending;
            ctx.ProtectiveStopFlattenPending = protectiveStopFlattenPending;
            ctx.WatchdogTradeDirection = watchdogTradeDirection;
            ctx.WatchdogTradeQuantity = watchdogTradeQuantity;
            ctx.WatchdogEntryPrice = watchdogEntryPrice;
            ctx.WatchdogEntrySignal = watchdogEntrySignal;
            ctx.WatchdogMismatchBar = watchdogMismatchBar;
            ctx.WatchdogLastManagedBar = watchdogLastManagedBar;
            ctx.HasDailyLossBaseline = hasDailyLossBaseline;
            ctx.DailyRealizedPnLDollars = dailyRealizedPnLDollars;
            ctx.DailyBaselineDate = dailyBaselineDate;
            ctx.NearMissLogBar = nearMissLogBar;
            ctx.GateBlockLogBar = gateBlockLogBar;
            ctx.ExpireWatchIsLong = expireWatchIsLong;
            ctx.ExpireWatchLimitPrice = expireWatchLimitPrice;
            ctx.ExpireWatchSubmittedTime = expireWatchSubmittedTime;
            ctx.ExpireWatchTemplateNumber = expireWatchTemplateNumber;
            ctx.ExpireWatchExpireMinutes = expireWatchExpireMinutes;
            ctx.FlattenOnEnablePending = flattenOnEnablePending;
            ctx.ExitTradeId = _exitTradeId;
            ctx.ExitBarsHeld = _exitBarsHeld;
            ctx.ExitEntryPrice = _exitEntryPrice;
            ctx.ExitOneRPoints = _exitOneRPoints;
            ctx.ExitDirection = _exitDirection;
            ctx.ExitFeatureHistory = _exitFeatureHistory;
            ctx.LastExitReason = _lastExitReason;
            ctx.LastMlExitSampleBar = _lastMlExitSampleBar;
            ctx.LastMlExitSampleLogTime = _lastMlExitSampleLogTime;
            ctx.LastMlExitPredictionBar = _lastMlExitPredictionBar;
            ctx.LastMlExitControlBar = _lastMlExitControlBar;
            ctx.MlExitSubmitted = _mlExitSubmitted;
            ctx.MlExitPhaseCheckPending = _mlExitPhaseCheckPending;
            ctx.MlExitPhaseCheckDate = _mlExitPhaseCheckDate;
            ctx.MlExitRecommendedPhase = _mlExitRecommendedPhase;
            ctx.MlExitPhase3Unlocked = _mlExitPhase3Unlocked;
            ctx.MlExitArmedPrinted = _mlExitArmedPrinted;
            ctx.ActiveTemplateNumber = _activeTemplateNumber;
            ctx.TemplateStateLoaded = _templateStateLoaded;
            ctx.TemplateEligibleElapsed = _templateEligibleElapsed;
            ctx.TemplateLastEligibleClock = _templateLastEligibleClock;
            ctx.ActiveTemplateWasWinner = _activeTemplateWasWinner;
            ctx.TemplateTimerInitialized = _templateTimerInitialized;
            ctx.BandTema = bandTema;
            ctx.BandBb = bandBb;
            ctx.BandMfi = bandMfi;
            ctx.BandStochRsi = bandStochRsi;
            ctx.ShadowTrades = _shadowTrades;
            ctx.ShadowCooldownUntilBar = _shadowCooldownUntilBar;
            ctx.LastShadowProcessedBar = _lastShadowProcessedBar;
            // Mirror the shadow-session range so ClearOutOfBandShadowTradesIfNeeded's change
            // detection works across bars (LoadContext restores these; without the save they
            // reset to int.MinValue every switch-in, forcing a full clear-scan each bar).
            ctx.LastShadowSessionMinTemplate = _lastShadowSessionMinTemplate;
            ctx.LastShadowSessionMaxTemplate = _lastShadowSessionMaxTemplate;
            ctx.LastLiveSessionMinTemplate = _lastLiveSessionMinTemplate;
            ctx.LastLiveSessionMaxTemplate = _lastLiveSessionMaxTemplate;
            ctx.TemaLength = TemaLength;
            ctx.BBLength = BBLength;
            ctx.BBStdDev = BBStdDev;
            ctx.EnableStochRsiCrossFilter = EnableStochRsiCrossFilter;
            ctx.StochRsiPeriod = StochRsiPeriod;
            ctx.StochRsiLowerLine = StochRsiLowerLine;
            ctx.StochRsiUpperLine = StochRsiUpperLine;
            ctx.StochRsiCrossLookbackBars = StochRsiCrossLookbackBars;
            ctx.EnableMfiFilter = EnableMfiFilter;
            ctx.MfiPeriod = MfiPeriod;
            ctx.MfiPriorBars = MfiPriorBars;
            ctx.MfiLongMax = MfiLongMax;
            ctx.MfiShortMin = MfiShortMin;
            ctx.EnableRsiFilter = EnableRsiFilter;
            ctx.RsiLongMax = RsiLongMax;
            ctx.RsiShortMin = RsiShortMin;
            ctx.EnableTemaVwapMidBbCrossEntry = EnableTemaVwapMidBbCrossEntry;
            ctx.MlMinConfidence = MlMinConfidence;
            ctx.MlExitHoldThreshold = MlExitHoldThreshold;
            ctx.MinBarsBeforeMlExit = MinBarsBeforeMlExit;
            ctx.MinUnrealizedRForMlExit = MinUnrealizedRForMlExit;
            ctx.PullbackTicks = PullbackTicks;
            ctx.EntryOrderExpireMinutes = EntryOrderExpireMinutes;
            ctx.RiskDollars1R = RiskDollars1R;
            ctx.LastPullbackAtr = _lastPullbackAtr; ctx.LastPullbackAtrAvg = _lastPullbackAtrAvg; ctx.LastPullbackAtrRatio = _lastPullbackAtrRatio; ctx.LastPullbackAtrRatioRaw = _lastPullbackAtrRatioRaw;
            ctx.LadderRiskDollars1R = LadderRiskDollars1R;
            ctx.Contracts = Contracts;
            ctx.DailyEntryRiskDollars = DailyEntryRiskDollars;
            ctx.DailyEntrySlippageDollars = DailyEntrySlippageDollars;
            ctx.ReentryCooldownBars = ReentryCooldownBars;
        }


        // "BarsPeriodType_Value" for the active series (e.g. "Volume_1000"), so per-series template files never
        // collide between two instances on the same account+instrument. Falls back to "series" when the bars
        // aren't resolvable yet.
        private string ResolveSeriesTag() {
            try {
                int bip = CurrentBarsInProgressIndex();
                if (BarsArray != null && bip >= 0 && bip < BarsArray.Length && BarsArray[bip] != null && BarsArray[bip].BarsPeriod != null)
                    return BarsArray[bip].BarsPeriod.BarsPeriodType + "_" + BarsArray[bip].BarsPeriod.Value;
            }
            catch {
            }
            return "series";
        }

        private string TemplateStatePath() {
            string instrumentName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : "UNKNOWN";
            // Account included so two instances (live/sim or separate accounts) never share rotation state on disk.
            string accountName = Account != null ? SanitizeFileNamePart(Account.Name) : "UNKNOWN";
            string safeName = "temalimit_template_state_" + accountName + "_" + instrumentName + "_" + ResolveSeriesTag() + ".txt";
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, safeName);
        }

        // Per instrument+series, NOT per account -- same key as TemplatePnlStatePath. "Has template N been
        // tried on NQ 1000 Volume" is a fact about the template and the series, so every account shares one
        // checklist and no sibling re-tests what another already covered. Series-scoped because a 1000 Volume
        // instance and a Renko instance are testing genuinely different things.
        private string TemplateUsageStatePath() {
            string instrumentName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : "UNKNOWN";
            string safeName = "temalimit_template_usage_" + instrumentName + "_" + ResolveSeriesTag() + ".txt";
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, safeName);
        }

        private void LoadTemplateUsage() {
            _usedTemplateNumbers.Clear();
            string path = TemplateUsageStatePath();
            try {
                lock (templateUsageLock) {
                    if (File.Exists(path)) {
                        foreach (string part in File.ReadAllText(path).Split(',')) {
                            int templateNumber;
                            if (int.TryParse(part.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out templateNumber))
                                _usedTemplateNumbers.Add(templateNumber);
                        }
                    }
                }
            }
            catch (Exception error) {
                D("Template usage load failed: " + error.Message);
            }
        }

        // Re-reads the shared usage record before merging templateNumber, so a concurrent sibling write isn't clobbered.
        private void MarkTemplateUsed(int templateNumber) {
            string path = TemplateUsageStatePath();
            try {
                lock (templateUsageLock) {
                    HashSet<int> merged = new HashSet<int>();
                    if (File.Exists(path)) {
                        foreach (string part in File.ReadAllText(path).Split(',')) {
                            int existing;
                            if (int.TryParse(part.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out existing))
                                merged.Add(existing);
                        }
                    }
                    merged.Add(templateNumber);

                    var ordered = new List<int>(merged);
                    ordered.Sort();
                    File.WriteAllText(path, string.Join(",", ordered));

                    _usedTemplateNumbers = merged;
                }
            }
            catch (Exception error) {
                D("Template usage save failed: " + error.Message);
            }
        }

        // Every mode except 0 (Manual) persists rotation state and actively rotates. Central predicate so
        // adding a rotation mode is one edit here instead of one per guard.
        private bool IsRotatingTemplateMode() {
            return TemplateMode == 1 || TemplateMode == 2 || TemplateMode == 3 || TemplateMode == 4 || TemplateMode == 5;
        }

        // Per instrument+series, deliberately NOT per account: "how has template N done on NQ 1000 Volume"
        // is a property of the template and the series, not of who traded it, so every account pools into
        // one ledger and a new account inherits the full history instead of ranking from an empty file.
        // Series-scoped because the same template number performs very differently on 1000 Volume vs Renko.
        // (TemplateUsageStatePath stays account-scoped -- mode 3's "already tried" pool is per-instance
        // rotation position, which siblings must not consume for each other.)
        // Format: "template:dollars" pairs, comma-separated.
        private string TemplatePnlStatePath() {
            string instrumentName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : "UNKNOWN";
            string safeName = "temalimit_template_pnl_" + instrumentName + "_" + ResolveSeriesTag() + ".txt";
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, safeName);
        }

        private void LoadTemplatePnl() {
            _templatePnl.Clear();
            string path = TemplatePnlStatePath();
            try {
                lock (templatePnlLock) {
                    if (File.Exists(path)) {
                        foreach (string part in File.ReadAllText(path).Split(',')) {
                            int colon = part.IndexOf(':');
                            if (colon <= 0)
                                continue;
                            int templateNumber;
                            double dollars;
                            if (int.TryParse(part.Substring(0, colon).Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out templateNumber)
                                && double.TryParse(part.Substring(colon + 1).Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out dollars))
                                _templatePnl[templateNumber] = dollars;
                        }
                    }
                }
            }
            catch (Exception error) {
                D("Template P&L load failed: " + error.Message);
            }
        }

        // Re-reads the shared ledger before adding, so a concurrent sibling write isn't clobbered. Runs on
        // every closed trade in every mode -- cheap, and keeps the 4/5 rankings populated regardless of the
        // mode that was active when the P&L was earned.
        private void MarkTemplateRealized(int templateNumber, double dollars) {
            string path = TemplatePnlStatePath();
            try {
                lock (templatePnlLock) {
                    Dictionary<int, double> merged = new Dictionary<int, double>();
                    if (File.Exists(path)) {
                        foreach (string part in File.ReadAllText(path).Split(',')) {
                            int colon = part.IndexOf(':');
                            if (colon <= 0)
                                continue;
                            int existingTemplate;
                            double existingDollars;
                            if (int.TryParse(part.Substring(0, colon).Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out existingTemplate)
                                && double.TryParse(part.Substring(colon + 1).Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out existingDollars))
                                merged[existingTemplate] = existingDollars;
                        }
                    }

                    double running;
                    merged[templateNumber] = (merged.TryGetValue(templateNumber, out running) ? running : 0.0) + dollars;

                    var keys = new List<int>(merged.Keys);
                    keys.Sort();
                    var parts = new List<string>();
                    foreach (int key in keys)
                        parts.Add(key.ToString(CultureInfo.InvariantCulture) + ":" + merged[key].ToString("0.########", CultureInfo.InvariantCulture));
                    File.WriteAllText(path, string.Join(",", parts));

                    _templatePnl = merged;
                }
            }
            catch (Exception error) {
                D("Template P&L save failed: " + error.Message);
            }
        }

        // Modes 4/5 ranked traversal order over the active session/mode bounds. Mode 4 = only net-losing
        // templates, worst (most negative) first. Mode 5 = only net-winning templates, best (most positive)
        // first. Ties broken by template number for a stable order. Reloads the ledger from disk first.
        // Returns empty when no template qualifies (fresh ledger, or all P&L on the wrong side of zero).
        private List<int> BuildRankedTemplateList() {
            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            LoadTemplatePnl();

            bool winnersMode = TemplateMode == 5;
            var qualifying = new List<int>();
            for (int t = minTemplate; t <= maxTemplate; t++) {
                double dollars;
                if (!_templatePnl.TryGetValue(t, out dollars))
                    continue;
                if (winnersMode ? dollars > 0.0 : dollars < 0.0)
                    qualifying.Add(t);
            }

            qualifying.Sort((a, b) => {
                double da = _templatePnl[a];
                double db = _templatePnl[b];
                // Mode 5: descending dollars (biggest gain first). Mode 4: ascending dollars (biggest loss first).
                int cmp = winnersMode ? db.CompareTo(da) : da.CompareTo(db);
                return cmp != 0 ? cmp : a.CompareTo(b);
            });

            return qualifying;
        }

        // Next template in the ranked list after the current one, wrapping. Falls back to simple forward
        // numeric rotation when nothing qualifies, so a fresh ledger still trades and builds P&L history.
        private int GetNextRankedTemplate(int current) {
            List<int> ranked = BuildRankedTemplateList();
            if (ranked.Count == 0)
                return GetNextTemplateForward(current);

            int idx = ranked.IndexOf(current);
            if (idx < 0)
                return ranked[0];
            return ranked[(idx + 1) % ranked.Count];
        }

        private void LoadTemplateState() {
            int seededTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, TemplateNumber));

            if (TemplateMode == 2) {
                List<int> customList = GetCustomTemplateList();
                _activeTemplateNumber = customList.Contains(seededTemplate) ? seededTemplate : customList[0];
            }
            else {
                int loadMin, loadMax;
                GetActiveSessionTemplateBounds(out loadMin, out loadMax);
                _activeTemplateNumber = EnableSessionBasedTemplateRange
                    ? Math.Max(loadMin, Math.Min(loadMax, seededTemplate))
                    : Math.Max(1, Math.Min(Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, MaxTemplateNumber)), seededTemplate));
            }

            if (!IsRotatingTemplateMode()) {
                // Mode 0 (Manual/fixed) never rotates, so there is no saved rotation state to
                // restore -- but still write the file once so downstream tooling (8765 dashboard's
                // Active Templates card) can see this instance and its fixed template. Previously
                // mode 0 wrote no file at all here and was invisible there.
                SaveTemplateState();
                return;
            }

            string path = TemplateStatePath();
            try {
                lock (templateStateLock) {
                    if (File.Exists(path)) {
                        string[] parts = File.ReadAllText(path).Split('|');
                        if (parts.Length >= 2
                            && int.TryParse(parts[0], NumberStyles.Integer, CultureInfo.InvariantCulture, out int savedTemplate)
                            && int.TryParse(parts[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out int savedWins)) {
                            // parts[2] is the Template Number value active at last save ("source"); reseeds only when the field has since been edited.
                            bool sourceMatches = parts.Length >= 3
                                && int.TryParse(parts[2], NumberStyles.Integer, CultureInfo.InvariantCulture, out int savedSource)
                                && savedSource == TemplateNumber;

                            if (sourceMatches) {
                                int absoluteSavedTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, savedTemplate));
                                if (TemplateMode == 2) {
                                    List<int> customList = GetCustomTemplateList();
                                    _activeTemplateNumber = customList.Contains(absoluteSavedTemplate) ? absoluteSavedTemplate : customList[0];
                                }
                                else {
                                    int loadMin, loadMax;
                                    GetActiveSessionTemplateBounds(out loadMin, out loadMax);
                                    _activeTemplateNumber = EnableSessionBasedTemplateRange
                                        ? Math.Max(loadMin, Math.Min(loadMax, absoluteSavedTemplate))
                                        : Math.Max(1, Math.Min(Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, MaxTemplateNumber)), absoluteSavedTemplate));
                                }
                            }
                            // Template Number was edited by the user; keep the fresh seed from TemplateNumber above.
                        }
                    }
                }
            }
            catch (Exception error) {
                D("Template state load failed: " + error.Message);
            }
        }

        // The 8765 dashboard's Active Templates card treats the state file's mtime as "this
        // instance was alive this trading day". Rotation events refresh it naturally, but a
        // running instance can legitimately produce none for hours (position held, working
        // entry order, single qualifying template in modes 2/4/5, blocked windows), so
        // ProcessTemplateNoFillRotation also re-saves on this interval as a heartbeat.
        private static readonly TimeSpan TemplateStateHeartbeatInterval = TimeSpan.FromMinutes(30);
        private DateTime _lastTemplateStateSaveUtc = DateTime.MinValue;

        private void SaveTemplateState() {
            // Updated even if the write below fails: heartbeat pacing must not turn a persistent
            // disk error into a retry (and a D-print) on every single bar.
            _lastTemplateStateSaveUtc = DateTime.UtcNow;

            string path = TemplateStatePath();
            try {
                lock (templateStateLock) {
                    // Middle field used to be a legacy consecutive-wins slot always written as 0;
                    // repurposed to carry TemplateMode so downstream tooling (8765 dashboard) can
                    // tell a fixed-template (mode 0) instance apart from a rotating one. Encoded
                    // as -(mode+1) (mode 0 -> -1, mode 1 -> -2, ...) so it can never collide with
                    // the literal 0 in files written by older builds -- those are always rotating
                    // instances (old code never saved mode 0 at all) and the dashboard must not
                    // mistake them for fixed-mode ones. Safe reuse: LoadTemplateState only checks
                    // that this field parses as an int, never reads its value.
                    File.WriteAllText(path, _activeTemplateNumber + "|" + (-(TemplateMode + 1)) + "|" + TemplateNumber);
                }
            }
            catch (Exception error) {
                D("Template state save failed: " + error.Message);
            }
        }

        private static readonly TimeSpan NormalTemplateNoFillWindow = TimeSpan.FromMinutes(34.5);
        private static readonly TimeSpan WinningTemplateNoFillWindow = TimeSpan.FromMinutes(69.0);

        private void InitializeTemplateRotation() {
            MaxTemplateNumber = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, MaxTemplateNumber));
            LoadTemplateState();

            // Mode 3 resumes from the saved template like modes 1/2 (LoadTemplateState above);
            // jump-to-lowest-unused happens only on trade close, so a restart mid-rotation
            // doesn't reset back to template 1 when the usage record is still empty.

            // Modes 4/5 always start at the top of the realized-P&L ranking (worst loser / best winner),
            // ignoring the persisted state. An empty ledger (no history yet) keeps the loaded/seeded template.
            if (TemplateMode == 4 || TemplateMode == 5) {
                List<int> ranked = BuildRankedTemplateList();
                if (ranked.Count > 0)
                    _activeTemplateNumber = ranked[0];
            }

            ApplyTemplate(_activeTemplateNumber);
            _templateEligibleElapsed = TimeSpan.Zero;
            _templateLastEligibleClock = DateTime.MinValue;
            _activeTemplateWasWinner = false;
            _templateTimerInitialized = false;
            _templateStateLoaded = true;

            // Reset so the first SnapTemplateToSessionBandIfNeeded call this run treats the
            // just-applied band as the baseline instead of a "changed" band to snap away from.
            _lastLiveSessionMinTemplate = int.MinValue;
            _lastLiveSessionMaxTemplate = int.MinValue;
        }

        private void ArmTemplateNoFillTimer(bool winnerWindow) {
            _activeTemplateWasWinner = winnerWindow;
            _templateEligibleElapsed = TimeSpan.Zero;
            _templateLastEligibleClock = DateTime.MinValue;
            _templateTimerInitialized = true;
        }

        private bool TemplateRotationIsEligibleNow() {
            if (State != State.Realtime || EffectiveMarketPosition() != MarketPosition.Flat || HasWorkingEntryOrder())
                return false;

            // Same entry gate as live orders: no-trade/cutoff periods and the 2-3 PM maintenance closure don't consume timer time.
            return !HandleBlockedTimeWindow() && !dailyLossLimitHit;
        }

        private bool IsRegularSessionNowLocal() {
            if (!EnableSessionBasedTemplateRange)
                return false;

            int nowHHmmss = int.Parse(DateTime.Now.ToString("HHmmss", CultureInfo.InvariantCulture));
            int start = RegularSessionStartTimeLocal;
            int end = RegularSessionEndTimeLocal;

            if (start <= end)
                return nowHHmmss >= start && nowHHmmss < end;

            // Handles a range that wraps past midnight, just in case.
            return nowHHmmss >= start || nowHHmmss < end;
        }

        private void GetActiveSessionTemplateBounds(out int minTemplate, out int maxTemplate) {
            // Modes that ignore session rules entirely and rank/select over the full 1..MaxTemplateNumber range:
            // 0 (Manual, direct pick), 2 (Custom Range, own list), 3 (Unused Only), 4/5 (Losers/Winners ranked by realized P&L).
            if (!EnableSessionBasedTemplateRange || TemplateMode == 0 || TemplateMode == 2 || TemplateMode == 3 || TemplateMode == 4 || TemplateMode == 5) {
                minTemplate = 1;
                maxTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, MaxTemplateNumber));
                return;
            }

            if (IsRegularSessionNowLocal()) {
                minTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, RegularMinTemplateNumber));
                maxTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, RegularMaxTemplateNumber));
            }
            else {
                minTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, OvernightMinTemplateNumber));
                maxTemplate = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, OvernightMaxTemplateNumber));
            }

            if (maxTemplate < minTemplate)
                maxTemplate = minTemplate;
        }

        private void GetActiveSessionNoFillWindows(out TimeSpan normalWindow, out TimeSpan winnerWindow) {
            if (TemplateMode == 2 && UseCustomRotationTiming) {
                normalWindow = TimeSpan.FromMinutes(Math.Max(0.1, CustomNoFillWindowMinutes));
                winnerWindow = TimeSpan.FromMinutes(Math.Max(0.1, CustomWinnerWindowMinutes));
            }
            else if (EnableSessionBasedTemplateRange && IsRegularSessionNowLocal()) {
                normalWindow = RegularNormalNoFillWindow;
                winnerWindow = RegularWinnerNoFillWindow;
            }
            else if (EnableSessionBasedTemplateRange) {
                normalWindow = OvernightNormalNoFillWindow;
                winnerWindow = OvernightWinnerNoFillWindow;
            }
            else {
                normalWindow = NormalTemplateNoFillWindow;
                winnerWindow = WinningTemplateNoFillWindow;
            }
        }

        private List<int> ParseCustomTemplateRanges(string raw) {
            var result = new List<int>();
            if (string.IsNullOrWhiteSpace(raw))
                return result;

            foreach (string rawToken in raw.Split(',')) {
                string token = rawToken.Trim();
                if (token.Length == 0)
                    continue;

                int dash = token.IndexOf('-');
                if (dash > 0) {
                    string aStr = token.Substring(0, dash).Trim();
                    string bStr = token.Substring(dash + 1).Trim();
                    if (int.TryParse(aStr, NumberStyles.Integer, CultureInfo.InvariantCulture, out int a)
                        && int.TryParse(bStr, NumberStyles.Integer, CultureInfo.InvariantCulture, out int b)) {
                        a = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, a));
                        b = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, b));
                        if (a <= b) {
                            for (int v = a; v <= b; v++)
                                result.Add(v);
                        }
                        else {
                            for (int v = a; v >= b; v--)
                                result.Add(v);
                        }
                    }
                }
                else if (int.TryParse(token, NumberStyles.Integer, CultureInfo.InvariantCulture, out int single))
                    result.Add(Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, single)));
            }

            return result;
        }

        private List<int> GetCustomTemplateList() {
            if (_customTemplateList == null || _customTemplateListSource != CustomTemplateRanges) {
                _customTemplateList = ParseCustomTemplateRanges(CustomTemplateRanges);
                if (_customTemplateList.Count == 0)
                    _customTemplateList.Add(Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, TemplateNumber)));
                _customTemplateListSource = CustomTemplateRanges;
            }
            return _customTemplateList;
        }

        private int GetNextTemplateForward(int current) {
            if (TemplateMode == 2) {
                List<int> list = GetCustomTemplateList();
                int idx = list.IndexOf(current);
                if (idx < 0)
                    idx = 0;
                int nextIdx = idx >= list.Count - 1 ? 0 : idx + 1;
                return list[nextIdx];
            }

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);
            return current >= maxTemplate ? minTemplate : current + 1;
        }

        private int GetNextTemplateBackward(int current) {
            if (TemplateMode == 2) {
                List<int> list = GetCustomTemplateList();
                int idx = list.IndexOf(current);
                if (idx < 0)
                    idx = 0;
                int nextIdx = idx >= list.Count - 1 ? 0 : Math.Max(0, idx - 1);
                return list[nextIdx];
            }

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);
            return current == maxTemplate ? minTemplate : Math.Max(minTemplate, current - 1);
        }

        // Mode 3: always the lowest-numbered template without a completed trade, ignoring where rotation last left off. Reloads usage from disk first; falls back to round-robin if all templates have traded.
        private int GetNextUnusedTemplateForward(int current) {
            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            LoadTemplateUsage();

            for (int t = minTemplate; t <= maxTemplate; t++)
                if (!_usedTemplateNumbers.Contains(t))
                    return t;

            return GetNextTemplateForward(current);
        }

        private void ProcessTemplateNoFillRotation() {
            if (!IsRotatingTemplateMode() || !_templateStateLoaded)
                return;

            // Liveness heartbeat for the dashboard (see TemplateStateHeartbeatInterval). Runs
            // BEFORE the eligibility early-return below on purpose: an instance that is in a
            // position or working an order all day produces no rotation events, and without
            // this its state file would age out of the dashboard's same-trading-day filter
            // while the instance is very much alive.
            if (DateTime.UtcNow - _lastTemplateStateSaveUtc >= TemplateStateHeartbeatInterval)
                SaveTemplateState();

            DateTime now = CurrentClockTime();
            if (!TemplateRotationIsEligibleNow()) {
                // Re-anchor on every paused update so the first tick after reopening adds zero elapsed time.
                _templateLastEligibleClock = now;
                return;
            }

            SnapTemplateToSessionBandIfNeeded();

            if (!_templateTimerInitialized)
                ArmTemplateNoFillTimer(false);

            if (_templateLastEligibleClock == DateTime.MinValue) {
                _templateLastEligibleClock = now;
                return;
            }

            TimeSpan elapsed = now - _templateLastEligibleClock;
            _templateLastEligibleClock = now;
            if (elapsed > TimeSpan.Zero)
                _templateEligibleElapsed += elapsed;

            TimeSpan normalWindow, winnerWindow;
            GetActiveSessionNoFillWindows(out normalWindow, out winnerWindow);
            TimeSpan required = _activeTemplateWasWinner ? winnerWindow : normalWindow;
            if (_templateEligibleElapsed < required)
                return;

            // Mode 3: a no-fill timeout just steps forward to the next template number (wrapping), regardless of used/unused --
            // unused-only selection is reserved for OnTradeClosedForTemplateRotation, otherwise a template that never fills
            // would keep re-selecting itself forever since it's never marked used.
            // Modes 4/5: step to the next template in the realized-P&L ranking (wrapping), so a non-filling top-ranked
            // template can't pin the strategy -- the traversal walks the whole ranked list, not just re-picking rank 0.
            int nextTemplate = (TemplateMode == 4 || TemplateMode == 5)
                ? GetNextRankedTemplate(_activeTemplateNumber)
                : GetNextTemplateForward(_activeTemplateNumber);
            ApplyTemplate(nextTemplate);
            _activeTemplateSetByMl = false;
            ArmTemplateNoFillTimer(false);
            SaveTemplateState();
        }

        private void OnTradeClosedForTemplateRotation(bool isWin) {
            if (!IsRotatingTemplateMode() || !_templateStateLoaded)
                return;

            if (TemplateMode == 3) {
                // Any completed trade (win or loss) marks the template used and drops it from the unused-only pool.
                // ML-selected templates are exempt: rotation never chose them, so they stay in the untested pool.
                if (!_activeTemplateSetByMl)
                    MarkTemplateUsed(_activeTemplateNumber);
                int nextUnused = GetNextUnusedTemplateForward(_activeTemplateNumber);
                ApplyTemplate(nextUnused);
                _activeTemplateSetByMl = false;
                ArmTemplateNoFillTimer(false);
                SaveTemplateState();
                return;
            }

            if (TemplateMode == 4 || TemplateMode == 5) {
                // The just-closed trade's realized dollars were already folded into the ledger at the execution
                // site (MarkTemplateRealized), so BuildRankedTemplateList here reflects it. Advance to the next
                // template in the ranking; if the just-traded template's P&L flipped it out of the qualifying
                // set (e.g. a mode-4 loser turned net-positive), GetNextRankedTemplate restarts from rank 0.
                int nextRanked = GetNextRankedTemplate(_activeTemplateNumber);
                ApplyTemplate(nextRanked);
                _activeTemplateSetByMl = false;
                ArmTemplateNoFillTimer(false);
                SaveTemplateState();
                return;
            }

            if (isWin) {
                // A winner stays on its template but gets a double 69-minute fill window.
                ApplyTemplate(_activeTemplateNumber);
                ArmTemplateNoFillTimer(true);
            }
            else {
                // Losses step back; floor stays at floor, ceiling wraps to floor (per session range or custom list).
                int nextTemplate = GetNextTemplateBackward(_activeTemplateNumber);
                ApplyTemplate(nextTemplate);
                ArmTemplateNoFillTimer(false);
            }

            _activeTemplateSetByMl = false;
            SaveTemplateState();
        }


        private struct TemplateParams {
            public double MfiLongMax;
            public double MfiShortMin;
            public double RsiLongMax;
            public double RsiShortMin;
            public double StochLongMax;
            public double StochShortMin;
            public int PullbackTicks;
            public double ExitHoldThreshold;
            public int ExitMinBars;
            public double ExitMinR;
            public double Selectivity;
        }

        private static int BandIndexForSelectivity(double selectivity) {
            int best = 0;
            for (int i = 1; i < BandSelectivities.Length; i++) {
                if (Math.Abs(BandSelectivities[i] - selectivity) < Math.Abs(BandSelectivities[best] - selectivity))
                    best = i;
            }
            return best;
        }

        private static int DerivedTemaLength(double selectivity) {
            return (int)Math.Round(12.0 - selectivity * 7.0, MidpointRounding.AwayFromZero);
        }

        private static int DerivedBbLength(double selectivity) {
            return (int)Math.Round(30.0 - selectivity * 12.0, MidpointRounding.AwayFromZero);
        }

        private static double DerivedBbStdDev(double selectivity) {
            return Math.Round(2.20 - selectivity * 0.80, 2, MidpointRounding.AwayFromZero);
        }

        private static int DerivedMfiPeriod(double selectivity) {
            return (int)Math.Round(10.0 + selectivity * 10.0, MidpointRounding.AwayFromZero);
        }

        private static int DerivedStochRsiPeriod(double selectivity) {
            return (int)Math.Round(20.0 - selectivity * 10.0, MidpointRounding.AwayFromZero);
        }

        private static int DerivedMfiPriorBars(double selectivity) {
            return (int)Math.Round(selectivity * 3.0, MidpointRounding.AwayFromZero);
        }

        private static int DerivedStochRsiCrossLookbackBars(double selectivity) {
            return (int)Math.Round(selectivity * 3.0, MidpointRounding.AwayFromZero);
        }

        private static int DerivedEntryOrderExpireMinutes(double selectivity) {
            return (int)Math.Round(1.0 + selectivity * 4.0, MidpointRounding.AwayFromZero) + 1;
        }

        private static int DerivedReentryCooldownBars(double selectivity) {
            return (int)Math.Round(1.0 + selectivity * 4.0, MidpointRounding.AwayFromZero);
        }

        private const double UniversalBase = 3000.0;

        // Templates 1..Tier1MaxTemplate share one formula; Tier1MaxTemplate+1..AbsoluteMaxTemplateNumber ("Tier 2") uses a
        // second, instrument-specific formula. See TieredDollarValue.
        private const int Tier1MaxTemplate = 19;
        private const double EsTickDollars = 12.5;
        // ES's Tier 2 must move in whole ES ticks (see TieredDollarValue) -- fractional or zero values reintroduce duplicate
        // rounded values across adjacent templates. Whole numbers >= 1 only.
        private const double EsTier2TicksPerTemplate = 1.0;

        // === Entry gate auto-adjust (patched by auto_apply_sizing.py; evidence on the dashboard's Entry
        // Gate Reassess card) ===
        // A positive widen LOOSENS that gate for every template in its tier: MFI/RSI widen is added to
        // LongMax and subtracted from ShortMin (points), Stoch widen is added to StochLongMax and
        // subtracted from StochShortMin (0-1 units). Negative values tighten, but the automation never
        // drives a widen below 0 -- the static TemplateParamsTable stays the designed floor. Applied
        // centrally in GetTemplateParams so live entries, shadow evaluation, and the template reference
        // JSON export all see the same effective thresholds. Tier split matches Tier1MaxTemplate (19),
        // the same 1-19 / 20-40 split Risk1R sizing uses.
        private const double MfiGateWidenT1to19 = 15.0000;
        private const double MfiGateWidenT20to40 = 15.0000;
        private const double RsiGateWidenT1to19 = 15.0000;
        private const double RsiGateWidenT20to40 = 14.3400;
        private const double StochGateWidenT1to19 = 0.1500;
        private const double StochGateWidenT20to40 = 0.1500;
        // Added on top of DerivedEntryOrderExpireMinutes for the tier; effective value clamps to 1-30 min.
        private const int EntryExpireExtraMinutesT1to19 = 20;
        private const int EntryExpireExtraMinutesT20to40 = 13;
        // Slippage reserve: DailyEntrySlippageDollars = LadderRiskDollars1R * this ratio (was a
        // hardcoded 0.10). Evidence: TemaLimit_slippage_log.tsv -- realized stop-exit slippage vs
        // the reserve. Effective value clamps to 0.02-0.30 in ComputeSharedRiskAndSlippage.
        private const double SlippageReserveRatio = 0.0240;
        // ATR-bound pullback clamp band (AtrBoundPullbackTicks); defaults match the original
        // hardcoded 0.5/1.5. Only the FLOOR is auto-adjusted -- floor-bound no-fills measurably
        // show a tighter distance would have filled (raw ratio + missedByTicks in the no-fill
        // log). The ceiling stays manual: ceiling-bound entries fill MORE easily, their cost is
        // entry quality (MAE), which this evidence doesn't measure. Effective values clamp to
        // 0.20-1.00 (floor) and 1.00-2.50 (ceiling) in AtrBoundPullbackTicks.
        private const double AtrClampMin = 0.50;
        private const double AtrClampMax = 1.50;

        private static string NormalizeRiskTicker(string tickerName) {
            string normalized = string.IsNullOrWhiteSpace(tickerName) ? string.Empty : tickerName.Trim().ToUpperInvariant();
            return normalized == "NQ" || normalized == "ES" || normalized == "RTY" || normalized == "YM"
                ? normalized
                : "ES";
        }

        private static double TemplateMultiplier(int templateNumber) {
            int clamped = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, templateNumber));
            return 1.0 + 0.041667 * (clamped - 1);
        }

        private static double InstrumentMultiplier(string tickerName) {
            switch (NormalizeRiskTicker(tickerName)) {
                case "NQ":
                    return 0.166667;
                case "ES":
                    return 0.104167;
                case "RTY":
                    return 0.066667;
                case "YM":
                    return 0.033333;
                default:
                    return 0.104167;
            }
        }

        private static double LadderMultiplier(string tickerName) {
            switch (NormalizeRiskTicker(tickerName)) {
                case "NQ":
                    return 0.125;
                case "ES":
                    return 0.10;
                case "RTY":
                    return 0.05;
                case "YM":
                    return 0.021245;
                default:
                    return 0.10;
            }
        }

        // Tier 2 (templates Tier1MaxTemplate+1..40) endpoint for NQ/RTY/YM, which use a straight line from their
        // Tier 1 template-19 value up to this target. ES does not use this -- see TieredDollarValue.
        private static double Tier2Target(string tickerName, bool isLadderDaily) {
            switch (NormalizeRiskTicker(tickerName)) {
                case "NQ":
                    return isLadderDaily ? 800.0 : 877.06;
                case "RTY":
                    return isLadderDaily ? 375.0 : 500.0;
                case "YM":
                    return isLadderDaily ? 255.0 : 300.05;
                default:
                    return isLadderDaily ? 800.0 : 1000.0;
            }
        }

        private static double RoundDollarsToWholeTicks(double dollarValue, string tickerName) {
            double rounded = Math.Round(dollarValue, 1, MidpointRounding.AwayFromZero);
            string normalized = NormalizeRiskTicker(tickerName);
            if (normalized == "ES")
                return Math.Round(rounded / EsTickDollars, MidpointRounding.AwayFromZero) * EsTickDollars;
            return rounded;
        }

        // Templates 1..Tier1MaxTemplate: UniversalBase x TemplateMultiplier x perInstrumentMultiplier, same shape for
        // every instrument. Templates beyond that ("Tier 2"): NQ/RTY/YM continue on a straight line from their
        // template-19 value to Tier2Target; ES instead steps up by a fixed number of whole $12.5 ticks per template,
        // because ES's tick size is too coarse for a smooth line to stay free of duplicate rounded values over just
        // 21 templates.
        private static double TieredDollarValue(string tickerName, int templateNumber, double perInstrumentMultiplier, bool isLadderDaily) {
            string ticker = NormalizeRiskTicker(tickerName);
            int clamped = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, templateNumber));

            if (clamped <= Tier1MaxTemplate) {
                return RoundDollarsToWholeTicks(UniversalBase * TemplateMultiplier(clamped) * perInstrumentMultiplier, ticker);
            }

            if (ticker == "ES") {
                double tier1EndValue = RoundDollarsToWholeTicks(UniversalBase * TemplateMultiplier(Tier1MaxTemplate) * perInstrumentMultiplier, ticker);
                return tier1EndValue + (clamped - Tier1MaxTemplate) * EsTier2TicksPerTemplate * EsTickDollars;
            }

            double tier1EndRaw = UniversalBase * TemplateMultiplier(Tier1MaxTemplate) * perInstrumentMultiplier;
            double target = Tier2Target(ticker, isLadderDaily);
            double frac = (clamped - Tier1MaxTemplate) / (double)(AbsoluteMaxTemplateNumber - Tier1MaxTemplate);
            return RoundDollarsToWholeTicks(tier1EndRaw + (target - tier1EndRaw) * frac, ticker);
        }

        private static double TemplateRiskDollars1RForTicker(string tickerName, int templateNumber) {
            return TieredDollarValue(tickerName, templateNumber, InstrumentMultiplier(tickerName), false);
        }

        private double TemplateRiskDollars1R(int templateNumber) {
            string tickerName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : string.Empty;
            return TemplateRiskDollars1RForTicker(tickerName, templateNumber);
        }

        private static double DerivedMlMinConfidence(double selectivity) {
            return Math.Round(0.70 - selectivity * 0.20, 2, MidpointRounding.AwayFromZero);
        }

        // Single source of truth for LadderRiskDollars1R/DailyEntryRiskDollars and slippage; shared by live sizing and the reference export.
        private static void ComputeSharedRiskAndSlippage(string tickerName, int templateNumber, out double sharedRiskValue, out double dailyEntrySlippage) {
            sharedRiskValue = TieredDollarValue(tickerName, templateNumber, LadderMultiplier(tickerName), true);
            double slippageRatio = Math.Max(0.02, Math.Min(0.30, SlippageReserveRatio));
            dailyEntrySlippage = Math.Round(sharedRiskValue * slippageRatio, 1, MidpointRounding.AwayFromZero);
        }

        private void ApplyTemplate(int templateNumber) {
            int clamped;
            if (TemplateMode == 2) {
                // Custom Range mode restricts to its own template list; clamp only to absolute 1..AbsoluteMaxTemplateNumber bounds.
                clamped = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, templateNumber));
            }
            else {
                int minTemplate, maxTemplate;
                GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);
                clamped = Math.Max(minTemplate, Math.Min(maxTemplate, templateNumber));
            }

            TemplateParams p = GetTemplateParams(clamped);
            ApplyTemplateDerivedSettings(clamped, p);
        }

        // Template table: 1=strictest, 40=loosest. exitMinBars runs inverted (1=1 bar, 40=7 bars) by design. All tickers use this template's own base pullbackTicks.
        private static int PullbackTicksForTicker(string tickerName, int templateNumber) {
            int pullbackBase = GetTemplateParams(templateNumber).PullbackTicks;
            bool lowTier = templateNumber <= 17;

            // High-tier multipliers restored to the designed values on 2026-07-18: the ~0.012 values
            // they replaced were artifacts of the July-17 pre-cutoff auto-apply ratchet (1-tick live
            // pullbacks on T18-40). Evidence replay: 1-tick-era high-tier trades were net losers on
            // 3 of 4 tickers while designed-distance-era trades were positive on all 4 -- see
            // ML_SYSTEM_GUIDE.txt 2026-07-18. Protected by seeded pullback cutoffs in
            // auto_apply_history.json, so the automation only adjusts these on new-regime evidence.
            double pullbackMultiplier = tickerName == "ES" ? (lowTier ? 0.10 : 0.0123)
                : tickerName == "RTY" ? (lowTier ? 0.0196 : 0.0139)
                : tickerName == "YM" ? (lowTier ? 0.10 : 0.0154)
                : (lowTier ? 1.0000 : 0.0120); // NQ and any unrecognized ticker
            return Math.Max(1, (int)Math.Round(pullbackBase * pullbackMultiplier, MidpointRounding.AwayFromZero));
        }

        private const int AtrBoundAveragePeriod = 20;

        // ATR-bound pullback: starts from the static table value, flexes vs. the trailing 20-bar ATR average
        // inside the AtrClampMin..AtrClampMax band (auto-adjust constants, default 0.5-1.5); falls back to
        // the table value on any warmup/indexing edge case. Pure -- no field writes -- so the shadow sweep
        // and the per-tick dashboard exporter can call it without clobbering the _lastPullback* snapshot
        // that the template-apply path takes for the order actually placed.
        private int ComputeAtrBoundPullbackTicks(string tickerName, int templateNumber, out double atrValue, out double atrAverage, out double ratioRaw, out double ratioClamped) {
            int tableTicks = PullbackTicksForTicker(tickerName, templateNumber);
            atrValue = 0.0;
            atrAverage = 0.0;
            ratioRaw = 1.0;
            ratioClamped = 1.0;

            try {
                if (atr == null || atr.CurrentBar < AtrBoundAveragePeriod - 1)
                    return tableTicks;

                double sum = 0.0;
                for (int i = 0; i < AtrBoundAveragePeriod; i++)
                    sum += atr[i];
                double average = sum / AtrBoundAveragePeriod;

                if (average <= 0) {
                    atrValue = atr[0];
                    return tableTicks;
                }

                double clampMin = Math.Max(0.20, Math.Min(1.00, AtrClampMin));
                double clampMax = Math.Max(1.00, Math.Min(2.50, AtrClampMax));
                double ratio = atr[0] / average;
                atrValue = atr[0];
                atrAverage = average;
                ratioRaw = ratio;
                ratioClamped = Math.Max(clampMin, Math.Min(clampMax, ratio));
                return Math.Max(1, (int)Math.Round(tableTicks * ratioClamped, MidpointRounding.AwayFromZero));
            }
            catch (Exception error) {
                D("ComputeAtrBoundPullbackTicks failed, falling back to table value: " + error.Message);
                atrValue = 0.0;
                atrAverage = 0.0;
                ratioRaw = 1.0;
                ratioClamped = 1.0;
                return tableTicks;
            }
        }

        // Field-writing wrapper, called only from ApplyTemplateDerivedSettings: snapshots the ratio pair
        // actually baked into the PullbackTicks a live order will use, so the no-fill log's
        // atrRatioRaw/atrRatioClamped and the completed-trades pullbackAtr columns describe the placed
        // order. ExportPullbackState used to route through here every tick, which overwrote this snapshot
        // with cancel/exit-time values and broke the Clamp Band Reassess would-fill math
        // (pullbackTicks * raw/clamped only reconstructs the unclamped distance when the pair is the one
        // that produced those ticks).
        private int AtrBoundPullbackTicks(string tickerName, int templateNumber) {
            double atrValue, atrAverage, ratioRaw, ratioClamped;
            int ticks = ComputeAtrBoundPullbackTicks(tickerName, templateNumber, out atrValue, out atrAverage, out ratioRaw, out ratioClamped);
            _lastPullbackAtr = atrValue;
            _lastPullbackAtrAvg = atrAverage;
            _lastPullbackAtrRatio = ratioClamped;
            _lastPullbackAtrRatioRaw = ratioRaw;
            return ticks;
        }

        // Selectivity: 0.0 (loosest) to 1.0 (tightest), derived from MFI/Stoch RSI/Pullback extremity. Drives TemaLength, BBLength, MlMinConfidence, RiskDollars1R.
        private static readonly TemplateParams[] TemplateParamsTable = {
            new TemplateParams { MfiLongMax = 5, MfiShortMin = 95, RsiLongMax = 20, RsiShortMin = 80, StochLongMax = 0.100, StochShortMin = 0.900, PullbackTicks = 45, ExitHoldThreshold = 0.60, ExitMinBars = 1, ExitMinR = 0.75, Selectivity = 0.00 }, // was #20
            new TemplateParams { MfiLongMax = 6, MfiShortMin = 94, RsiLongMax = 21, RsiShortMin = 79, StochLongMax = 0.109, StochShortMin = 0.891, PullbackTicks = 46, ExitHoldThreshold = 0.58, ExitMinBars = 1, ExitMinR = 0.73, Selectivity = 0.10 }, // was #36
            new TemplateParams { MfiLongMax = 7, MfiShortMin = 93, RsiLongMax = 22, RsiShortMin = 78, StochLongMax = 0.118, StochShortMin = 0.882, PullbackTicks = 47, ExitHoldThreshold = 0.55, ExitMinBars = 1, ExitMinR = 0.71, Selectivity = 0.30 }, // was #11
            new TemplateParams { MfiLongMax = 8, MfiShortMin = 92, RsiLongMax = 23, RsiShortMin = 77, StochLongMax = 0.126, StochShortMin = 0.874, PullbackTicks = 48, ExitHoldThreshold = 0.53, ExitMinBars = 1, ExitMinR = 0.69, Selectivity = 0.30 }, // was #12
            new TemplateParams { MfiLongMax = 9, MfiShortMin = 91, RsiLongMax = 24, RsiShortMin = 76, StochLongMax = 0.135, StochShortMin = 0.865, PullbackTicks = 49, ExitHoldThreshold = 0.53, ExitMinBars = 1, ExitMinR = 0.67, Selectivity = 0.30 }, // was #15
            new TemplateParams { MfiLongMax = 10, MfiShortMin = 90, RsiLongMax = 25, RsiShortMin = 75, StochLongMax = 0.144, StochShortMin = 0.856, PullbackTicks = 50, ExitHoldThreshold = 0.53, ExitMinBars = 1, ExitMinR = 0.65, Selectivity = 0.30 }, // was #17
            new TemplateParams { MfiLongMax = 11, MfiShortMin = 89, RsiLongMax = 26, RsiShortMin = 74, StochLongMax = 0.153, StochShortMin = 0.847, PullbackTicks = 51, ExitHoldThreshold = 0.53, ExitMinBars = 2, ExitMinR = 0.63, Selectivity = 0.30 }, // was #19
            new TemplateParams { MfiLongMax = 12, MfiShortMin = 88, RsiLongMax = 27, RsiShortMin = 73, StochLongMax = 0.162, StochShortMin = 0.838, PullbackTicks = 52, ExitHoldThreshold = 0.53, ExitMinBars = 2, ExitMinR = 0.62, Selectivity = 0.30 }, // was #26
            new TemplateParams { MfiLongMax = 13, MfiShortMin = 87, RsiLongMax = 28, RsiShortMin = 72, StochLongMax = 0.171, StochShortMin = 0.829, PullbackTicks = 53, ExitHoldThreshold = 0.50, ExitMinBars = 2, ExitMinR = 0.60, Selectivity = 0.30 }, // was #29
            new TemplateParams { MfiLongMax = 14, MfiShortMin = 86, RsiLongMax = 29, RsiShortMin = 71, StochLongMax = 0.179, StochShortMin = 0.821, PullbackTicks = 54, ExitHoldThreshold = 0.50, ExitMinBars = 2, ExitMinR = 0.58, Selectivity = 0.30 }, // was #33
            new TemplateParams { MfiLongMax = 15, MfiShortMin = 85, RsiLongMax = 30, RsiShortMin = 70, StochLongMax = 0.188, StochShortMin = 0.812, PullbackTicks = 55, ExitHoldThreshold = 0.50, ExitMinBars = 2, ExitMinR = 0.56, Selectivity = 0.30 }, // was #38
            new TemplateParams { MfiLongMax = 16, MfiShortMin = 84, RsiLongMax = 31, RsiShortMin = 69, StochLongMax = 0.197, StochShortMin = 0.803, PullbackTicks = 56, ExitHoldThreshold = 0.50, ExitMinBars = 3, ExitMinR = 0.54, Selectivity = 0.40 }, // was #10
            new TemplateParams { MfiLongMax = 17, MfiShortMin = 83, RsiLongMax = 32, RsiShortMin = 68, StochLongMax = 0.206, StochShortMin = 0.794, PullbackTicks = 57, ExitHoldThreshold = 0.50, ExitMinBars = 3, ExitMinR = 0.52, Selectivity = 0.40 }, // was #14
            new TemplateParams { MfiLongMax = 18, MfiShortMin = 82, RsiLongMax = 33, RsiShortMin = 67, StochLongMax = 0.215, StochShortMin = 0.785, PullbackTicks = 58, ExitHoldThreshold = 0.50, ExitMinBars = 3, ExitMinR = 0.50, Selectivity = 0.40 }, // was #18
            new TemplateParams { MfiLongMax = 19, MfiShortMin = 81, RsiLongMax = 34, RsiShortMin = 66, StochLongMax = 0.224, StochShortMin = 0.776, PullbackTicks = 59, ExitHoldThreshold = 0.50, ExitMinBars = 3, ExitMinR = 0.48, Selectivity = 0.40 }, // was #21
            new TemplateParams { MfiLongMax = 20, MfiShortMin = 80, RsiLongMax = 35, RsiShortMin = 65, StochLongMax = 0.232, StochShortMin = 0.768, PullbackTicks = 60, ExitHoldThreshold = 0.50, ExitMinBars = 3, ExitMinR = 0.46, Selectivity = 0.40 }, // was #39
            new TemplateParams { MfiLongMax = 21, MfiShortMin = 79, RsiLongMax = 36, RsiShortMin = 64, StochLongMax = 0.241, StochShortMin = 0.759, PullbackTicks = 61, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.44, Selectivity = 0.40 }, // was #40
            new TemplateParams { MfiLongMax = 22, MfiShortMin = 78, RsiLongMax = 37, RsiShortMin = 63, StochLongMax = 0.250, StochShortMin = 0.750, PullbackTicks = 62, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.42, Selectivity = 0.50 }, // was #3
            new TemplateParams { MfiLongMax = 23, MfiShortMin = 77, RsiLongMax = 38, RsiShortMin = 62, StochLongMax = 0.259, StochShortMin = 0.741, PullbackTicks = 63, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.40, Selectivity = 0.50 }, // was #6
            new TemplateParams { MfiLongMax = 24, MfiShortMin = 76, RsiLongMax = 39, RsiShortMin = 61, StochLongMax = 0.268, StochShortMin = 0.732, PullbackTicks = 64, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.38, Selectivity = 0.50 }, // was #13
            new TemplateParams { MfiLongMax = 25, MfiShortMin = 75, RsiLongMax = 40, RsiShortMin = 60, StochLongMax = 0.276, StochShortMin = 0.724, PullbackTicks = 65, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.37, Selectivity = 0.50 }, // was #22
            new TemplateParams { MfiLongMax = 26, MfiShortMin = 74, RsiLongMax = 41, RsiShortMin = 59, StochLongMax = 0.285, StochShortMin = 0.715, PullbackTicks = 66, ExitHoldThreshold = 0.48, ExitMinBars = 4, ExitMinR = 0.35, Selectivity = 0.50 }, // was #25
            new TemplateParams { MfiLongMax = 27, MfiShortMin = 73, RsiLongMax = 42, RsiShortMin = 58, StochLongMax = 0.294, StochShortMin = 0.706, PullbackTicks = 67, ExitHoldThreshold = 0.48, ExitMinBars = 5, ExitMinR = 0.33, Selectivity = 0.50 }, // was #30
            new TemplateParams { MfiLongMax = 28, MfiShortMin = 72, RsiLongMax = 43, RsiShortMin = 57, StochLongMax = 0.303, StochShortMin = 0.697, PullbackTicks = 68, ExitHoldThreshold = 0.45, ExitMinBars = 5, ExitMinR = 0.31, Selectivity = 0.50 }, // was #37
            new TemplateParams { MfiLongMax = 29, MfiShortMin = 71, RsiLongMax = 44, RsiShortMin = 56, StochLongMax = 0.312, StochShortMin = 0.688, PullbackTicks = 69, ExitHoldThreshold = 0.45, ExitMinBars = 5, ExitMinR = 0.29, Selectivity = 0.60 }, // was #5
            new TemplateParams { MfiLongMax = 30, MfiShortMin = 70, RsiLongMax = 45, RsiShortMin = 55, StochLongMax = 0.321, StochShortMin = 0.679, PullbackTicks = 70, ExitHoldThreshold = 0.45, ExitMinBars = 5, ExitMinR = 0.27, Selectivity = 0.60 }, // was #8
            new TemplateParams { MfiLongMax = 31, MfiShortMin = 69, RsiLongMax = 46, RsiShortMin = 54, StochLongMax = 0.329, StochShortMin = 0.671, PullbackTicks = 71, ExitHoldThreshold = 0.45, ExitMinBars = 5, ExitMinR = 0.25, Selectivity = 0.60 }, // was #9
            new TemplateParams { MfiLongMax = 32, MfiShortMin = 68, RsiLongMax = 47, RsiShortMin = 53, StochLongMax = 0.338, StochShortMin = 0.662, PullbackTicks = 72, ExitHoldThreshold = 0.45, ExitMinBars = 5, ExitMinR = 0.23, Selectivity = 0.60 }, // was #16
            new TemplateParams { MfiLongMax = 33, MfiShortMin = 67, RsiLongMax = 48, RsiShortMin = 52, StochLongMax = 0.347, StochShortMin = 0.653, PullbackTicks = 73, ExitHoldThreshold = 0.45, ExitMinBars = 6, ExitMinR = 0.21, Selectivity = 0.60 }, // was #24
            new TemplateParams { MfiLongMax = 34, MfiShortMin = 66, RsiLongMax = 49, RsiShortMin = 51, StochLongMax = 0.356, StochShortMin = 0.644, PullbackTicks = 74, ExitHoldThreshold = 0.43, ExitMinBars = 6, ExitMinR = 0.19, Selectivity = 0.60 }, // was #27
            new TemplateParams { MfiLongMax = 35, MfiShortMin = 65, RsiLongMax = 50, RsiShortMin = 50, StochLongMax = 0.365, StochShortMin = 0.635, PullbackTicks = 75, ExitHoldThreshold = 0.43, ExitMinBars = 6, ExitMinR = 0.17, Selectivity = 0.60 }, // was #32
            new TemplateParams { MfiLongMax = 36, MfiShortMin = 64, RsiLongMax = 51, RsiShortMin = 49, StochLongMax = 0.374, StochShortMin = 0.626, PullbackTicks = 76, ExitHoldThreshold = 0.43, ExitMinBars = 6, ExitMinR = 0.15, Selectivity = 0.60 }, // was #34
            new TemplateParams { MfiLongMax = 37, MfiShortMin = 63, RsiLongMax = 52, RsiShortMin = 48, StochLongMax = 0.382, StochShortMin = 0.618, PullbackTicks = 77, ExitHoldThreshold = 0.43, ExitMinBars = 6, ExitMinR = 0.13, Selectivity = 0.70 }, // was #2
            new TemplateParams { MfiLongMax = 38, MfiShortMin = 62, RsiLongMax = 53, RsiShortMin = 47, StochLongMax = 0.391, StochShortMin = 0.609, PullbackTicks = 78, ExitHoldThreshold = 0.43, ExitMinBars = 6, ExitMinR = 0.12, Selectivity = 0.70 }, // was #4
            new TemplateParams { MfiLongMax = 39, MfiShortMin = 61, RsiLongMax = 54, RsiShortMin = 46, StochLongMax = 0.400, StochShortMin = 0.600, PullbackTicks = 79, ExitHoldThreshold = 0.43, ExitMinBars = 7, ExitMinR = 0.10, Selectivity = 0.70 }, // was #28
            new TemplateParams { MfiLongMax = 40, MfiShortMin = 60, RsiLongMax = 55, RsiShortMin = 45, StochLongMax = 0.409, StochShortMin = 0.591, PullbackTicks = 80, ExitHoldThreshold = 0.43, ExitMinBars = 7, ExitMinR = 0.08, Selectivity = 0.70 }, // was #31
            new TemplateParams { MfiLongMax = 41, MfiShortMin = 59, RsiLongMax = 56, RsiShortMin = 44, StochLongMax = 0.418, StochShortMin = 0.582, PullbackTicks = 81, ExitHoldThreshold = 0.43, ExitMinBars = 7, ExitMinR = 0.06, Selectivity = 0.70 }, // was #35
            new TemplateParams { MfiLongMax = 42, MfiShortMin = 58, RsiLongMax = 57, RsiShortMin = 43, StochLongMax = 0.426, StochShortMin = 0.574, PullbackTicks = 82, ExitHoldThreshold = 0.43, ExitMinBars = 7, ExitMinR = 0.04, Selectivity = 0.80 }, // was #7
            new TemplateParams { MfiLongMax = 43, MfiShortMin = 57, RsiLongMax = 58, RsiShortMin = 42, StochLongMax = 0.435, StochShortMin = 0.565, PullbackTicks = 83, ExitHoldThreshold = 0.38, ExitMinBars = 7, ExitMinR = 0.02, Selectivity = 0.90 }, // was #1
            new TemplateParams { MfiLongMax = 44, MfiShortMin = 56, RsiLongMax = 59, RsiShortMin = 41, StochLongMax = 0.450, StochShortMin = 0.550, PullbackTicks = 84, ExitHoldThreshold = 0.35, ExitMinBars = 7, ExitMinR = 0.00, Selectivity = 1.00 }, // was #23
        };

        private static TemplateParams GetTemplateParams(int templateNumber) {
            int clamped = Math.Max(1, Math.Min(AbsoluteMaxTemplateNumber, templateNumber));
            TemplateParams p = TemplateParamsTable[clamped - 1]; // struct copy; the table itself is never mutated
            bool tier1 = clamped <= Tier1MaxTemplate;
            double mfiWiden = tier1 ? MfiGateWidenT1to19 : MfiGateWidenT20to40;
            double rsiWiden = tier1 ? RsiGateWidenT1to19 : RsiGateWidenT20to40;
            double stochWiden = tier1 ? StochGateWidenT1to19 : StochGateWidenT20to40;
            // Clamps keep long/short thresholds from crossing the 50 / 0.50 midline (MFI, Stoch) and inside
            // sane indicator bounds (RSI long/short overlap past 50 by design in loose templates, so RSI
            // only gets range clamps).
            p.MfiLongMax = Math.Max(1.0, Math.Min(49.0, p.MfiLongMax + mfiWiden));
            p.MfiShortMin = Math.Min(99.0, Math.Max(51.0, p.MfiShortMin - mfiWiden));
            p.RsiLongMax = Math.Max(1.0, Math.Min(99.0, p.RsiLongMax + rsiWiden));
            p.RsiShortMin = Math.Max(1.0, Math.Min(99.0, p.RsiShortMin - rsiWiden));
            p.StochLongMax = Math.Max(0.01, Math.Min(0.49, p.StochLongMax + stochWiden));
            p.StochShortMin = Math.Min(0.99, Math.Max(0.51, p.StochShortMin - stochWiden));
            return p;
        }

        private static int EffectiveEntryOrderExpireMinutes(double selectivity, int templateNumber) {
            int extra = templateNumber <= Tier1MaxTemplate ? EntryExpireExtraMinutesT1to19 : EntryExpireExtraMinutesT20to40;
            return Math.Max(1, Math.Min(30, DerivedEntryOrderExpireMinutes(selectivity) + extra));
        }

        private void ApplyTemplateDerivedSettings(int clamped, TemplateParams p) {
            double mfiLongMax = p.MfiLongMax, mfiShortMin = p.MfiShortMin;
            double rsiLongMax = p.RsiLongMax, rsiShortMin = p.RsiShortMin;
            double stochLongMax = p.StochLongMax, stochShortMin = p.StochShortMin;
            double exitHoldThreshold = p.ExitHoldThreshold, exitMinR = p.ExitMinR;
            int exitMinBars = p.ExitMinBars;
            double selectivity = p.Selectivity;

            // TemaLength/BBLength: inverted vs. selectivity (12-5 / 30-18) to balance trigger rate across templates.
            int temaLength = DerivedTemaLength(selectivity);
            int bbLength = DerivedBbLength(selectivity);

            // MlMinConfidence: inverted (0.70-0.50); loose-filter templates lean on ML confidence to compensate.
            double mlMinConfidence = DerivedMlMinConfidence(selectivity);

            // MfiPeriod: scales with selectivity (10-20); a smoothness knob, not a trigger-frequency multiplier.
            int mfiPeriod = DerivedMfiPeriod(selectivity);

            // MfiPriorBars: 0-3, scales with selectivity; tighter templates tolerate a one-bar overshoot further back.
            int mfiPriorBars = DerivedMfiPriorBars(selectivity);

            // StochRsiPeriod/BBStdDev: inverted (20-10 / 2.20-1.40) to avoid all three maxing out together.
            int stochRsiPeriod = DerivedStochRsiPeriod(selectivity);
            double bbStdDev = DerivedBbStdDev(selectivity);

            // EntryOrderExpireMinutes/ReentryCooldownBars: 1-5, scale with selectivity; tighter templates wait/cool down longer.
            // Effective value includes the per-tier auto-adjust extra (EntryExpireExtraMinutesT1to19/T20to40).
            int entryOrderExpireMinutes = EffectiveEntryOrderExpireMinutes(selectivity, clamped);
            int reentryCooldownBars = DerivedReentryCooldownBars(selectivity);

            // StochRsiCrossLookbackBars: 0-3, scales with selectivity like the expire/cooldown settings above.
            int stochRsiCrossLookbackBars = DerivedStochRsiCrossLookbackBars(selectivity);

            // RiskDollars1R: computed from UniversalBase, TemplateMultiplier, InstrumentMultiplier, and tick-aware rounding. Unknown symbols fall back to ES-like behavior.
            string tickerName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : string.Empty;
            double riskDollars1R = TemplateRiskDollars1R(clamped);

            // LadderRiskDollars1R/DailyEntryRiskDollars share one computed value from UniversalBase, TemplateMultiplier, LadderMultiplier, and tick-aware rounding; slippage is 10% of it.
            double sharedRiskValue, dailyEntrySlippage;
            ComputeSharedRiskAndSlippage(tickerName, clamped, out sharedRiskValue, out dailyEntrySlippage);

            bool changed = _activeTemplateNumber != clamped || !_templateStateLoaded;

            MfiLongMax = mfiLongMax;
            MfiShortMin = mfiShortMin;
            RsiLongMax = rsiLongMax;
            RsiShortMin = rsiShortMin;
            StochRsiLowerLine = stochLongMax;
            StochRsiUpperLine = stochShortMin;
            PullbackTicks = AtrBoundPullbackTicks(tickerName, clamped);
            MlExitHoldThreshold = exitHoldThreshold;
            MinBarsBeforeMlExit = exitMinBars;
            MinUnrealizedRForMlExit = exitMinR;
            TemaLength = temaLength;
            BBLength = bbLength;
            MlMinConfidence = mlMinConfidence;
            RiskDollars1R = riskDollars1R;
            LadderRiskDollars1R = sharedRiskValue;
            DailyEntryRiskDollars = sharedRiskValue;
            DailyEntrySlippageDollars = dailyEntrySlippage;
            Contracts = 1;
            MfiPeriod = mfiPeriod;
            MfiPriorBars = mfiPriorBars;
            StochRsiPeriod = stochRsiPeriod;
            BBStdDev = bbStdDev;
            EntryOrderExpireMinutes = entryOrderExpireMinutes;
            ReentryCooldownBars = reentryCooldownBars;
            StochRsiCrossLookbackBars = stochRsiCrossLookbackBars;
            EnableMfiFilter = true;
            EnableRsiFilter = true;
            EnableTemaVwapMidBbCrossEntry = true;
            EnableStochRsiCrossFilter = true;

            _activeTemplateNumber = clamped;
            // TemplateNumber input is not overwritten here; SaveTemplateState uses it as the "source" value to detect manual edits.

            // Point the live entry logic at this template's band indicator set so mid-session rotation actually changes indicator periods.
            SelectBandIndicators(BandIndexForSelectivity(selectivity));

            if (PrintTemplateChanges && changed) {
                PrintTo savedPrintTo = PrintTo;
                try {
                    PrintTo = PrintTo.OutputTab2;
                    Print(OutputTimePrefix() + OutputContext() + " Template " + clamped + " applied: "
                        + "MfiLongMax=" + mfiLongMax + " MfiShortMin=" + mfiShortMin
                        + " RsiLongMax=" + rsiLongMax + " RsiShortMin=" + rsiShortMin
                        + " StochLongMax=" + stochLongMax + " StochShortMin=" + stochShortMin
                        + " PullbackTicks=" + PullbackTicks
                        + " | ExitHoldThreshold=" + exitHoldThreshold
                        + " ExitMinBars=" + exitMinBars
                        + " ExitMinR=" + exitMinR
                        + " | TemaLength=" + temaLength
                        + " BBLength=" + bbLength
                        + " MlMinConfidence=" + mlMinConfidence
                        + " RiskDollars1R=" + riskDollars1R + " (" + tickerName + ")"
                        + " Contracts=1"
                        + " | MfiPeriod=" + mfiPeriod
                        + " MfiPriorBars=" + mfiPriorBars
                        + " StochRsiPeriod=" + stochRsiPeriod
                        + " BBStdDev=" + bbStdDev
                        + " | LadderRiskDollars1R=" + sharedRiskValue
                        + " DailyEntryRiskDollars=" + sharedRiskValue
                        + " DailyEntrySlippageDollars=" + dailyEntrySlippage
                        + " | EntryOrderExpireMinutes=" + entryOrderExpireMinutes
                        + " ReentryCooldownBars=" + reentryCooldownBars
                        + " StochRsiCrossLookbackBars=" + stochRsiCrossLookbackBars);
                }
                finally {
                    // Restore even on a throw -- otherwise every later Print in the process is stranded on Tab2.
                    PrintTo = savedPrintTo;
                }
            }
        }

        // Confirms a fresh compile actually took effect (e.g. after auto_apply_sizing.py edits temalimit.cs
        // and NinjaScript Editor autocompiles) without needing to check auto_apply_sizing.log. Runs once per process.
        private static bool _compileNotificationPrinted;

        private void PrintCompileNotificationIfNeeded() {
            if (_compileNotificationPrinted)
                return;

            PrintTo savedPrintTo = PrintTo;
            try {
                PrintTo = PrintTo.OutputTab2;
                Print(OutputTimePrefix() + "temalimit.cs compiled/reloaded successfully.");

                // Whatever last changed temalimit.cs (auto_apply_sizing.py, or a manual/Claude edit) can drop
                // a plain-text summary here; this reads+prints it once and deletes it, so the banner only
                // shows real changes, never stale info from a prior compile.
                string changesPath = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "temalimit_last_auto_apply.txt");
                try {
                    if (File.Exists(changesPath)) {
                        foreach (string line in File.ReadAllLines(changesPath)) {
                            if (!string.IsNullOrWhiteSpace(line))
                                Print(OutputTimePrefix() + line);
                        }
                        File.Delete(changesPath);
                    }
                }
                catch (IOException) {
                    // File locked by the Python writer mid-update; just skip this cycle, nothing left stale to clean up.
                    // Deliberately still latches below: this path is "skip", not "failed", and retrying would
                    // reprint the banner.
                }

                // Latch only after the banner actually printed, matching ExportTemplateReferenceIfNeeded: a
                // throw in here (as the State.DataLoaded Time[0] bug used to cause) leaves the guard clear so
                // the next State.DataLoaded retries instead of silently losing the banner for the process.
                _compileNotificationPrinted = true;
            }
            finally {
                // Restore even on a throw -- otherwise every later Print in the process is stranded on Tab2.
                PrintTo = savedPrintTo;
            }
        }

        // Dumps every template's computed fields to JSON for the ML dashboard's Template Reference table. Runs once per process, on first State.DataLoaded.
        private static bool _templateReferenceExported;
        private static readonly string[] TemplateReferenceTickers = { "ES", "NQ", "YM", "RTY" };

        private void ExportTemplateReferenceIfNeeded() {
            if (_templateReferenceExported)
                return;

            // Latch the guard only on success, so a transient failure lets the next State.DataLoaded retry.
            try {
                StringBuilder sb = new StringBuilder();
                sb.Append("{\"generatedAtUtc\":\"").Append(DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)).Append("\",");
                sb.Append("\"tickers\":[\"ES\",\"NQ\",\"YM\",\"RTY\"],");
                sb.Append("\"templates\":[");

                for (int t = FirstTemplateNumber; t <= AbsoluteMaxTemplateNumber; t++) {
                    TemplateParams p = GetTemplateParams(t);
                    double selectivity = p.Selectivity;

                    if (t > 1)
                        sb.Append(",");

                    sb.Append("{");
                    sb.Append("\"template\":").Append(t).Append(",");
                    sb.Append("\"selectivity\":").Append(selectivity.ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"mfiLongMax\":").Append(p.MfiLongMax.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"mfiShortMin\":").Append(p.MfiShortMin.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"rsiLongMax\":").Append(p.RsiLongMax.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"rsiShortMin\":").Append(p.RsiShortMin.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"stochLongMax\":").Append(p.StochLongMax.ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"stochShortMin\":").Append(p.StochShortMin.ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"pullbackTicks\":").Append(p.PullbackTicks).Append(",");
                    sb.Append("\"pullbackTicksByTicker\":{");
                    for (int i = 0; i < TemplateReferenceTickers.Length; i++) {
                        if (i > 0)
                            sb.Append(",");
                        string ticker = TemplateReferenceTickers[i];
                        sb.Append("\"").Append(ticker).Append("\":").Append(PullbackTicksForTicker(ticker, t));
                    }
                    sb.Append("},");
                    sb.Append("\"exitHoldThreshold\":").Append(p.ExitHoldThreshold.ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"exitMinBars\":").Append(p.ExitMinBars).Append(",");
                    sb.Append("\"exitMinR\":").Append(p.ExitMinR.ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"temaLength\":").Append(DerivedTemaLength(selectivity)).Append(",");
                    sb.Append("\"bbLength\":").Append(DerivedBbLength(selectivity)).Append(",");
                    sb.Append("\"bbStdDev\":").Append(DerivedBbStdDev(selectivity).ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"mfiPeriod\":").Append(DerivedMfiPeriod(selectivity)).Append(",");
                    sb.Append("\"stochRsiPeriod\":").Append(DerivedStochRsiPeriod(selectivity)).Append(",");
                    sb.Append("\"mfiPriorBars\":").Append(DerivedMfiPriorBars(selectivity)).Append(",");
                    sb.Append("\"stochRsiCrossLookbackBars\":").Append(DerivedStochRsiCrossLookbackBars(selectivity)).Append(",");
                    sb.Append("\"entryOrderExpireMinutes\":").Append(EffectiveEntryOrderExpireMinutes(selectivity, t)).Append(",");
                    sb.Append("\"reentryCooldownBars\":").Append(DerivedReentryCooldownBars(selectivity)).Append(",");
                    sb.Append("\"mlMinConfidence\":").Append(DerivedMlMinConfidence(selectivity).ToString("0.00", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"risk\":{");

                    for (int i = 0; i < TemplateReferenceTickers.Length; i++) {
                        string ticker = TemplateReferenceTickers[i];
                        double risk1R = TemplateRiskDollars1RForTicker(ticker, t);
                        double sharedRiskValue, dailyEntrySlippage;
                        ComputeSharedRiskAndSlippage(ticker, t, out sharedRiskValue, out dailyEntrySlippage);

                        if (i > 0)
                            sb.Append(",");
                        sb.Append("\"").Append(ticker).Append("\":{");
                        sb.Append("\"risk1R\":").Append(risk1R.ToString("0.0", CultureInfo.InvariantCulture)).Append(",");
                        sb.Append("\"ladderDaily\":").Append(sharedRiskValue.ToString("0.0", CultureInfo.InvariantCulture)).Append(",");
                        sb.Append("\"slippage\":").Append(dailyEntrySlippage.ToString("0.0", CultureInfo.InvariantCulture));
                        sb.Append("}");
                    }

                    sb.Append("}}");
                }

                sb.Append("]}");

                string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "temalimit_template_reference.json");
                File.WriteAllText(path, sb.ToString());
                _templateReferenceExported = true;
            }
            catch (Exception error) {
                D("Template reference export failed: " + error.Message);
            }
        }

        private void BuildBandIndicators() {
            int bands = BandSelectivities.Length;
            bandTema = new TEMA[bands];
            bandBb = new Bollinger[bands];
            bandMfi = new MFI[bands];
            bandStochRsi = new NinjaTrader.NinjaScript.Indicators.StochRSI[bands];

            for (int i = 0; i < bands; i++) {
                double s = BandSelectivities[i];
                bandTema[i] = TEMA(DerivedTemaLength(s));
                bandBb[i] = Bollinger(DerivedBbStdDev(s), DerivedBbLength(s));
                bandMfi[i] = MFI(DerivedMfiPeriod(s));
                bandStochRsi[i] = StochRSI(DerivedStochRsiPeriod(s));
            }

            _shadowTrades = new ShadowTrade[TemplateSlotCount];
            _shadowCooldownUntilBar = new int[TemplateSlotCount];
            for (int t = 0; t < _shadowCooldownUntilBar.Length; t++)
                _shadowCooldownUntilBar[t] = int.MinValue;
            _lastShadowSessionMinTemplate = int.MinValue;
            _lastShadowSessionMaxTemplate = int.MinValue;
        }

        private void SelectBandIndicators(int bandIndex) {
            if (bandTema == null)
                return;

            int clamped = Math.Max(0, Math.Min(BandSelectivities.Length - 1, bandIndex));
            temaIndicator = bandTema[clamped];
            bb = bandBb[clamped];
            mfi = bandMfi[clamped];
            stochRsi = bandStochRsi[clamped];
        }

        // Per-bar shadow sweep: each template paper-trades against its own band's indicators using completed bars, with pessimistic fill/exit rules. Only the active template places real orders.
        private void ProcessShadowEvaluation() {
            if (_shadowTrades == null || bandTema == null || CurrentContextBar < BarsRequiredToTrade + 2)
                return;

            if (_lastShadowProcessedBar == CurrentContextBar)
                return;
            _lastShadowProcessedBar = CurrentContextBar;

            UpdateShadowFillsAndExits();
            TryOpenShadowTrades();
        }

        private void ClearOutOfBandShadowTradesIfNeeded() {
            if (_shadowTrades == null || _shadowCooldownUntilBar == null)
                return;

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            if (_lastShadowSessionMinTemplate == minTemplate && _lastShadowSessionMaxTemplate == maxTemplate)
                return;

            bool hadPreviousRange = _lastShadowSessionMinTemplate != int.MinValue && _lastShadowSessionMaxTemplate != int.MinValue;
            int clearedTrades = 0;
            var clearedCooldowns = 0;

            for (int t = FirstTemplateNumber; t <= AbsoluteMaxTemplateNumber; t++) {
                bool inActiveRange = t >= minTemplate && t <= maxTemplate;
                if (inActiveRange)
                    continue;

                if (_shadowTrades[t] != null) {
                    _shadowTrades[t] = null;
                    clearedTrades++;
                }

                if (_shadowCooldownUntilBar[t] != int.MinValue) {
                    _shadowCooldownUntilBar[t] = int.MinValue;
                    clearedCooldowns++;
                }
            }

            if (hadPreviousRange && (clearedTrades > 0 || clearedCooldowns > 0)) {
                D("Shadow session range changed: old=" + _lastShadowSessionMinTemplate + "-" + _lastShadowSessionMaxTemplate
                    + " new=" + minTemplate + "-" + maxTemplate
                    + " clearedTrades=" + clearedTrades
                    + " clearedCooldowns=" + clearedCooldowns);
            }

            _lastShadowSessionMinTemplate = minTemplate;
            _lastShadowSessionMaxTemplate = maxTemplate;
        }

        // Live-rotation sibling of ClearOutOfBandShadowTradesIfNeeded: snaps the active template into
        // the current regular/overnight band the moment the boundary is crossed, instead of leaving it
        // outside the new band until the next no-fill timeout or trade close happens to re-clamp it.
        // Only runs when the caller (ProcessTemplateNoFillRotation) has already confirmed we're flat
        // with no working order -- ApplyTemplate must never fire against an open position.
        private void SnapTemplateToSessionBandIfNeeded() {
            if (!EnableSessionBasedTemplateRange || TemplateMode == 0 || TemplateMode == 2 || TemplateMode == 3 || TemplateMode == 4 || TemplateMode == 5)
                return;

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            if (_lastLiveSessionMinTemplate == minTemplate && _lastLiveSessionMaxTemplate == maxTemplate)
                return;

            _lastLiveSessionMinTemplate = minTemplate;
            _lastLiveSessionMaxTemplate = maxTemplate;

            // Usually a no-op on the first observation this run, since InitializeTemplateRotation
            // already applied the band active at that instant -- but the session boundary can fall
            // in the gap between that call and this one (e.g. strategy restarts right at 9:30), so
            // the range check below still runs even on the first observation rather than assuming init covered it.
            if (_activeTemplateNumber >= minTemplate && _activeTemplateNumber <= maxTemplate)
                return;

            int snapped = Math.Max(minTemplate, Math.Min(maxTemplate, _activeTemplateNumber));
            ApplyTemplate(snapped);
            ArmTemplateNoFillTimer(false);
            SaveTemplateState();
            if (PrintTemplateChanges)
                Print(OutputTimePrefix() + OutputContext() + " Session band changed: snapped template to " + snapped + " (band " + minTemplate + "-" + maxTemplate + ")");
        }

        private void UpdateShadowFillsAndExits() {
            double through = Math.Max(0, ShadowFillThroughTicks) * TickSize;

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            for (int t = minTemplate; t <= maxTemplate; t++) {
                ShadowTrade trade = _shadowTrades[t];
                if (trade == null)
                    continue;

                if (!trade.Filled) {
                    TemplateParams p = GetTemplateParams(t);
                    // Wall-clock check, matching CancelExpiredEntryOrder(); Time[1] can lag real time on slow-forming bars.
                    if (CurrentClockTime() >= trade.SubmittedTime.AddMinutes(EffectiveEntryOrderExpireMinutes(p.Selectivity, t))) {
                        _shadowTrades[t] = null;
                        continue;
                    }

                    // Pessimistic fill: price must trade through the limit, not just touch it.
                    bool filled = trade.IsLong
                        ? Low[1] <= trade.LimitPrice - through
                        : High[1] >= trade.LimitPrice + through;

                    if (!filled)
                        continue;

                    // Wick-and-reverse guard: bar traded through the limit but closed back past it; logged as a forced adverse sample.
                    bool reversedSameBar = trade.IsLong
                        ? Close[1] >= trade.LimitPrice + through
                        : Close[1] <= trade.LimitPrice - through;

                    if (reversedSameBar) {
                        D("Shadow fill wick-and-reverse: template " + t + " " + (trade.IsLong ? "LONG" : "SHORT")
                            + " limit=" + trade.LimitPrice.ToString("0.00") + " low=" + Low[1].ToString("0.00")
                            + " high=" + High[1].ToString("0.00") + " close=" + Close[1].ToString("0.00"));

                        trade.Filled = true;
                        trade.FillBar = CurrentContextBar;
                        trade.EntryPrice = trade.LimitPrice;
                        double forcedExitPrice = trade.IsLong
                            ? trade.LimitPrice - Math.Max(through, TickSize)
                            : trade.LimitPrice + Math.Max(through, TickSize);
                        LogShadowSample(trade, forcedExitPrice);
                        LogTemplateShadowSample(trade, forcedExitPrice);

                        TemplateParams excludedParams = GetTemplateParams(t);
                        _shadowCooldownUntilBar[t] = CurrentContextBar + DerivedReentryCooldownBars(excludedParams.Selectivity);
                        _shadowTrades[t] = null;
                        continue;
                    }

                    trade.Filled = true;
                    trade.FillBar = CurrentContextBar;
                    trade.EntryPrice = trade.LimitPrice;
                    continue; // exits evaluated from the next completed bar onward
                }

                double exitPrice;
                bool stopHit = trade.IsLong ? Low[1] <= trade.StopPrice : High[1] >= trade.StopPrice;
                bool targetHit = trade.IsLong ? High[1] >= trade.TargetPrice : Low[1] <= trade.TargetPrice;

                // Pessimistic bar resolution: if a bar spans both stop and target, count the stop; stops eat one tick of slippage.
                if (stopHit)
                    exitPrice = trade.IsLong ? trade.StopPrice - TickSize : trade.StopPrice + TickSize;
                else if (targetHit)
                    exitPrice = trade.TargetPrice;
                else if (CurrentContextBar - trade.FillBar >= Math.Max(1, ShadowMaxHoldBars))
                    exitPrice = Close[1];
                else
                    continue;

                LogShadowSample(trade, exitPrice);
                LogTemplateShadowSample(trade, exitPrice);
                TemplateParams resolved = GetTemplateParams(t);
                _shadowCooldownUntilBar[t] = CurrentContextBar + DerivedReentryCooldownBars(resolved.Selectivity);
                _shadowTrades[t] = null;
            }
        }

        private void TryOpenShadowTrades() {
            int bands = BandSelectivities.Length;
            bool[] bandComputed = new bool[bands];
            bool[] bandNormLong = new bool[bands];
            bool[] bandNormShort = new bool[bands];
            bool[] bandCrossLong = new bool[bands];
            bool[] bandCrossShort = new bool[bands];
            string[] bandWindowJson = new string[bands];

            // Same ticker resolution ApplyTemplateDerivedSettings uses for the live pullback.
            string shadowTickerName = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : string.Empty;

            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);

            for (int t = minTemplate; t <= maxTemplate; t++) {
                if (_shadowTrades[t] != null || CurrentContextBar < _shadowCooldownUntilBar[t])
                    continue;

                TemplateParams p = GetTemplateParams(t);
                int band = BandIndexForSelectivity(p.Selectivity);

                if (!bandComputed[band]) {
                    bandComputed[band] = true;
                    ComputeBandSignals(band, out bandNormLong[band], out bandNormShort[band], out bandCrossLong[band], out bandCrossShort[band]);
                }

                bool isLong;
                string trigger;
                // Same stoch pass reused for normal and cross triggers, matching TrySubmitEntry()'s gate reuse.
                bool longStochPass = ShadowStochCrossPasses(band, true, p.StochLongMax, DerivedStochRsiCrossLookbackBars(p.Selectivity));
                bool shortStochPass = ShadowStochCrossPasses(band, false, p.StochShortMin, DerivedStochRsiCrossLookbackBars(p.Selectivity));
                bool normalLong = bandNormLong[band] && longStochPass;
                bool normalShort = bandNormShort[band] && shortStochPass;
                bool crossLong = bandCrossLong[band] && longStochPass;
                bool crossShort = bandCrossShort[band] && shortStochPass;

                if (normalLong) { isLong = true; trigger = "TEMA/BB"; }
                else if (normalShort) { isLong = false; trigger = "TEMA/BB"; }
                else if (crossLong) { isLong = true; trigger = ShadowCrossSource(band, true); }
                else if (crossShort) { isLong = false; trigger = ShadowCrossSource(band, false); }
                else continue;

                if (!ShadowMfiPasses(band, isLong, p.MfiLongMax, p.MfiShortMin, DerivedMfiPriorBars(p.Selectivity)))
                    continue;

                if (!ShadowRsiPasses(isLong, p.RsiLongMax, p.RsiShortMin))
                    continue;

                if (bandWindowJson[band] == null)
                    bandWindowJson[band] = BuildShadowWindowJson(band);

                double signalPrice = CurrentInstrument.MasterInstrument.RoundToTickSize(bandTema[band][1]);
                // Same per-ticker, ATR-bound distance a live entry on this template would use (pure
                // compute -- the _lastPullback* snapshot stays owned by the template-apply path).
                // Previously this used the raw table ticks (45-84), placing shadow limits up to ~80x
                // further from the signal than live orders on ES/YM/RTY high-tier templates -- every
                // shadow fill/outcome described a different strategy than the one trading live.
                double shadowAtr, shadowAtrAvg, shadowRatioRaw, shadowRatioClamped;
                int shadowPullbackTicks = ComputeAtrBoundPullbackTicks(shadowTickerName, t, out shadowAtr, out shadowAtrAvg, out shadowRatioRaw, out shadowRatioClamped);
                double pullback = shadowPullbackTicks * TickSize;
                double limitPrice = CurrentInstrument.MasterInstrument.RoundToTickSize(isLong ? signalPrice - pullback : signalPrice + pullback);

                double pointValue = CurrentInstrument.MasterInstrument.PointValue;
                double riskPoints = Math.Max(1, Math.Round(TemplateRiskDollars1R(t) / pointValue / TickSize, MidpointRounding.AwayFromZero)) * TickSize;

                _shadowTrades[t] = new ShadowTrade {
                    Template = t,
                    IsLong = isLong,
                    LimitPrice = limitPrice,
                    SubmittedTime = GetCurrentBarSetupTimestamp(),
                    Filled = false,
                    StopPrice = isLong ? limitPrice - riskPoints : limitPrice + riskPoints,
                    TargetPrice = isLong ? limitPrice + riskPoints : limitPrice - riskPoints,
                    WindowJson = bandWindowJson[band],
                    Trigger = trigger,
                    Symbol = shadowTickerName,
                };
            }
        }

        private void ComputeBandSignals(int band, out bool normLong, out bool normShort, out bool crossLong, out bool crossShort) {
            TEMA tema = bandTema[band];
            Bollinger bands = bandBb[band];

            normLong = tema[2] < bands.Lower[2] && tema[1] >= bands.Lower[1];
            normShort = tema[2] > bands.Upper[2] && tema[1] <= bands.Upper[1];

            bool vwapCrossUp = CrossedAboveAt(tema, sessionVwapSeries, 1);
            bool vwapCrossDown = CrossedBelowAt(tema, sessionVwapSeries, 1);
            bool midBbCrossUp = CrossedAboveAt(tema, bands.Middle, 1);
            bool midBbCrossDown = CrossedBelowAt(tema, bands.Middle, 1);
            bool vwapAboveUpperBand = sessionVwapSeries[1] > bands.Upper[1];
            bool vwapBelowLowerBand = sessionVwapSeries[1] < bands.Lower[1];

            crossLong = midBbCrossUp || (vwapCrossUp && !vwapAboveUpperBand) || (vwapCrossDown && vwapBelowLowerBand);
            crossShort = midBbCrossDown || (vwapCrossDown && !vwapBelowLowerBand) || (vwapCrossUp && vwapAboveUpperBand);
        }

        private string ShadowCrossSource(int band, bool isLong) {
            TEMA tema = bandTema[band];
            Bollinger bands = bandBb[band];
            bool vwapCrossUp = CrossedAboveAt(tema, sessionVwapSeries, 1);
            bool vwapCrossDown = CrossedBelowAt(tema, sessionVwapSeries, 1);
            bool midBbCrossUp = CrossedAboveAt(tema, bands.Middle, 1);
            bool midBbCrossDown = CrossedBelowAt(tema, bands.Middle, 1);
            bool vwapAboveUpperBand = sessionVwapSeries[1] > bands.Upper[1];
            bool vwapBelowLowerBand = sessionVwapSeries[1] < bands.Lower[1];

            if (isLong) {
                if (vwapCrossDown && vwapBelowLowerBand)
                    return "VWAP long rejection";
                if (vwapCrossUp && !vwapAboveUpperBand)
                    return "VWAP long cross";
                return midBbCrossUp ? "MidBB long cross" : "Long cross";
            }

            if (vwapCrossUp && vwapAboveUpperBand)
                return "VWAP short rejection";
            if (vwapCrossDown && !vwapBelowLowerBand)
                return "VWAP short cross";
            return midBbCrossDown ? "MidBB short cross" : "Short cross";
        }

        // Shadow sweep gates on completed bars (baseBarsAgo = 1); shares MfiPassesCore/RsiPassesCore/
        // StochCrossPassesCore with the live filters so the two paths can never drift apart.
        private bool ShadowStochCrossPasses(int band, bool isLong, double threshold, int lookbackBars) {
            return StochCrossPassesCore(bandStochRsi[band], isLong, threshold, lookbackBars, 1);
        }

        private bool ShadowMfiPasses(int band, bool isLong, double mfiLongMax, double mfiShortMin, int priorBars) {
            return MfiPassesCore(bandMfi[band], isLong, mfiLongMax, mfiShortMin, priorBars, 1);
        }

        private bool ShadowRsiPasses(bool isLong, double rsiLongMax, double rsiShortMin) {
            return RsiPassesCore(isLong, rsiLongMax, rsiShortMin, 1);
        }

        private string BuildShadowWindowJson(int band) {
            int bars = Math.Max(1, Math.Min(Math.Max(1, MlWindowBars), CurrentContextBar));
            StringBuilder builder = new StringBuilder();
            builder.Append("[");

            for (int offset = bars - 1; offset >= 0; offset--) {
                if (offset != bars - 1)
                    builder.Append(",");

                builder.Append("[");
                AppendMlFeatureRowFor(builder, 1 + offset, bandTema[band], bandBb[band], bandMfi[band], bandStochRsi[band]);
                builder.Append("]");
            }

            builder.Append("]");
            return builder.ToString();
        }

        // True when the context active at resolve time is a different instrument than the one
        // that opened the shadow trade. Both log paths bail on a mismatch: the trade's window
        // and prices belong to the opening instrument, so logging it under the resolving one
        // poisons that group's training data (this happened July 9-10, 2026 via a context
        // save gap -- see ML_SYSTEM_GUIDE.txt).
        private bool ShadowTradeContextMismatch(ShadowTrade trade) {
            string activeSymbol = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name
                : string.Empty;
            if (string.IsNullOrEmpty(trade.Symbol) || trade.Symbol == activeSymbol)
                return false;
            D("SHADOW SAMPLE DROPPED: cross-context bleed, trade opened on " + trade.Symbol
                + " but resolving under " + activeSymbol + " (template " + trade.Template + ")");
            return true;
        }

        private void LogShadowSample(ShadowTrade trade, double exitPrice) {
            if (ShadowTradeContextMismatch(trade))
                return;
            try {
                double points = trade.IsLong ? exitPrice - trade.EntryPrice : trade.EntryPrice - exitPrice;
                double ticks = TickSize > 0 ? points / TickSize : 0.0;
                double pointValue = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                    ? CurrentInstrument.MasterInstrument.PointValue
                    : 1.0;
                double dollars = points * pointValue;

                // A losing shadow trade must never be labeled with the opposite direction.
                string label = "no_trade";
                if (points > TickSize * 0.5)
                    label = trade.IsLong ? "long" : "short";

                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", trade.Trigger)
                    .Str("timestamp", trade.SubmittedTime.ToString("o", CultureInfo.InvariantCulture))
                    .Raw("label", "\"" + label + "\"")
                    .Obj("metadata", new JsonWriter()
                        .Raw("source", "\"shadow\"")
                        .Raw("shadow", "true")
                        .Str("prediction", trade.IsLong ? "shadow_long" : "shadow_short")
                        .Str("setup_direction", trade.IsLong ? "long" : "short")
                        .Str("setup_source", trade.Trigger)
                        .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty)
                        .Raw("points", FormatJsonDouble(points))
                        .Raw("ticks", FormatJsonDouble(ticks))
                        .Raw("dollars", FormatJsonDouble(dollars))
                        .Raw("quantity", "1")
                        .Raw("template_number", trade.Template.ToString(CultureInfo.InvariantCulture))
                        .Str("resolved_timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture)))
                    .Raw("window", trade.WindowJson)
                    .ToString();


                FireAndForgetPostJson(TrimTrailingSlash(MlServiceUrl) + "/log-sample", payload);
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("Shadow sample log failed: " + error.Message);
            }
        }

        private void LogTemplateShadowSample(ShadowTrade trade, double exitPrice) {
            if (ShadowTradeContextMismatch(trade))
                return;
            try {
                double points = trade.IsLong ? exitPrice - trade.EntryPrice : trade.EntryPrice - exitPrice;
                double pointValue = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                    ? CurrentInstrument.MasterInstrument.PointValue
                    : 1.0;
                double dollars = points * pointValue;
                TemplateParams p = GetTemplateParams(trade.Template);
                double riskPoints = trade.IsLong ? trade.LimitPrice - trade.StopPrice : trade.StopPrice - trade.LimitPrice;
                double rMultiple = riskPoints > 0 ? points / riskPoints : 0.0;

                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", trade.Trigger)
                    .Str("setup_timestamp", trade.SubmittedTime.ToString("o", CultureInfo.InvariantCulture))
                    .Str("resolved_timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture))
                    .Raw("template_number", trade.Template.ToString(CultureInfo.InvariantCulture))
                    .Raw("selectivity", FormatJsonDouble(p.Selectivity))
                    .Str("setup_direction", trade.IsLong ? "long" : "short")
                    .Raw("r_multiple", FormatJsonDouble(rMultiple))
                    .Raw("dollars", FormatJsonDouble(dollars))
                    .Raw("shadow", "true")
                    .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty)
                    .Raw("window", trade.WindowJson)
                    .ToString();

                FireAndForgetPostJson(TrimTrailingSlash(MlServiceUrl) + "/log-template-sample", payload);
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("Template shadow sample log failed: " + error.Message);
            }
        }

        // Same /log-sample endpoint and "no_trade" label as the shadow evaluator, tagged metadata.source="live" for a real ML gate veto.
        private void LogLiveNoTrade(string source, bool defaultIsLong, MlDecision decision) {
            try {
                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", source)
                    .Str("timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture))
                    .Raw("label", "\"no_trade\"")
                    .Obj("metadata", new JsonWriter()
                        .Raw("source", "\"live\"")
                        .Raw("shadow", "false")
                        .Str("setup_direction", defaultIsLong ? "long" : "short")
                        .Str("setup_source", source)
                        .Raw("confidence", decision.Confidence.ToString("0.000", CultureInfo.InvariantCulture))
                        .Raw("template_number", _activeTemplateNumber.ToString(CultureInfo.InvariantCulture))
                        .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty))
                    .ToString();


                FireAndForgetPostJson(TrimTrailingSlash(MlServiceUrl) + "/log-sample", payload);
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("Live no-trade log failed: " + error.Message);
            }
        }



        private readonly HashSet<int> _observedBarsInProgress = new HashSet<int>();

        protected override void OnBarUpdate() {
            // Diagnostic wrapper only: print the full exception (with stack trace) and rethrow, so NT
            // still disables the strategy on an escaped exception but the Output window names the exact
            // method/line instead of NT's generic "accessing an index ... out-of-range" message that made
            // the 2026-07-19 SessionVwapSeries sync bug take hours to locate. Never swallow here: trading
            // on after an unknown mid-bar failure is worse than the disable.
            try {
                OnBarUpdateWrapped();
            }
            catch (Exception ex) {
                Print(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " | OnBarUpdate EXCEPTION " + Name + " (bar=" + CurrentBar + " BarsInProgress=" + BarsInProgress + "): " + ex);
                throw;
            }
        }

        private void OnBarUpdateWrapped() {
            if (!_observedBarsInProgress.Contains(BarsInProgress)) {
                _observedBarsInProgress.Add(BarsInProgress);
                string instName = (BarsArray != null && BarsInProgress < BarsArray.Length && BarsArray[BarsInProgress] != null && BarsArray[BarsInProgress].Instrument != null)
                    ? BarsArray[BarsInProgress].Instrument.FullName
                    : "unknown";
                Print(OutputTimePrefix() + "FIRST BAR RECEIVED " + Name + ": BarsInProgress=" + BarsInProgress + " instrument=" + instName + " CurrentBars[bip]=" + CurrentBars[BarsInProgress]);
            }

            if (EnableMultiSymbolMode) {
                RunWithContext(BarsInProgress, OnBarUpdateCore);
                return;
            }

            OnBarUpdateCore();
        }

        private void OnBarUpdateCore() {
            UpdateSessionVwap();
            DrawStrategyVwap();
            DrawStrategyValues();

            if (State == State.Historical) {
                TryBackfillMlHistoricalSample();
                return;
            }

            if (State != State.Realtime)
                return;

            // Redeliver any exit samples spooled while the ML service was unreachable.
            // Self-throttled (60s) and off-thread, so this is a cheap check per bar.
            ReplayPendingExitSamples();

            ExportPullbackState();

            // Runs before the tradability gates on purpose: a post-cancel touch is evidence whenever it
            // happens, including during no-trade windows or while a position is open.
            ProcessPendingExpireWatch();

            // Must run BEFORE the position-status block below. See ReconcileWatchdogClosedByExitFill.
            ReconcileWatchdogClosedByExitFill();

            if (EffectiveMarketPosition() != MarketPosition.Flat) {
                WriteOpenTradeStatus();
                CheckManualExitRequest();
            }
            else
                ClearOpenTradeStatus();

            if (HasWorkingEntryOrder()) {
                UpdateEntryOrderClosestApproach();
                WritePendingTradeStatus();
                CheckManualCancelRequest();
            }
            else
                ClearPendingTradeStatus();

            if (HandleFlattenOnEnable()) {
                D("GATE " + OutputContext() + ": stopped at HandleFlattenOnEnable");
                return;
            }

            if (CurrentContextBar < BarsRequiredToTrade) {
                D("GATE " + OutputContext() + ": stopped at BarsRequiredToTrade (bar=" + CurrentContextBar + ")");
                return;
            }

            RenderActiveStopVisual(currentStopPrice);

            if (!ConfigIsValid()) {
                D("GATE " + OutputContext() + ": stopped at ConfigIsValid");
                return;
            }

            // Session-boundary check, independent of position/order state; fires once per new session.

            UpdateDailyLossLimit();
            TryRefreshMlExitPhase();

            // General catch-all, unchanged. The narrower account-flat-aware reset near the top of this
            // method handles the exit-fill case early enough for WriteOpenTradeStatus; this still covers
            // every other way the watchdog can go stale while the strategy reads flat.
            if (CurrentPosition.MarketPosition == MarketPosition.Flat && HasWatchdogPosition()) {
                D("WATCHDOG: Clearing stale watchdog state while strategy is flat.");
                ResetWatchdogState();
                WatchdogHeartbeat("OnBarUpdateCore");
            }

            if (EffectiveMarketPosition() != MarketPosition.Flat) {
                ManageOpenPosition();
                HandleMlExitModelOnBar();
            }

            bool templateRotationEligible = !HandleBlockedTimeWindow() && !dailyLossLimitHit;
            if (!templateRotationEligible) {
                // Keep the timer anchored during no-trade/cutoff windows and maintenance breaks.
                ProcessTemplateNoFillRotation();
                D("GATE " + OutputContext() + ": stopped at HandleBlockedTimeWindow/dailyLossLimitHit=" + dailyLossLimitHit);
                return;
            }

            // Eligible trading time: advance the no-fill rotation timer and rotate if the window has elapsed with no fill.
            ProcessTemplateNoFillRotation();

            // Runs only after the same tradability gates a live entry requires, so shadow samples match real conditions.
            if (EnableShadowEvaluation) {
                ClearOutOfBandShadowTradesIfNeeded();
                ProcessShadowEvaluation();
            }

            if (EffectiveMarketPosition() != MarketPosition.Flat) {
                D("GATE " + OutputContext() + ": stopped, EffectiveMarketPosition=" + EffectiveMarketPosition());
                return;
            }

            if (IsFullyFlatAndReconciled())
                ResetPositionState();

            CancelExpiredEntryOrder();

            if (HasWorkingEntryOrder()) {
                D("GATE " + OutputContext() + ": stopped at HasWorkingEntryOrder, entryOrder=" + (entryOrder == null ? "null" : (entryOrder.Name + "/" + entryOrder.OrderState)));
                return;
            }

            if (IsReentryCooldownActive()) {
                D("GATE " + OutputContext() + ": stopped at IsReentryCooldownActive (lastExitBar=" + lastExitBar + " cooldown=" + ReentryCooldownBars + " bar=" + CurrentContextBar + ")");
                return;
            }
            TrySubmitEntry();
        }

        private struct EntrySignals {
            public bool NormalLongSignal;
            public bool NormalShortSignal;
            public bool CrossLongSignal;
            public bool CrossShortSignal;
            public bool NormalLongTemaCross;
            public bool NormalShortTemaCross;
            public bool LongStochPass;
            public bool ShortStochPass;
            public bool CrossLongCore;
            public bool CrossShortCore;

            public bool Any {
                get { return NormalLongSignal || NormalShortSignal || CrossLongSignal || CrossShortSignal; }
            }
        }

        private void TrySubmitEntry() {
            EntrySignals signals = ComputeEntrySignals();

            if (EnableMlTemplateSelection && signals.Any)
                if (MaybeApplyMlTemplateSelection())
                    signals = ComputeEntrySignals(); // template changed under us, indicators moved

            SubmitFromSignals(signals);
        }

        // Pure: indicator reads only, no field mutation/logging/orders -- may run twice per bar when an ML template switch repoints the indicators.
        private EntrySignals ComputeEntrySignals() {
            EntrySignals signals = new EntrySignals();
            signals.NormalLongTemaCross = temaIndicator[1] < bb.Lower[1] && temaIndicator[0] >= bb.Lower[0];
            signals.NormalShortTemaCross = temaIndicator[1] > bb.Upper[1] && temaIndicator[0] <= bb.Upper[0];
            signals.LongStochPass = StochRsiCrossPasses(true);
            signals.ShortStochPass = StochRsiCrossPasses(false);
            signals.NormalLongSignal = signals.NormalLongTemaCross && signals.LongStochPass;
            signals.NormalShortSignal = signals.NormalShortTemaCross && signals.ShortStochPass;
            bool vwapCrossUp = CrossedAbove(temaIndicator, sessionVwapSeries);
            bool vwapCrossDown = CrossedBelow(temaIndicator, sessionVwapSeries);
            bool midBbCrossUp = CrossedAbove(temaIndicator, bb.Middle);
            bool midBbCrossDown = CrossedBelow(temaIndicator, bb.Middle);
            bool vwapAboveUpperBand = sessionVwapSeries[0] > bb.Upper[0];
            bool vwapBelowLowerBand = sessionVwapSeries[0] < bb.Lower[0];
            signals.CrossLongCore = midBbCrossUp || (vwapCrossUp && !vwapAboveUpperBand) || (vwapCrossDown && vwapBelowLowerBand);
            signals.CrossShortCore = midBbCrossDown || (vwapCrossDown && !vwapBelowLowerBand) || (vwapCrossUp && vwapAboveUpperBand);
            signals.CrossLongSignal = EnableTemaVwapMidBbCrossEntry && signals.CrossLongCore && signals.LongStochPass;
            signals.CrossShortSignal = EnableTemaVwapMidBbCrossEntry && signals.CrossShortCore && signals.ShortStochPass;
            return signals;
        }

        private void SubmitFromSignals(EntrySignals signals) {
            bool longSignal = signals.NormalLongSignal || signals.CrossLongSignal;
            bool shortSignal = signals.NormalShortSignal || signals.CrossShortSignal;

            LogNormalNearMiss(true, signals.NormalLongTemaCross, signals.LongStochPass);
            LogNormalNearMiss(false, signals.NormalShortTemaCross, signals.ShortStochPass);
            LogCrossNearMiss(true, EnableTemaVwapMidBbCrossEntry, signals.CrossLongCore, signals.LongStochPass);
            LogCrossNearMiss(false, EnableTemaVwapMidBbCrossEntry, signals.CrossShortCore, signals.ShortStochPass);
            LogGateBlocks(signals);
            if (!StartupEntrySignalsCleared(longSignal, shortSignal))
                return;

            if (signals.NormalLongSignal) {
                double price = CurrentInstrument.MasterInstrument.RoundToTickSize(temaIndicator[0]);
                SubmitMlDirectedEntry(true, price, "L", "TEMA/BB");
            }
            else if (signals.NormalShortSignal) {
                double price = CurrentInstrument.MasterInstrument.RoundToTickSize(temaIndicator[0]);
                SubmitMlDirectedEntry(false, price, "S", "TEMA/BB");
            }
            else if (signals.CrossLongSignal) {
                double price = CurrentInstrument.MasterInstrument.RoundToTickSize(temaIndicator[0]);
                string signalName = CrossSignalName(true);
                string source = CrossSignalSource(true);
                DrawCrossDebugLabel(signalName, source);
                SubmitMlDirectedEntry(true, price, signalName, source);
            }
            else if (signals.CrossShortSignal) {
                double price = CurrentInstrument.MasterInstrument.RoundToTickSize(temaIndicator[0]);
                string signalName = CrossSignalName(false);
                string source = CrossSignalSource(false);
                DrawCrossDebugLabel(signalName, source);
                SubmitMlDirectedEntry(false, price, signalName, source);
            }
        }

        private bool MaybeApplyMlTemplateSelection() {
            if (_lastTemplateMlSelectionBar == CurrentContextBar)
                return false;
            _lastTemplateMlSelectionBar = CurrentContextBar;

            string windowJson = BuildMlWindowJson();
            MlTemplateDecision decision = RequestMlTemplateDecision("template_select", windowJson);
            _templateMlStatus = decision.Status;

            if (!decision.Ok || decision.Status != "good_to_use")
                return false;

            if (decision.Template == _activeTemplateNumber)
                return false;

            ApplyTemplate(decision.Template);
            _activeTemplateSetByMl = true;
            if (PrintTemplateChanges)
                Print(OutputTimePrefix() + "ML TEMPLATE " + OutputContext() + ": selected=" + decision.Template + " confidence=" + decision.Confidence.ToString("0.000", CultureInfo.InvariantCulture));
            return true;
        }

        private void TryBackfillMlHistoricalSample() {
            if (!EnableMlHistoricalBackfill || CurrentContextBar < BarsRequiredToTrade)
                return;

            if (mlBackfillSamplesSent >= Math.Max(1, MlBackfillMaxSamples))
                return;

            int horizon = Math.Max(1, MlBackfillHorizonBars);
            if (CurrentContextBar <= horizon + 2)
                return;

            int eventBarsAgo = horizon;
            if (Time[eventBarsAgo].Date != NinjaTrader.Core.Globals.Now.Date)
                return;

            string trigger = BackfillTriggerName(eventBarsAgo);
            if (string.IsNullOrEmpty(trigger))
                return;

            string label = BackfillLabel(eventBarsAgo, horizon);
            string windowJson = BuildMlWindowJsonAt(eventBarsAgo);
            string payload = new JsonWriter()
                .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                .Str("trigger", "backfill " + trigger)
                .Str("timestamp", Time[eventBarsAgo].ToString("o", CultureInfo.InvariantCulture))
                .Raw("label", "\"" + label + "\"")
                .Obj("metadata", new JsonWriter()
                    .Raw("source", "\"historical_backfill\"")
                    .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty)
                    .Raw("horizon_bars", horizon.ToString(CultureInfo.InvariantCulture)))
                .Raw("window", windowJson)
                .ToString();


            try {
                PostJson(TrimTrailingSlash(MlServiceUrl) + "/log-sample", payload);
                mlBackfillSamplesSent++;
                if (DebugMode)
                    Print(Time[0] + " " + Name + " - ML backfill sample " + mlBackfillSamplesSent + " " + trigger + " label=" + label);
            }
            catch (Exception error) {
                if (DebugMode && mlBackfillSamplesSent == 0)
                    if (ShouldPrintMlHttpError()) Print(Time[0] + " " + Name + " - ML backfill failed: " + error.Message);
            }
        }

        private string BackfillTriggerName(int barsAgo) {
            if (barsAgo + 1 > CurrentContextBar)
                return string.Empty;

            bool normalLongSignal = temaIndicator[barsAgo + 1] < bb.Lower[barsAgo + 1] && temaIndicator[barsAgo] >= bb.Lower[barsAgo];
            bool normalShortSignal = temaIndicator[barsAgo + 1] > bb.Upper[barsAgo + 1] && temaIndicator[barsAgo] <= bb.Upper[barsAgo];
            bool vwapCrossUp = CrossedAboveAt(temaIndicator, sessionVwapSeries, barsAgo);
            bool vwapCrossDown = CrossedBelowAt(temaIndicator, sessionVwapSeries, barsAgo);
            bool midBbCrossUp = CrossedAboveAt(temaIndicator, bb.Middle, barsAgo);
            bool midBbCrossDown = CrossedBelowAt(temaIndicator, bb.Middle, barsAgo);
            bool vwapAboveUpperBand = sessionVwapSeries[barsAgo] > bb.Upper[barsAgo];
            bool vwapBelowLowerBand = sessionVwapSeries[barsAgo] < bb.Lower[barsAgo];

            if (normalLongSignal)
                return "lower_bb_cross_up";
            if (normalShortSignal)
                return "upper_bb_cross_down";
            if (midBbCrossUp)
                return "mid_bb_cross_up";
            if (midBbCrossDown)
                return "mid_bb_cross_down";
            if (vwapCrossDown && vwapBelowLowerBand)
                return "vwap_long_rejection";
            if (vwapCrossUp && vwapAboveUpperBand)
                return "vwap_short_rejection";
            if (vwapCrossUp)
                return "vwap_cross_up";
            if (vwapCrossDown)
                return "vwap_cross_down";

            return string.Empty;
        }

        private bool CrossedAboveAt(ISeries<double> input, ISeries<double> line, int barsAgo) {
            return barsAgo + 1 <= CurrentContextBar && input[barsAgo + 1] < line[barsAgo + 1] && input[barsAgo] >= line[barsAgo];
        }

        private bool CrossedBelowAt(ISeries<double> input, ISeries<double> line, int barsAgo) {
            return barsAgo + 1 <= CurrentContextBar && input[barsAgo + 1] > line[barsAgo + 1] && input[barsAgo] <= line[barsAgo];
        }

        private string BackfillLabel(int eventBarsAgo, int horizon) {
            double entry = Close[eventBarsAgo];
            double maxUp = 0.0;
            double maxDown = 0.0;

            for (int barsAgo = eventBarsAgo - 1; barsAgo >= 0; barsAgo--) {
                maxUp = Math.Max(maxUp, High[barsAgo] - entry);
                maxDown = Math.Max(maxDown, entry - Low[barsAgo]);
            }

            double minMove = Math.Max(0, MlBackfillMinMoveTicks) * TickSize;
            if (maxUp < minMove && maxDown < minMove)
                return "no_trade";

            return maxUp >= maxDown ? "long" : "short";
        }

        private string BuildMlWindowJsonAt(int eventBarsAgo) {
            int bars = Math.Max(1, Math.Min(Math.Max(1, MlWindowBars), CurrentContextBar - eventBarsAgo + 1));
            StringBuilder builder = new StringBuilder();
            builder.Append("[");

            for (int offset = bars - 1; offset >= 0; offset--) {
                if (offset != bars - 1)
                    builder.Append(",");

                builder.Append("[");
                AppendMlFeatureRow(builder, eventBarsAgo + offset);
                builder.Append("]");
            }

            builder.Append("]");
            return builder.ToString();
        }
        private struct MlDecision {
            public bool Ok;
            public string Action;
            public double Confidence;
            public string Status;
            public string RawResponse;
        }

        private void SubmitMlDirectedEntry(bool defaultIsLong, double price, string signalName, string source) {
            bool finalIsLong = defaultIsLong;
            string finalSignalName = signalName;
            string finalSource = source;
            // Same bar-cached clock read TryOpenShadowTrades uses, so this setup's shadow rows
            // (if any) and this live entry (if it fills) can be grouped together downstream.
            pendingSetupTimestamp = GetCurrentBarSetupTimestamp();
            pendingMlWindowJson = string.Empty;
            pendingMlTrigger = string.Empty;
            pendingMlPrediction = string.Empty;
            pendingMlConfidence = 0.0;
            pendingMlSetupDirection = defaultIsLong ? "long" : "short";
            pendingMlSignal = signalName;
            pendingMlReversal = false;
            string mlSubmitOutput = string.Empty;

            string windowJson = string.Empty;
            if (EnableMlTradeLogging || EnableMlDirectionService) {
                windowJson = BuildMlWindowJson();
                pendingMlWindowJson = windowJson;
                pendingMlTrigger = source;
                pendingMlPrediction = defaultIsLong ? "strategy_long" : "strategy_short";
                pendingMlConfidence = 1.0;
                pendingMlSetupDirection = defaultIsLong ? "long" : "short";
                pendingMlSignal = signalName;
                pendingMlReversal = false;
            }

            if (EnableMlDirectionService) {
                MlDecision decision = RequestMlDecision(source, windowJson);

                // Outage/invalid-response handling: falls back to the plain technical signal
                // (same as an ungated status below) instead of skipping the entry outright.
                // Before this fix, an ML service outage silently blocked EVERY entry on every
                // ML-enabled instance -- a single point of failure discovered during the
                // 2026-07-18 phase audit (port-8765 double-bind history made this a real risk,
                // not theoretical). finalIsLong/pendingMl* already hold the plain-signal
                // defaults set above, so no other change is needed to submit normally.
                if (!decision.Ok) {
                    D("ML service unavailable or invalid (falling back to plain signal) for " + source + ". Response=" + decision.RawResponse);
                }
                // Quality gate: decision.Status mirrors ml_model.py's classify_entry_model_status(). Only "good to use" is ML-gated/reversed; other groups submit the plain technical signal.
                else if (decision.Status != "good_to_use") {
                    if (DebugMode)
                        Print(OutputTimePrefix() + OutputContext() + " ML NOT GOOD-TO-USE (status=" + decision.Status + "): using plain signal for " + source);
                }
                else {
                    if (decision.Action == "no_trade") {
                        bool isNewStreak = source != _lastNoTradeSource;
                        bool intervalElapsed = (CurrentClockTime() - _lastNoTradeLogTime) >= TimeSpan.FromSeconds(NoTradeLogIntervalSeconds);

                        if (isNewStreak || intervalElapsed) {
                            if (DebugMode)
                            Print(OutputTimePrefix() + "ML NO_TRADE " + OutputContext() + ": source=" + source + " confidence=" + decision.Confidence.ToString("0.000", CultureInfo.InvariantCulture));
                            _lastNoTradeLogTime = CurrentClockTime();
                        }

                        // Unthrottled so the dashboard's live-veto count reflects every gate decision.
                        LogLiveNoTrade(source, defaultIsLong, decision);

                        _lastNoTradeSource = source;
                        return;
                    }

                    _lastNoTradeSource = string.Empty;

                    finalIsLong = decision.Action == "long";
                    bool mlReversal = finalIsLong != defaultIsLong;
                    finalSignalName = mlReversal
                        ? (defaultIsLong ? "ReversalLong" : "ReversalShort")
                        : (finalIsLong ? "ConfirmLong" : "ConfirmShort");
                    finalSource = source + " ML " + decision.Action + " confidence=" + decision.Confidence.ToString("0.000", CultureInfo.InvariantCulture);
                    mlSubmitOutput = mlReversal
                        ? "ML REVERSAL " + OutputContext() + ": setup=" + (defaultIsLong ? "LONG" : "SHORT") + " ml=" + (finalIsLong ? "LONG" : "SHORT") + " signal=" + finalSignalName + " confidence=" + decision.Confidence.ToString("0.000", CultureInfo.InvariantCulture) + " source=" + source + " order=submitted"
                        : string.Empty;
                    pendingMlWindowJson = windowJson;
                    pendingMlTrigger = source;
                    pendingMlPrediction = decision.Action;
                    pendingMlConfidence = decision.Confidence;
                    pendingMlSetupDirection = defaultIsLong ? "long" : "short";
                    pendingMlSignal = finalSignalName;
                    pendingMlReversal = mlReversal;
                }
            }

            if (!string.IsNullOrEmpty(pendingMlWindowJson))
                SaveMlTradeState(false);

            SubmitEntry(finalIsLong, price, finalSignalName, finalSource, mlSubmitOutput);
        }

        private MlDecision RequestMlDecision(string trigger, string windowJson) {
            MlDecision decision = new MlDecision { Ok = false, Action = "no_trade", Confidence = 0.0, Status = "warming_up", RawResponse = string.Empty };

            try {
                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", trigger)
                    .Str("timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture))
                    .Raw("min_confidence", MlMinConfidence.ToString("0.########", CultureInfo.InvariantCulture))
                    .Obj("metadata", new JsonWriter()
                        .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty))
                    .Raw("window", windowJson)
                    .ToString();


                string response = PostJson(TrimTrailingSlash(MlServiceUrl) + "/predict", payload);
                decision.RawResponse = response;
                decision.Action = ExtractJsonString(response, "action");
                decision.Confidence = ExtractJsonDouble(response, "confidence");
                decision.Status = ExtractJsonString(response, "status");
                decision.Ok = decision.Action == "long" || decision.Action == "short" || decision.Action == "no_trade";
            }
            catch (Exception error) {
                decision.RawResponse = error.Message;
                if (ShouldPrintMlHttpError()) D("ML request failed: " + error.Message);
            }

            return decision;
        }

        private struct MlTemplateDecision {
            public bool Ok;
            public int Template;
            public double Confidence;
            public string Status;
            public string RawResponse;
        }

        private MlTemplateDecision RequestMlTemplateDecision(string trigger, string windowJson) {
            MlTemplateDecision decision = new MlTemplateDecision {
                Ok = false,
                Template = 0,
                Confidence = 0.0,
                Status = "warming_up",
                RawResponse = string.Empty
            };

            try {
                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", trigger)
                    .Str("timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture))
                    .Obj("metadata", new JsonWriter()
                        .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty))
                    .Raw("window", windowJson)
                    .ToString();

                // Hyphenated to match /log-sample, /predict-exit, /log-exit-sample, /ml-exit-phase.
                string response = PostJson(TrimTrailingSlash(MlServiceUrl) + "/predict-template", payload);
                decision.RawResponse = response;
                double templateRaw = ExtractJsonDouble(response, "template");
                decision.Confidence = ExtractJsonDouble(response, "confidence");
                decision.Status = ExtractJsonString(response, "status");
                if (string.IsNullOrEmpty(decision.Status))
                    decision.Status = "warming_up";

                int minTemplate, maxTemplate;
                GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);
                // Session/mode bounds, not the global 1-40 range: an out-of-session recommendation is rejected
                // the same way rotation would reject it. Non-integer/NaN/zero/negative all fail here too.
                // Round (not truncate) before assigning Template, so floating-point noise just under an
                // integer (e.g. 7.9999999) doesn't silently become the wrong template even though it's
                // within the whole-number tolerance below.
                bool wholeNumber = !double.IsNaN(templateRaw) && !double.IsInfinity(templateRaw)
                    && Math.Abs(templateRaw - Math.Round(templateRaw)) < 1e-6;
                decision.Template = wholeNumber ? (int)Math.Round(templateRaw) : 0;
                decision.Ok = wholeNumber && decision.Template >= minTemplate && decision.Template <= maxTemplate;
            }
            catch (Exception error) {
                decision.RawResponse = error.Message;
                if (ShouldPrintMlHttpError()) D("ML template request failed: " + error.Message);
            }

            return decision;
        }

        private string BuildMlWindowJson() {
            int bars = Math.Max(1, Math.Min(Math.Max(1, MlWindowBars), CurrentContextBar + 1));
            StringBuilder builder = new StringBuilder();
            builder.Append("[");

            for (int offset = bars - 1; offset >= 0; offset--) {
                if (offset != bars - 1)
                    builder.Append(",");

                builder.Append("[");
                AppendMlFeatureRow(builder, offset);
                builder.Append("]");
            }

            builder.Append("]");
            return builder.ToString();
        }

        private void AppendMlFeatureRow(StringBuilder builder, int barsAgo) {
            AppendMlFeatureRowFor(builder, barsAgo, temaIndicator, bb, mfi, stochRsi);
        }

        // Shadow evaluation: indicator-parametrized so shadow windows use the triggering template's band.
        private void AppendMlFeatureRowFor(StringBuilder builder, int barsAgo, TEMA temaSource, Bollinger bbSource, MFI mfiSource, NinjaTrader.NinjaScript.Indicators.StochRSI stochSource) {
            double tick = Math.Max(TickSize, 1e-9);
            double close = Close[barsAgo];
            double open = Open[barsAgo];
            double high = High[barsAgo];
            double low = Low[barsAgo];
            double temaNow = temaSource[barsAgo];
            double temaPrev = barsAgo + 1 <= CurrentContextBar ? temaSource[barsAgo + 1] : temaNow;
            double macdNow = macd != null ? macd.Diff[barsAgo] : 0.0;
            double macdPrev = macd != null && barsAgo + 1 <= CurrentContextBar ? macd.Diff[barsAgo + 1] : macdNow;
            double mfiNow = mfiSource != null ? mfiSource[barsAgo] : 50.0;
            double mfiPrev = mfiSource != null && barsAgo + 1 <= CurrentContextBar ? mfiSource[barsAgo + 1] : mfiNow;
            double stochNow = stochSource != null ? stochSource[barsAgo] : 0.5;
            double stochPrev = stochSource != null && barsAgo + 1 <= CurrentContextBar ? stochSource[barsAgo + 1] : stochNow;
            double vwapNow = sessionVwapSeries[barsAgo];
            double vwapPrev = barsAgo + 1 <= CurrentContextBar ? sessionVwapSeries[barsAgo + 1] : vwapNow;
            double priorClose = barsAgo + 1 <= CurrentContextBar ? Close[barsAgo + 1] : close;

            double[] features = new double[] {
                (close - vwapNow) / tick,
                (close - bbSource.Middle[barsAgo]) / tick,
                (close - bbSource.Upper[barsAgo]) / tick,
                (close - bbSource.Lower[barsAgo]) / tick,
                (temaNow - temaPrev) / tick,
                macdNow / tick,
                (macdNow - macdPrev) / tick,
                mfiNow,
                mfiNow - mfiPrev,
                rsi != null ? rsi[barsAgo] : 50.0,
                stochNow,
                stochNow - stochPrev,
                atr != null ? atr[barsAgo] / tick : 0.0,
                (bbSource.Upper[barsAgo] - bbSource.Lower[barsAgo]) / tick,
                (vwapNow - vwapPrev) / tick,
                Math.Abs(close - open) / tick,
                (high - Math.Max(open, close)) / tick,
                (Math.Min(open, close) - low) / tick,
                Volume[barsAgo] / 10000.0,
                (close - priorClose) / tick,
                SymbolHashFeature(17),
                SymbolHashFeature(131),
                DollarsPerTickFeature(),
                PriceScaleFeature(close),
                BarsPeriodValueFeature(),
                BarsTypeHashFeature(23),
                BarsTypeHashFeature(151),
                BarsTypeCategoryFeature()
            };

            for (int i = 0; i < features.Length; i++) {
                if (i > 0)
                    builder.Append(",");
                builder.Append(FormatJsonDouble(features[i]));
            }
        }

        private double SymbolHashFeature(int seed) {
            int hash = seed;
            string symbol = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? CurrentInstrument.MasterInstrument.Name.ToUpperInvariant()
                : "UNKNOWN";

            for (int i = 0; i < symbol.Length; i++)
                hash = unchecked(hash * 31 + symbol[i]);

            return Math.Max(-1.0, Math.Min(1.0, Math.Abs(hash % 20001) / 10000.0 - 1.0));
        }

        private double DollarsPerTickFeature() {
            double dollarsPerTick = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                ? Math.Max(1e-9, CurrentInstrument.MasterInstrument.PointValue * TickSize)
                : 1.0;

            return Math.Max(-2.0, Math.Min(2.0, (Math.Log10(dollarsPerTick) - 1.0) / 2.0));
        }

        private double PriceScaleFeature(double price) {
            return Math.Max(-2.0, Math.Min(2.0, (Math.Log10(Math.Max(1e-9, price)) - 3.5) / 2.0));
        }

        private double BarsPeriodValueFeature() {
            int value = CurrentBarsPeriod != null ? Math.Max(1, CurrentBarsPeriod.Value) : 1;
            return Math.Max(-2.0, Math.Min(2.0, (Math.Log10(value) - 2.0) / 2.0));
        }

        private double BarsTypeHashFeature(int seed) {
            int hash = seed;
            string text = CurrentBarsPeriod != null
                ? (CurrentBarsPeriod.BarsPeriodType.ToString() + ":" + CurrentBarsPeriod.Value.ToString(CultureInfo.InvariantCulture)).ToUpperInvariant()
                : "UNKNOWN";

            for (int i = 0; i < text.Length; i++)
                hash = unchecked(hash * 31 + text[i]);

            return Math.Max(-1.0, Math.Min(1.0, Math.Abs(hash % 20001) / 10000.0 - 1.0));
        }

        private double BarsTypeCategoryFeature() {
            if (CurrentBarsPeriod == null)
                return 0.0;

            string type = CurrentBarsPeriod.BarsPeriodType.ToString().ToLowerInvariant();
            if (type.Contains("tick"))
                return -1.0;
            if (type.Contains("minute"))
                return -0.5;
            if (type.Contains("range"))
                return 0.0;
            if (type.Contains("volume"))
                return 0.5;
            if (type.Contains("day"))
                return 1.0;

            return 0.25;
        }

        private void LogMlTradeOutcome(double exitPrice) {
            if (!EnableMlTradeLogging || activeMlSampleLogged || string.IsNullOrEmpty(activeMlWindowJson) || entryPrice <= 0)
                return;

            try {
                double points = activeMlIsLong ? exitPrice - entryPrice : entryPrice - exitPrice;
                double ticks = TickSize > 0 ? points / TickSize : 0.0;
                int mlQuantity = Math.Max(1, EffectiveQuantity());
                double pointValue = CurrentInstrument != null && CurrentInstrument.MasterInstrument != null
                    ? CurrentInstrument.MasterInstrument.PointValue
                    : 1.0;
                double dollars = points * pointValue * mlQuantity;
                // A losing trade must never be labeled with the opposite direction (matches LogShadowSample()).
                string label = "no_trade";
                if (points > TickSize * 0.5)
                    label = activeMlIsLong ? "long" : "short";

                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", activeMlTrigger)
                    .Str("timestamp", Time[0].ToString("o", CultureInfo.InvariantCulture))
                    .Raw("label", "\"" + label + "\"")
                    .Obj("metadata", new JsonWriter()
                        .Str("prediction", activeMlPrediction)
                        .Str("setup_direction", activeMlSetupDirection)
                        .Str("ml_direction", activeMlPrediction)
                        .Str("ml_signal", activeMlSignal)
                        .Raw("ml_reversal", activeMlReversal ? "true" : "false")
                        .Str("setup_source", activeMlTrigger)
                        .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty)
                        .Raw("confidence", activeMlConfidence.ToString("0.########", CultureInfo.InvariantCulture))
                        .Raw("points", points.ToString("0.########", CultureInfo.InvariantCulture))
                        .Raw("ticks", ticks.ToString("0.########", CultureInfo.InvariantCulture))
                        .Raw("dollars", dollars.ToString("0.########", CultureInfo.InvariantCulture))
                        .Raw("quantity", mlQuantity.ToString(CultureInfo.InvariantCulture))
                        .Raw("template_number", _activeTemplateNumber.ToString(CultureInfo.InvariantCulture)))
                    .Raw("window", activeMlWindowJson)
                    .ToString();


                // Fire-and-forget, matching the sibling /log-sample calls (see LogSample / LogTemplateSample).
                // This runs on NT's order-callback thread inside the exit-fill handler; a synchronous PostJson
                // here blocks fill processing on the ML service's responsiveness. On 2026-07-20 an instance
                // reloaded over the open NQ short went silent right after Realtime during a reload thrash, and
                // this was one of the synchronous-I/O vectors on that thread. FireAndForgetPostJson caps the
                // blast radius to a worker Task; the order-callback thread never waits on the network.
                FireAndForgetPostJson(TrimTrailingSlash(MlServiceUrl) + "/log-sample", payload);
                activeMlSampleLogged = true;
                D("ML sample dispatched label=" + label + " points=" + points.ToString("0.00", CultureInfo.InvariantCulture) + " dollars=" + dollars.ToString("0.00", CultureInfo.InvariantCulture));
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("ML sample log failed: " + error.Message);
            }
        }

        // Persists in-flight ML state to disk, keyed by instrument, so a fresh instance after recompile/restart can recover it and still log the /log-sample outcome.
        private readonly object mlTradeStateLock = new object();

        private string SanitizeFileNamePart(string value) {
            if (string.IsNullOrEmpty(value))
                return "unknown";

            StringBuilder sb = new StringBuilder(value.Length);
            foreach (char c in value)
                sb.Append(char.IsLetterOrDigit(c) ? c : '_');
            return sb.ToString();
        }

        private string MlTradeStatePath(string instrumentFullName) {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_ml_state_" + SanitizeFileNamePart(instrumentFullName) + ".json");
        }

        // Case-preserving string extraction (ExtractJsonString lowercases, which would corrupt trigger/prediction text).
        private sealed class JsonWriter {
            private readonly StringBuilder sb = new StringBuilder("{");
            private bool hasField;

            private void Prefix() {
                if (hasField) sb.Append(',');
                hasField = true;
            }

            public JsonWriter Str(string key, string value) {
                Prefix();
                sb.Append('"').Append(key).Append("\":\"").Append(JsonEscape(value)).Append('"');
                return this;
            }

            public JsonWriter Raw(string key, string rawValue) {
                Prefix();
                sb.Append('"').Append(key).Append("\":").Append(rawValue);
                return this;
            }

            public JsonWriter Obj(string key, JsonWriter nested) {
                Prefix();
                sb.Append('"').Append(key).Append("\":").Append(nested.ToString());
                return this;
            }

            public override string ToString() {
                return sb.ToString() + "}";
            }
        }

        private int FindJsonValueOffset(string json, string key) {
            string marker = "\"" + key + "\":";
            int start = json.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
            return start < 0 ? -1 : start + marker.Length;
        }

        private string ExtractJsonStringRaw(string json, string key) {
            int start = FindJsonValueOffset(json, key);
            if (start < 0)
                return string.Empty;
            start = json.IndexOf('"', start);
            if (start < 0)
                return string.Empty;

            StringBuilder sb = new StringBuilder();
            int i = start + 1;
            while (i < json.Length) {
                char c = json[i];
                if (c == '\\' && i + 1 < json.Length) {
                    sb.Append(json[i + 1]);
                    i += 2;
                    continue;
                }
                if (c == '"')
                    break;
                sb.Append(c);
                i++;
            }
            return sb.ToString();
        }


        private void SaveMlTradeState(bool filled, string orderId = "", string executionId = "") {
            try {
                if (CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                    return;

                string windowJson = filled ? activeMlWindowJson : pendingMlWindowJson;

                // Exit-model tracking fields persist even when entry-side ML logging is disabled, so a restart mid-position can still recover them.
                string json = new JsonWriter()
                    .Str("instrument", CurrentInstrument.FullName)
                    .Raw("filled", filled ? "true" : "false")
                    .Str("entryOrderId", orderId)
                    .Str("entryExecutionId", executionId)
                    .Raw("isLong", activeMlIsLong ? "true" : "false")
                    .Raw("entryPrice", FormatJsonDouble(entryPrice))
                    .Str("entrySignal", activeEntrySignal)
                    .Str("trigger", filled ? activeMlTrigger : pendingMlTrigger)
                    .Str("prediction", filled ? activeMlPrediction : pendingMlPrediction)
                    .Raw("confidence", FormatJsonDouble(filled ? activeMlConfidence : pendingMlConfidence))
                    .Str("setupDirection", filled ? activeMlSetupDirection : pendingMlSetupDirection)
                    .Str("signal", filled ? activeMlSignal : pendingMlSignal)
                    .Raw("reversal", (filled ? activeMlReversal : pendingMlReversal) ? "true" : "false")
                    .Raw("templateNumber", _activeTemplateNumber.ToString(CultureInfo.InvariantCulture))
                    .Str("savedAtUtc", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))
                    .Str("windowJson", windowJson)
                    .Str("exitTradeId", _exitTradeId)
                    .Raw("exitEntryPrice", FormatJsonDouble(_exitEntryPrice))
                    .Raw("exitOneRPoints", FormatJsonDouble(_exitOneRPoints))
                    .Str("exitDirection", _exitDirection)
                    .Raw("exitBarsHeld", _exitBarsHeld.ToString(CultureInfo.InvariantCulture))
                    .Str("exitFeatureHistoryJson", SerializeFeatureHistoryJson(_exitFeatureHistory))
                    .ToString();


                lock (mlTradeStateLock) {
                    File.WriteAllText(MlTradeStatePath(CurrentInstrument.FullName), json);
                }
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("ML trade state save failed: " + error.Message);
            }
        }

        private void ClearMlTradeState(string instrumentFullName) {
            try {
                string path = MlTradeStatePath(instrumentFullName);
                lock (mlTradeStateLock) {
                    if (File.Exists(path))
                        File.Delete(path);
                }
            }
            catch {
            }
        }

        private bool TryLoadMlTradeState(string instrumentFullName, out string json) {
            json = string.Empty;
            try {
                string path = MlTradeStatePath(instrumentFullName);
                lock (mlTradeStateLock) {
                    if (!File.Exists(path))
                        return false;
                    json = File.ReadAllText(path);
                }
                return !string.IsNullOrWhiteSpace(json);
            }
            catch {
                return false;
            }
        }

        // Covers a restart between order-submit and fill: recover in-memory pending fields from the pre-restart persisted state.
        private void RestorePendingMlStateIfMissing() {
            if (!string.IsNullOrEmpty(pendingMlWindowJson) || CurrentInstrument == null)
                return;

            string json;
            if (!TryLoadMlTradeState(CurrentInstrument.FullName, out json))
                return;

            // A stale filled record must never be adopted as a pending entry, to avoid attaching a prior trade's ML metadata.
            if (ExtractJsonBool(json, "filled"))
                return;

            string windowJson = ExtractJsonStringRaw(json, "windowJson");
            if (string.IsNullOrEmpty(windowJson))
                return;

            pendingMlWindowJson = windowJson;
            pendingMlTrigger = ExtractJsonStringRaw(json, "trigger");
            pendingMlPrediction = ExtractJsonStringRaw(json, "prediction");
            pendingMlConfidence = ExtractJsonDouble(json, "confidence");
            pendingMlSetupDirection = ExtractJsonStringRaw(json, "setupDirection");
            pendingMlSignal = ExtractJsonStringRaw(json, "signal");
            pendingMlReversal = ExtractJsonBool(json, "reversal");
            Print(OutputTimePrefix() + "ML STATE RECOVERED (pending) " + OutputContext() + ": restored from disk after restart");
        }

        // Covers a restart with a position already open; trusts the persisted record only if written post-fill and matching direction.
        private void RestoreActiveMlStateIfMissing() {
            if (!string.IsNullOrEmpty(activeMlWindowJson) || CurrentInstrument == null)
                return;

            string json;
            if (!TryLoadMlTradeState(CurrentInstrument.FullName, out json))
                return;

            if (!ExtractJsonBool(json, "filled"))
                return;

            bool recordIsLong = ExtractJsonBool(json, "isLong");
            if (recordIsLong != (EffectiveMarketPosition() == MarketPosition.Long))
                return;

            string windowJson = ExtractJsonStringRaw(json, "windowJson");
            if (string.IsNullOrEmpty(windowJson))
                return;

            activeMlWindowJson = windowJson;
            activeMlTrigger = ExtractJsonStringRaw(json, "trigger");
            activeMlPrediction = ExtractJsonStringRaw(json, "prediction");
            activeMlConfidence = ExtractJsonDouble(json, "confidence");
            activeMlSetupDirection = ExtractJsonStringRaw(json, "setupDirection");
            activeMlSignal = ExtractJsonStringRaw(json, "signal");
            activeMlReversal = ExtractJsonBool(json, "reversal");
            activeMlIsLong = recordIsLong;
            activeMlSampleLogged = false;

            if (string.IsNullOrEmpty(activeEntrySignal))
                activeEntrySignal = ExtractJsonStringRaw(json, "entrySignal");

            Print(OutputTimePrefix() + "ML STATE RECOVERED (active) " + OutputContext() + ": restored from disk after restart, trigger=" + activeMlTrigger);
        }

        // Same restart-with-open-position recovery as RestoreActiveMlStateIfMissing(), for the exit model's tracking fields.
        private void RestoreExitTrackingIfMissing() {
            if (!string.IsNullOrEmpty(_exitTradeId) || CurrentInstrument == null)
                return;

            string json;
            if (!TryLoadMlTradeState(CurrentInstrument.FullName, out json))
                return;

            if (!ExtractJsonBool(json, "filled"))
                return;

            bool recordIsLong = ExtractJsonBool(json, "isLong");
            if (recordIsLong != (EffectiveMarketPosition() == MarketPosition.Long))
                return;

            string recordTradeId = ExtractJsonStringRaw(json, "exitTradeId");
            double recordEntryPrice = ExtractJsonDouble(json, "exitEntryPrice");
            double recordOneRPoints = ExtractJsonDouble(json, "exitOneRPoints");
            string recordDirection = ExtractJsonStringRaw(json, "exitDirection");

            if (string.IsNullOrEmpty(recordTradeId) || recordEntryPrice <= 0 || recordOneRPoints < TickSize
                || (recordDirection != "long" && recordDirection != "short"))
                return;

            _exitTradeId = recordTradeId;
            _exitEntryPrice = recordEntryPrice;
            _exitOneRPoints = recordOneRPoints;
            _exitDirection = recordDirection;
            _exitBarsHeld = ExtractJsonInt(json, "exitBarsHeld", 0);
            _exitFeatureHistory = ParseFeatureHistoryJson(ExtractJsonStringRaw(json, "exitFeatureHistoryJson"));
            _lastMlExitSampleBar = int.MinValue;
            _lastMlExitSampleLogTime = DateTime.MinValue;
            _lastMlExitPredictionBar = int.MinValue;
            _lastMlExitControlBar = int.MinValue;
            _mlExitSubmitted = false;
            _mlExitArmedPrinted = false;

            Print(OutputTimePrefix() + "ML EXIT STATE RECOVERED " + OutputContext() + ": restored from disk after restart, trade=" + _exitTradeId + " barsHeld=" + _exitBarsHeld + " historyRows=" + _exitFeatureHistory.Count);
        }

        private string DashboardTradeLogPath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_completed_trades.tsv");
        }

        private static readonly object noFillLogLock = new object();
        private bool noFillLogHeaderChecked;

        private string NoFillLogPath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_nofill_log.tsv");
        }

        // Logs an expired, unfilled limit entry order for context; a later fill is tracked separately in the completed-trades TSV.
        private void AppendNoFillLog(Order cancelledOrder) {
            if (CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            try {
                string ticker = (activeContext != null && activeContext.Symbol != string.Empty)
                    ? activeContext.Symbol
                    : CurrentInstrument.MasterInstrument.Name;
                string direction = cancelledOrder.OrderAction == OrderAction.Buy ? "LONG" : "SHORT";

                string barsPeriodType = string.Empty;
                string barsPeriodValue = string.Empty;
                try {
                    if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                        barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                        barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                    }
                }
                catch {
                }

                double waitedMinutes = entryOrderSubmittedTime > DateTime.MinValue
                    ? (CurrentClockTime() - entryOrderSubmittedTime).TotalMinutes
                    : 0.0;

                // A ~0-minute wait means the order was cancelled the instant it was submitted (the session-boundary
                // resubmit/cancel loop bug); nothing meaningful to log, so skip it rather than spamming the dashboard.
                if (waitedMinutes < 0.005)
                    return;

                double atrValue = atr != null ? atr[0] : 0.0;
                // Price of the instrument at the moment the limit order was placed, vs. at the moment it was cancelled.
                double marketPriceAtPlacement = entryOrderSubmittedMarketPrice;
                double marketPriceAtCancel = Closes[CurrentBarsInProgressIndex()][0];

                // How many ticks short of the limit price the market's closest approach actually missed by (>=0;
                // a confirmed no-fill never reaches/crosses the limit, but clamp defensively against tick noise).
                // Uses entryOrderClosestApproachPrice (the best Low/High seen while the order was working), not
                // marketPriceAtCancel, so this reflects the true nearest miss rather than wherever price happened
                // to be at the moment of cancellation.
                bool isLong = direction == "LONG";
                double rawTicksShort = isLong
                    ? (entryOrderClosestApproachPrice - cancelledOrder.LimitPrice) / TickSize
                    : (cancelledOrder.LimitPrice - entryOrderClosestApproachPrice) / TickSize;
                double missedByTicks = Math.Max(0.0, rawTicksShort);

                string path = NoFillLogPath();
                // atrRatioRaw/atrRatioClamped (added 2026-07-17): the last AtrBoundPullbackTicks ratio pair
                // at cancel time. raw < clamped means the AtrClampMin floor forced a wider pullback than
                // volatility warranted -- combined with missedByTicks this measures whether an unclamped
                // distance would have filled (drives the Clamp Band Reassess automation). Older rows
                // simply lack the columns; the dashboard parser treats them as unknown.
                string header = "time\tticker\tdirection\ttemplateNumber\tbarsPeriodType\tbarsPeriodValue\tlimitPrice\tpullbackTicks\tatr\twaitedMinutes\taccount\tmarketPriceAtPlacement\tmarketPriceAtCancel\tmissedByTicks\tatrRatioRaw\tatrRatioClamped";
                string line = string.Join("\t", new[] {
                    CurrentClockTime().ToString("o", CultureInfo.InvariantCulture),
                    ticker,
                    direction,
                    _activeTemplateNumber.ToString(CultureInfo.InvariantCulture),
                    barsPeriodType,
                    barsPeriodValue,
                    cancelledOrder.LimitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    PullbackTicks.ToString(CultureInfo.InvariantCulture),
                    atrValue.ToString("0.########", CultureInfo.InvariantCulture),
                    waitedMinutes.ToString("0.##", CultureInfo.InvariantCulture),
                    Account != null ? Account.Name : string.Empty,
                    marketPriceAtPlacement.ToString("0.########", CultureInfo.InvariantCulture),
                    marketPriceAtCancel.ToString("0.########", CultureInfo.InvariantCulture),
                    missedByTicks.ToString("0.##", CultureInfo.InvariantCulture),
                    _lastPullbackAtrRatioRaw.ToString("0.####", CultureInfo.InvariantCulture),
                    _lastPullbackAtrRatio.ToString("0.####", CultureInfo.InvariantCulture)
                });

                lock (noFillLogLock) {
                    if (!noFillLogHeaderChecked) {
                        if (!File.Exists(path) || new FileInfo(path).Length == 0)
                            File.WriteAllText(path, header + Environment.NewLine);
                        noFillLogHeaderChecked = true;
                    }

                    File.AppendAllText(path, line + Environment.NewLine);
                }
            }
            catch (Exception error) {
                D("No-fill TSV log failed: " + error.Message);
            }
        }

        // === Entry gate block log (evidence for the dashboard's Entry Gate Reassess card and
        // auto_apply_sizing.py's gate-widen automation) ===
        // Written on any entry-eligible bar where a setup trigger (TEMA/BB or TEMA/VWAP/MidBB cross)
        // fired but an indicator gate (StochRSI cross, MFI, RSI) blocked the entry -- one row per
        // blocking gate. gapPoints is the smallest widen of that gate's threshold that would have let
        // THIS bar pass (same units as the threshold); empty when no finite widen could have.
        private static readonly object gateBlockLogLock = new object();
        private bool gateBlockLogHeaderChecked;

        private string GateBlockLogPath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_gateblock_log.tsv");
        }

        private void LogGateBlocks(EntrySignals signals) {
            if (gateBlockLogBar == CurrentContextBar)
                return;

            bool longSetup = signals.NormalLongTemaCross || (EnableTemaVwapMidBbCrossEntry && signals.CrossLongCore);
            bool shortSetup = signals.NormalShortTemaCross || (EnableTemaVwapMidBbCrossEntry && signals.CrossShortCore);
            if (!longSetup && !shortSetup)
                return;

            bool logged = false;
            if (longSetup)
                logged |= LogGateBlocksForDirection(true, signals.LongStochPass);
            if (shortSetup)
                logged |= LogGateBlocksForDirection(false, signals.ShortStochPass);

            if (logged)
                gateBlockLogBar = CurrentContextBar;
        }

        private bool LogGateBlocksForDirection(bool isLong, bool stochPass) {
            bool logged = false;

            if (EnableStochRsiCrossFilter && !stochPass) {
                AppendGateBlockLog(isLong, "StochRSI", stochRsi != null ? stochRsi[0] : 0.0,
                    isLong ? StochRsiLowerLine : StochRsiUpperLine, StochGateGap(isLong));
                logged = true;
            }

            if (EnableMfiFilter && !MfiFilterPasses(isLong)) {
                AppendGateBlockLog(isLong, "MFI", mfi != null ? mfi[0] : 0.0,
                    isLong ? MfiLongMax : MfiShortMin, MfiGateGap(isLong));
                logged = true;
            }

            if (EnableRsiFilter && rsi != null && !RsiFilterPasses(isLong)) {
                AppendGateBlockLog(isLong, "RSI", rsi[0],
                    isLong ? RsiLongMax : RsiShortMin, RsiGateGap(isLong));
                logged = true;
            }

            return logged;
        }

        // Smallest MFI-points widen that would have passed this bar's MFI gate (checks the same
        // 0..MfiPriorBars window MfiFilterPasses does); -1 when the indicator isn't available.
        private double MfiGateGap(bool isLong) {
            if (mfi == null)
                return -1.0;

            int barsToCheck = Math.Min(Math.Max(0, MfiPriorBars), CurrentContextBar);
            double best = double.MaxValue;
            for (int barsAgo = 0; barsAgo <= barsToCheck; barsAgo++) {
                double gap = isLong ? mfi[barsAgo] - MfiLongMax : MfiShortMin - mfi[barsAgo];
                best = Math.Min(best, gap);
            }
            return best == double.MaxValue ? -1.0 : Math.Max(0.0, best);
        }

        private double RsiGateGap(bool isLong) {
            if (rsi == null)
                return -1.0;
            return Math.Max(0.0, isLong ? rsi[0] - RsiLongMax : RsiShortMin - rsi[0]);
        }

        // The StochRSI gate needs a CROSS (prior bar beyond the line, current bar back inside), so a
        // wider line can only manufacture a cross out of a bar pair already moving the right way. The
        // gap is the smallest line move that turns some rising (long) / falling (short) pair in the
        // lookback window into a cross; -1 when no pair qualifies (no finite widen passes this bar).
        private double StochGateGap(bool isLong) {
            if (stochRsi == null)
                return -1.0;

            int barsToCheck = Math.Min(Math.Max(0, StochRsiCrossLookbackBars), Math.Max(0, CurrentContextBar - 1));
            double best = double.MaxValue;
            for (int barsAgo = 0; barsAgo <= barsToCheck; barsAgo++) {
                int priorBarsAgo = barsAgo + 1;
                double prior = stochRsi[priorBarsAgo];
                double current = stochRsi[barsAgo];

                if (isLong && prior < current) {
                    // Need prior < line <= current: the widened lower line must sit just above prior.
                    double neededWiden = prior - StochRsiLowerLine + 0.001;
                    if (neededWiden > 0)
                        best = Math.Min(best, neededWiden);
                }
                else if (!isLong && prior > current) {
                    // Need prior > line >= current: the widened upper line must sit just below prior.
                    double neededWiden = StochRsiUpperLine - prior + 0.001;
                    if (neededWiden > 0)
                        best = Math.Min(best, neededWiden);
                }
            }
            return best == double.MaxValue ? -1.0 : best;
        }

        private void AppendGateBlockLog(bool isLong, string gate, double indicatorValue, double threshold, double gapPoints) {
            try {
                string ticker = ResolveTickerName();
                string direction = isLong ? "LONG" : "SHORT";

                string barsPeriodType = string.Empty;
                string barsPeriodValue = string.Empty;
                try {
                    if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                        barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                        barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                    }
                }
                catch {
                }

                string path = GateBlockLogPath();
                string header = "time\tticker\tdirection\ttemplateNumber\tbarsPeriodType\tbarsPeriodValue\tgate\tindicatorValue\tthreshold\tgapPoints\taccount";
                string line = string.Join("\t", new[] {
                    CurrentClockTime().ToString("o", CultureInfo.InvariantCulture),
                    ticker,
                    direction,
                    _activeTemplateNumber.ToString(CultureInfo.InvariantCulture),
                    barsPeriodType,
                    barsPeriodValue,
                    gate,
                    indicatorValue.ToString("0.####", CultureInfo.InvariantCulture),
                    threshold.ToString("0.####", CultureInfo.InvariantCulture),
                    gapPoints < 0 ? string.Empty : gapPoints.ToString("0.####", CultureInfo.InvariantCulture),
                    Account != null ? Account.Name : string.Empty
                });

                lock (gateBlockLogLock) {
                    if (!gateBlockLogHeaderChecked) {
                        if (!File.Exists(path) || new FileInfo(path).Length == 0)
                            File.WriteAllText(path, header + Environment.NewLine);
                        gateBlockLogHeaderChecked = true;
                    }

                    File.AppendAllText(path, line + Environment.NewLine);
                }
            }
            catch (Exception error) {
                D("Gate-block TSV log failed: " + error.Message);
            }
        }

        // === Stop-exit slippage log (evidence for the SlippageReserveRatio automation) ===
        // One row per completed stop-order exit: how far past the stop level the fill actually
        // landed, in ticks and dollars, alongside the ladder risk + reserve that were in effect.
        // Negative slippage (price improvement) is logged as-is. Market/limit exits (ML exit,
        // targets, manual) have no reference level to measure against and are not logged.
        private static readonly object slippageLogLock = new object();
        private bool slippageLogHeaderChecked;

        private string SlippageLogPath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_slippage_log.tsv");
        }

        private void AppendSlippageLog(Order stopOrder, double fillPrice, bool closedWasLong) {
            try {
                if (stopOrder == null || stopOrder.StopPrice <= 0 || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                    return;

                string ticker = ResolveTickerName();
                string direction = closedWasLong ? "LONG" : "SHORT";

                string barsPeriodType = string.Empty;
                string barsPeriodValue = string.Empty;
                try {
                    if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                        barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                        barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                    }
                }
                catch {
                }

                // Positive = filled worse than the stop level (real slippage cost); negative = improvement.
                double slippagePoints = closedWasLong ? stopOrder.StopPrice - fillPrice : fillPrice - stopOrder.StopPrice;
                double slippageTicks = slippagePoints / TickSize;
                int quantity = Math.Max(1, stopOrder.Quantity);
                double slippageDollars = slippagePoints * CurrentInstrument.MasterInstrument.PointValue * quantity;

                string path = SlippageLogPath();
                string header = "time\tticker\tdirection\ttemplateNumber\tbarsPeriodType\tbarsPeriodValue\tstopPrice\tfillPrice\tquantity\tslippageTicks\tslippageDollars\tladderRiskDollars\treserveDollars\taccount";
                string line = string.Join("\t", new[] {
                    CurrentClockTime().ToString("o", CultureInfo.InvariantCulture),
                    ticker,
                    direction,
                    _activeTemplateNumber.ToString(CultureInfo.InvariantCulture),
                    barsPeriodType,
                    barsPeriodValue,
                    stopOrder.StopPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    fillPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    quantity.ToString(CultureInfo.InvariantCulture),
                    slippageTicks.ToString("0.##", CultureInfo.InvariantCulture),
                    slippageDollars.ToString("0.##", CultureInfo.InvariantCulture),
                    LadderRiskDollars1R.ToString("0.##", CultureInfo.InvariantCulture),
                    DailyEntrySlippageDollars.ToString("0.##", CultureInfo.InvariantCulture),
                    Account != null ? Account.Name : string.Empty
                });

                lock (slippageLogLock) {
                    if (!slippageLogHeaderChecked) {
                        if (!File.Exists(path) || new FileInfo(path).Length == 0)
                            File.WriteAllText(path, header + Environment.NewLine);
                        slippageLogHeaderChecked = true;
                    }

                    File.AppendAllText(path, line + Environment.NewLine);
                }
            }
            catch (Exception error) {
                D("Slippage TSV log failed: " + error.Message);
            }
        }

        // === Post-cancel expire watch (evidence for the expire-minutes suggestion) ===
        // After CancelExpiredEntryOrder cancels an expired entry limit, watch whether the market later
        // trades THROUGH the old limit (ShadowFillThroughTicks, matching shadow-eval pessimism) within
        // ExpireWatchWindowMinutes of the original submission. TemaLimit_expire_log.tsv then answers
        // "would a longer EntryOrderExpireMinutes have turned this no-fill into a fill, and how many
        // minutes would it have needed?". There is one watch slot per context, so in a dense no-fill
        // streak the NEXT expire-cancel truncates the previous watch after only a few minutes -- the
        // minutesWatched column records how long each watch actually ran, and the server side only
        // counts a full-window untouched watch as "waiting longer wouldn't have helped" evidence.
        private const int ExpireWatchWindowMinutes = 60;
        private static readonly object expireLogLock = new object();
        private bool expireLogHeaderChecked;

        private string ExpireLogPath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_expire_log.tsv");
        }

        private void StartExpireWatch(Order cancelledOrder) {
            if (cancelledOrder == null || entryOrderSubmittedTime == DateTime.MinValue)
                return;

            // A new expire-cancel while the previous watch is still open: resolve the old one as
            // untouched first so its evidence isn't silently dropped.
            if (expireWatchLimitPrice > 0)
                AppendExpireLog(false, (CurrentClockTime() - expireWatchSubmittedTime).TotalMinutes);

            expireWatchIsLong = cancelledOrder.OrderAction == OrderAction.Buy;
            expireWatchLimitPrice = cancelledOrder.LimitPrice;
            expireWatchSubmittedTime = entryOrderSubmittedTime;
            expireWatchTemplateNumber = _activeTemplateNumber;
            expireWatchExpireMinutes = EntryOrderExpireMinutes;
        }

        private void ClearExpireWatch() {
            expireWatchLimitPrice = 0.0;
            expireWatchSubmittedTime = DateTime.MinValue;
        }

        private void ProcessPendingExpireWatch() {
            if (expireWatchLimitPrice <= 0 || expireWatchSubmittedTime == DateTime.MinValue)
                return;

            double minutesSinceSubmit = (CurrentClockTime() - expireWatchSubmittedTime).TotalMinutes;
            int idx = CurrentBarsInProgressIndex();
            // Same trade-through pessimism as the shadow fill check (was a hardcoded 1 tick, which
            // silently diverged whenever ShadowFillThroughTicks is changed from its default).
            double through = Math.Max(0, ShadowFillThroughTicks) * TickSize;
            bool touched = expireWatchIsLong
                ? Lows[idx][0] <= expireWatchLimitPrice - through
                : Highs[idx][0] >= expireWatchLimitPrice + through;

            if (touched) {
                AppendExpireLog(true, minutesSinceSubmit);
                ClearExpireWatch();
                return;
            }

            if (minutesSinceSubmit >= ExpireWatchWindowMinutes) {
                AppendExpireLog(false, minutesSinceSubmit);
                ClearExpireWatch();
            }
        }

        private void AppendExpireLog(bool touched, double minutesElapsed) {
            try {
                string ticker = ResolveTickerName();
                string direction = expireWatchIsLong ? "LONG" : "SHORT";

                string barsPeriodType = string.Empty;
                string barsPeriodValue = string.Empty;
                try {
                    if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                        barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                        barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                    }
                }
                catch {
                }

                string path = ExpireLogPath();
                // minutesWatched (was watchWindowMinutes, always the constant 60): how long this watch
                // actually ran -- a truncated watch (next expire-cancel took the slot) logs its real
                // elapsed minutes, so the server can tell it apart from a full-window untouched watch.
                string header = "time\tticker\tdirection\ttemplateNumber\tbarsPeriodType\tbarsPeriodValue\tlimitPrice\texpireMinutesUsed\ttouched\tminutesToTouch\tminutesWatched\taccount";
                string line = string.Join("\t", new[] {
                    CurrentClockTime().ToString("o", CultureInfo.InvariantCulture),
                    ticker,
                    direction,
                    expireWatchTemplateNumber.ToString(CultureInfo.InvariantCulture),
                    barsPeriodType,
                    barsPeriodValue,
                    expireWatchLimitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    expireWatchExpireMinutes.ToString(CultureInfo.InvariantCulture),
                    touched ? "1" : "0",
                    touched ? minutesElapsed.ToString("0.##", CultureInfo.InvariantCulture) : string.Empty,
                    minutesElapsed.ToString("0.##", CultureInfo.InvariantCulture),
                    Account != null ? Account.Name : string.Empty
                });

                lock (expireLogLock) {
                    if (!expireLogHeaderChecked) {
                        if (!File.Exists(path) || new FileInfo(path).Length == 0)
                            File.WriteAllText(path, header + Environment.NewLine);
                        expireLogHeaderChecked = true;
                    }

                    File.AppendAllText(path, line + Environment.NewLine);
                }
            }
            catch (Exception error) {
                D("Expire-watch TSV log failed: " + error.Message);
            }
        }

        private string ResolveTickerName(bool requireCurrentInstrument = true) {
            if (activeContext != null && activeContext.Symbol != string.Empty)
                return activeContext.Symbol;
            if (requireCurrentInstrument)
                return CurrentInstrument != null && CurrentInstrument.MasterInstrument != null ? CurrentInstrument.MasterInstrument.Name : Name;
            return CurrentInstrument.MasterInstrument != null ? CurrentInstrument.MasterInstrument.Name : Name;
        }

        private string ResolveAccountLabel() {
            return Account != null ? Account.Name : "Unknown";
        }
        private string ResolveMasterInstrumentSymbol() {
            return CurrentInstrument != null && CurrentInstrument.MasterInstrument != null ? CurrentInstrument.MasterInstrument.Name : string.Empty;
        }


        private double EffectiveMarkPrice(int bip, double referencePrice) {
            // Closes[bip][0] is the close of this series' own bar type (Volume/Renko/PointAndFigure/
            // etc.), which only advances when that synthetic bar completes -- for reversal-style bars
            // (PointAndFigure, Renko, Kagi, LineBreak) that can lag the real market by many points.
            // Bid/ask update on every quote regardless of bar type, so prefer their midpoint for any
            // "what's the price right now" display (dashboard mark/PnL), falling back to the bar
            // close only when no live quote is available (e.g. backtest).
            double bid = GetCurrentBid(bip);
            double ask = GetCurrentAsk(bip);
            double candidate;
            if (bid > 0 && ask > 0) candidate = (bid + ask) / 2.0;
            else if (bid > 0) candidate = bid;
            else if (ask > 0) candidate = ask;
            else candidate = Closes[bip][0];

            return SanitizeMarkPrice(candidate, referencePrice, bip);
        }

        // Same "implausible value" class the 2026-07-17 excursion clamp (see SanitizePointsExcursion
        // above) was added for: a stale/cross-context bid or ask can briefly hand back a mark price tens
        // of points from reality, which flows straight into the dashboard's displayed unrealized P&L
        // (e.g. an ES trade showing +$2000 unrealized before settling at its real ~+$100 close). Reject
        // any candidate more than MaxPlausiblePointsExcursion points from a known-good reference price,
        // falling back to the bar close and finally the reference price itself.
        private double SanitizeMarkPrice(double candidate, double referencePrice, int bip) {
            if (referencePrice <= 0 || Math.Abs(candidate - referencePrice) <= MaxPlausiblePointsExcursion)
                return candidate;

            double barClose = Closes[bip][0];
            if (barClose > 0 && Math.Abs(barClose - referencePrice) <= MaxPlausiblePointsExcursion) {
                if (ShouldPrintMlHttpError())
                    D("Implausible mark price clamped: candidate=" + candidate.ToString("0.##", CultureInfo.InvariantCulture) + " referencePrice=" + referencePrice.ToString("0.##", CultureInfo.InvariantCulture) + "; falling back to bar close=" + barClose.ToString("0.##", CultureInfo.InvariantCulture));
                return barClose;
            }

            if (ShouldPrintMlHttpError())
                D("Implausible mark price clamped: candidate=" + candidate.ToString("0.##", CultureInfo.InvariantCulture) + " barClose=" + barClose.ToString("0.##", CultureInfo.InvariantCulture) + " referencePrice=" + referencePrice.ToString("0.##", CultureInfo.InvariantCulture) + "; falling back to referencePrice");
            return referencePrice;
        }

        private string OpenTradeStatusFileName() {
            return OpenTradeStatusExporter.FileName("TemaLimit", ResolveTickerName() + "_" + ResolveAccountLabel());
        }

        private void WriteOpenTradeStatus() {
            MarketPosition position = EffectiveMarketPosition();
            if (position == MarketPosition.Flat || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null) {
                ClearOpenTradeStatus();
                return;
            }

            // Cross-check the real account position against the watchdog flag so the dashboard never shows a zombie trade.
            Position realPosition = GetAccountPositionForInstrument();
            if (realPosition == null || realPosition.MarketPosition == MarketPosition.Flat || realPosition.Quantity <= 0) {
                // Account went flat under us (manual flatten from the Positions/Orders tab, or an
                // NT-issued Close) -- log the completed trade before tearing the state down, or it
                // never reaches the dashboard TSV at all.
                LogManualFlattenIfNeeded(position);
                ResetWatchdogState();
                ClearOpenTradeStatus();
                return;
            }

            // Healthy account position again -- re-arm manual-flatten logging for this ticker+account.
            manualFlattenLoggedKeys.Remove(ManualExitTickerAccountKey());
            manualFlattenLoggedMarkExit.Remove(ManualExitTickerAccountKey());

            double openEntryPrice = EffectiveEntryPrice();
            if (openEntryPrice <= 0)
                return;

            int bip = CurrentBarsInProgressIndex();
            double currentPrice = EffectiveMarkPrice(bip, openEntryPrice);
            string direction = position == MarketPosition.Long ? "LONG" : "SHORT";
            int barsHeld = entryBar == int.MinValue ? 0 : CurrentContextBar - entryBar;
            string row = OpenTradeStatusExporter.Row(Times[bip][0], Name, ResolveTickerName(false), direction, EffectiveQuantity(),
                openEntryPrice, currentPrice, EffectiveUnrealizedPnl(currentPrice), barsHeld, EntrySignalForPosition(), Account != null ? Account.Name : string.Empty, activeMlReversal, _activeTemplateNumber, entryFillTime,
                CurrentBarsPeriod.BarsPeriodType.ToString(), CurrentBarsPeriod.Value.ToString(CultureInfo.InvariantCulture));
            OpenTradeStatusExporter.Write(OpenTradeStatusFileName(), row);
        }

        private void ClearOpenTradeStatus() {
            string fileName = OpenTradeStatusFileName();
            ReconcileStaleOpenTradeFile(fileName);
            OpenTradeStatusExporter.Clear(fileName);
        }

        // Manual flatten while this instance is alive: the account is flat but our tracked position
        // isn't. Logs the trade once (keyed per ticker+account) using the current mark price as the
        // exit. If a strategy-owned exit fill is actually in flight (fill raced ahead of the
        // execution callback), OnExecutionUpdateCore sees the key and skips its own dashboard/PnL
        // log so the trade can't appear twice.
        private void LogManualFlattenIfNeeded(MarketPosition position) {
            if (position == MarketPosition.Flat)
                return;

            double openEntryPrice = EffectiveEntryPrice();
            if (openEntryPrice <= 0)
                return;

            if (!manualFlattenLoggedKeys.Add(ManualExitTickerAccountKey()))
                return;

            bool wasLong = position == MarketPosition.Long;
            int bip = CurrentBarsInProgressIndex();
            double exitPrice = EffectiveMarkPrice(bip, openEntryPrice);
            // AppendDashboardTradeOutcome/AccumulateDailyRealizedPnL read the entryPrice field; on a
            // restart-recovered position only the watchdog copy may be populated.
            if (entryPrice <= 0)
                entryPrice = openEntryPrice;

            Print(OutputTimePrefix() + "MANUAL FLATTEN " + OutputContext() + ": account flat under tracked "
                + (wasLong ? "LONG" : "SHORT") + " -- logging trade with mark exit " + exitPrice);
            manualFlattenLoggedMarkExit[ManualExitTickerAccountKey()] = exitPrice;
            AppendDashboardTradeOutcome(exitPrice, EffectiveQuantity(), "ManualFlatten", Times[bip][0], wasLong);
            AccumulateDailyRealizedPnL(exitPrice, EffectiveQuantity(), wasLong);
            // The ML training log needs this close too. The 2026-07-19 work wired
            // external closes into the dashboard TSV, P&L and the template ledger,
            // but NOT into exit_samples_*.tsv -- LogMlExitFinalSample was only ever
            // called from the isExitFill branch of OnExecutionUpdate. So every
            // manual/external flatten produced a trade record with no terminal
            // label-0 row, and the trade's whole sample sequence read as
            // "never exits". Found 2026-07-20 via the exit_label_integrity check:
            // 13 ManualFlatten closes that day, and the flagged trades matched
            // them to the second. LogMlExitFinalSample dedupes internally, so the
            // later exit-fill callback (if it wins the race) is a no-op here.
            LogMlExitFinalSample(exitPrice, "ManualFlatten");
            // External closes must hit the modes-4/5 ledger too, or templates that only ever exit via manual
            // flatten rank on stale dollars (observed: Simtema_NQ ledger missing a +367.50 T38 close). If a
            // strategy exit fill later wins the race, CorrectManualFlattenLog nets the mark-vs-fill gap.
            if (CurrentInstrument != null && CurrentInstrument.MasterInstrument != null && entryPrice > 0) {
                double flattenPoints = wasLong ? exitPrice - entryPrice : entryPrice - exitPrice;
                MarkTemplateRealized(_activeTemplateNumber,
                    flattenPoints * CurrentInstrument.MasterInstrument.PointValue * Math.Max(1, EffectiveQuantity()));
            }
        }

        // A strategy-owned exit fill landed after LogManualFlattenIfNeeded already logged the trade at
        // the mark: the trade wasn't a manual flatten at all, just a fill whose execution callback lost
        // the race to the account-flat callback. Rewrite the ManualFlatten row in the dashboard TSV to
        // the real exit signal/price/PnL and true-up dailyRealizedPnLDollars for the mark-vs-fill gap.
        private void CorrectManualFlattenLog(double fillPrice, int quantity, string exitSignal, bool wasLong) {
            double markExit;
            string key = ManualExitTickerAccountKey();
            if (!manualFlattenLoggedMarkExit.TryGetValue(key, out markExit))
                return;
            manualFlattenLoggedMarkExit.Remove(key);

            if (CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            int tradeQuantity = Math.Max(1, quantity);
            double pointValue = CurrentInstrument.MasterInstrument.PointValue;
            // wasLong => higher exit is better; the manual-flatten log already added the mark-based PnL.
            double deltaPoints = wasLong ? fillPrice - markExit : markExit - fillPrice;
            dailyRealizedPnLDollars += deltaPoints * pointValue * tradeQuantity;
            // The template ledger was also marked at the mark price -- net it to the true fill.
            MarkTemplateRealized(_activeTemplateNumber, deltaPoints * pointValue * tradeQuantity);

            try {
                string path = DashboardTradeLogPath();
                // Same ticker resolution as AppendDashboardTradeOutcome so the row lookup matches what it wrote.
                string ticker = (activeContext != null && activeContext.Symbol != string.Empty)
                    ? activeContext.Symbol
                    : (CurrentInstrument.MasterInstrument != null ? CurrentInstrument.MasterInstrument.Name : Name);
                string accountLabel = Account != null ? Account.Name : string.Empty;
                lock (dashboardTradeLogLock) {
                    if (!File.Exists(path))
                        return;
                    string[] lines = File.ReadAllLines(path);
                    // Newest matching ManualFlatten row for this ticker+account is the one we just wrote.
                    for (int i = lines.Length - 1; i >= 1; i--) {
                        string[] cols = lines[i].Split('\t');
                        if (cols.Length < 9)
                            continue;
                        if (cols[8] != "ManualFlatten" || cols[1] != ticker || cols[cols.Length - 1] != accountLabel)
                            continue;
                        double rowEntryPrice;
                        if (!double.TryParse(cols[3], NumberStyles.Float, CultureInfo.InvariantCulture, out rowEntryPrice))
                            rowEntryPrice = entryPrice;
                        double points = wasLong ? fillPrice - rowEntryPrice : rowEntryPrice - fillPrice;
                        double dollars = points * pointValue * tradeQuantity;
                        cols[4] = fillPrice.ToString("0.########", CultureInfo.InvariantCulture);
                        cols[6] = dollars.ToString("0.########", CultureInfo.InvariantCulture);
                        cols[7] = dollars > 0 ? "WIN" : dollars < 0 ? "LOSS" : "FLAT";
                        cols[8] = exitSignal ?? "ManualFlatten";
                        lines[i] = string.Join("\t", cols);
                        File.WriteAllLines(path, lines);
                        Print(OutputTimePrefix() + "MANUAL FLATTEN CORRECTED " + OutputContext() + ": exit fill '"
                            + cols[8] + "' @ " + fillPrice + " replaced mark " + markExit);
                        break;
                    }
                }
            }
            catch (Exception error) {
                D("ManualFlatten correction failed: " + error.Message);
            }
        }

        // Startup recovery for the NT-crash case: a previous instance died holding a position and the
        // user flattened it while no strategy code was running, leaving a stale row in the open-trades
        // file with no completed-trade log anywhere. Runs once per file, only for rows whose entryTime
        // predates this instance's realtime start; exit price is the current mark (the true fill price
        // is unknowable here).
        private void ReconcileStaleOpenTradeFile(string fileName) {
            if (!reconciledOpenTradeFiles.Add(fileName) || realtimeStartTime == DateTime.MinValue)
                return;

            try {
                string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, fileName);
                if (!File.Exists(path))
                    return;

                string[] lines = File.ReadAllLines(path);
                if (lines.Length < 2 || string.IsNullOrWhiteSpace(lines[1]))
                    return;

                string[] f = lines[1].Split('\t');
                if (f.Length < 14)
                    return;

                double staleEntryPrice;
                if (!double.TryParse(f[5], System.Globalization.NumberStyles.Float, CultureInfo.InvariantCulture, out staleEntryPrice) || staleEntryPrice <= 0)
                    return;

                DateTime rowEntryTime;
                if (!DateTime.TryParse(f[13], CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.RoundtripKind, out rowEntryTime))
                    return;

                // Rows from this session belong to a live trade whose exit the normal fill path logs.
                if (rowEntryTime >= realtimeStartTime)
                    return;

                // The position is still open on the account (restart-recovery case, nothing was
                // flattened) -- don't fabricate a close for it.
                Position accountPosition = GetAccountPositionForInstrument();
                if (accountPosition != null && accountPosition.MarketPosition != MarketPosition.Flat && accountPosition.Quantity > 0)
                    return;

                int qty;
                if (!int.TryParse(f[4], System.Globalization.NumberStyles.Integer, CultureInfo.InvariantCulture, out qty) || qty < 1)
                    qty = 1;

                bool wasLong = string.Equals(f[3], "LONG", StringComparison.OrdinalIgnoreCase);
                double exitPrice = EffectiveMarkPrice(CurrentBarsInProgressIndex(), staleEntryPrice);

                Print(OutputTimePrefix() + "RECONCILE " + OutputContext() + ": stale open trade from " + f[13]
                    + " has no logged exit (crash/manual flatten while strategy was down) -- logging as ManualFlatten at mark " + exitPrice);
                AppendReconciledDashboardTradeOutcome(f[2], wasLong, staleEntryPrice, exitPrice, qty,
                    f.Length > 9 ? f[9] : string.Empty, f.Length > 12 ? f[12] : "0", rowEntryTime,
                    f.Length > 11 && string.Equals(f[11], "true", StringComparison.OrdinalIgnoreCase),
                    f.Length > 14 ? f[14] : string.Empty, f.Length > 15 ? f[15] : string.Empty);

                // Reconciled closes count toward the modes-4/5 ledger too -- use the stale row's own template,
                // not _activeTemplateNumber (this instance may have rotated since the dead one entered).
                int staleTemplate;
                if (f.Length > 12 && int.TryParse(f[12], NumberStyles.Integer, CultureInfo.InvariantCulture, out staleTemplate)
                    && staleTemplate > 0 && CurrentInstrument != null && CurrentInstrument.MasterInstrument != null) {
                    double stalePoints = wasLong ? exitPrice - staleEntryPrice : staleEntryPrice - exitPrice;
                    MarkTemplateRealized(staleTemplate, stalePoints * CurrentInstrument.MasterInstrument.PointValue * qty);
                }
            }
            catch (Exception error) {
                D("Open-trade reconcile failed for " + fileName + ": " + error.Message);
            }
        }

        private string PendingTradeStatusFileName() {
            return PendingTradeStatusExporter.FileName("TemaLimit", ResolveTickerName() + "_" + ResolveAccountLabel());
        }

        private void WritePendingTradeStatus() {
            if (entryOrder == null || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null) {
                ClearPendingTradeStatus();
                return;
            }

            int bip = CurrentBarsInProgressIndex();
            double currentPrice = EffectiveMarkPrice(bip, entryOrder.LimitPrice);
            string direction = entryOrder.OrderAction == OrderAction.Buy ? "LONG" : "SHORT";
            string row = PendingTradeStatusExporter.Row(Times[bip][0], Name, ResolveTickerName(false), direction, entryOrder.Quantity,
                entryOrder.LimitPrice, currentPrice, Account != null ? Account.Name : string.Empty, _activeTemplateNumber, entryOrderSubmittedTime,
                CurrentBarsPeriod.BarsPeriodType.ToString(), CurrentBarsPeriod.Value.ToString(CultureInfo.InvariantCulture));
            PendingTradeStatusExporter.Write(PendingTradeStatusFileName(), row);
        }

        private void ClearPendingTradeStatus() {
            PendingTradeStatusExporter.Clear(PendingTradeStatusFileName());
        }

        private void ExportPullbackState() {
            string tickerName = ResolveTickerName(false);
            int templateNumber = _activeTemplateNumber;
            // Pure compute: this runs every realtime tick for the dashboard's live pullback card, and
            // routing it through AtrBoundPullbackTicks used to overwrite the _lastPullback* snapshot
            // (the ratio pair actually used for the placed order) with whatever ATR looked like right now.
            double liveAtr, liveAtrAvg, liveRatioRaw, liveRatioClamped;
            int livePullbackTicks = ComputeAtrBoundPullbackTicks(tickerName, templateNumber, out liveAtr, out liveAtrAvg, out liveRatioRaw, out liveRatioClamped);
            int basePullbackTicks = PullbackTicksForTicker(tickerName, templateNumber);
            DateTime barTime = Times[CurrentBarsInProgressIndex()][0];

            string barsPeriodType = string.Empty;
            string barsPeriodValue = string.Empty;
            try {
                if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                    barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                    barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                }
            }
            catch {
            }

            PullbackStateExporter.Update(barTime, tickerName, barsPeriodType, barsPeriodValue, liveAtr, liveAtrAvg, liveRatioClamped, basePullbackTicks, livePullbackTicks);
        }

        private string ManualExitTickerAccountKey() {
            return ResolveTickerName() + "_" + ResolveAccountLabel();
        }


        // Checks whether the dashboard requested a manual flatten (POST /api/exit) and submits the exit if so.
        private void CheckManualExitRequest() {
            if (EffectiveMarketPosition() == MarketPosition.Flat)
                return;

            if (!ManualExitCommand.ConsumeIfRequested("TemaLimit", ManualExitTickerAccountKey()))
                return;

            Print(OutputTimePrefix() + "MANUAL EXIT REQUESTED " + OutputContext() + ": exiting from dashboard command");

            MarketPosition position = EffectiveMarketPosition();
            if (position == MarketPosition.Long)
                SubmitGuardedExitLong("ManualExit", EntrySignalForPosition());
            else if (position == MarketPosition.Short)
                SubmitGuardedExitShort("ManualExit", EntrySignalForPosition());
        }

        // Checks whether the dashboard requested cancellation of the working entry order (POST /api/cancel).
        private void CheckManualCancelRequest() {
            if (!CanRequestEntryCancel())
                return;

            if (!ManualCancelCommand.ConsumeIfRequested("TemaLimit", ManualExitTickerAccountKey()))
                return;

            Print(OutputTimePrefix() + "MANUAL CANCEL REQUESTED " + OutputContext() + ": cancelling " + entryOrder.Name + " from dashboard command");

            AppendNoFillLog(entryOrder);
            CancelOrder(entryOrder);
            entryOrderSubmittedTime = DateTime.MinValue;
        }

        // Gross P&L for this instrument's own fill (no commission); not SystemPerformance-based to avoid cross-symbol PnL bleed.
        private void AccumulateDailyRealizedPnL(double exitPrice, int quantity, bool wasLong) {
            if (entryPrice <= 0 || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            double points = wasLong ? exitPrice - entryPrice : entryPrice - exitPrice;
            int tradeQuantity = Math.Max(1, quantity);
            dailyRealizedPnLDollars += points * CurrentInstrument.MasterInstrument.PointValue * tradeQuantity;
        }

        // reversal/entryTime/account must stay the last 3 columns; the dashboard reads them by negative index across all strategy logs.
        private const string DashboardTradeLogHeader = "time\tticker\tdirection\tentryPrice\texitPrice\tquantity\tpnl\toutcome\texitSignal\tentrySignal\tprediction\ttrigger"
            + "\ttemplateNumber\tbarsPeriodType\tbarsPeriodValue\tmfiLongMax\tmfiShortMin\trsiLongMax\trsiShortMin\tstochRsiLongMax\tstochRsiShortMin\tpullbackTicks\tmlExitHoldThreshold\tminBarsBeforeMlExit\tminUnrealizedRForMlExit"
            + "\tpullbackAtr\tpullbackAtrAvg\tpullbackAtrRatio"
            + "\treversal\tentryTime\taccount";

        // Caller must hold dashboardTradeLogLock.
        private void EnsureDashboardTradeLogHeader(string path) {
            if (dashboardTradeLogHeaderChecked)
                return;

            if (!File.Exists(path) || new FileInfo(path).Length == 0) {
                File.WriteAllText(path, DashboardTradeLogHeader + Environment.NewLine);
            }
            else {
                // Upgrade the log header once per instance for older files predating the template columns.
                try {
                    string[] existingLines = File.ReadAllLines(path);
                    if (existingLines.Length > 0 && existingLines[0] != DashboardTradeLogHeader) {
                        existingLines[0] = DashboardTradeLogHeader;
                        File.WriteAllLines(path, existingLines);
                    }
                }
                catch {
                }
            }

            dashboardTradeLogHeaderChecked = true;
        }

        // Startup-reconcile sibling of AppendDashboardTradeOutcome: everything comes from the stale
        // open-trades row instead of live instance fields (which are already reset by the time this
        // runs), and the ML/indicator columns are left blank -- that trade's context died with the
        // crashed instance.
        private void AppendReconciledDashboardTradeOutcome(string ticker, bool wasLong, double staleEntryPrice, double exitPrice, int quantity,
            string entrySignal, string templateNumber, DateTime rowEntryTime, bool reversal, string barsPeriodType, string barsPeriodValue) {
            if (CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            try {
                double points = wasLong ? exitPrice - staleEntryPrice : staleEntryPrice - exitPrice;
                int tradeQuantity = Math.Max(1, quantity);
                double dollars = points * CurrentInstrument.MasterInstrument.PointValue * tradeQuantity;
                string outcome = dollars > 0 ? "WIN" : dollars < 0 ? "LOSS" : "FLAT";
                string line = string.Join("\t", new[] {
                    CurrentClockTime().ToString("o", CultureInfo.InvariantCulture),
                    string.IsNullOrEmpty(ticker) ? CurrentInstrument.MasterInstrument.Name : ticker,
                    wasLong ? "LONG" : "SHORT",
                    staleEntryPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    exitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    tradeQuantity.ToString(CultureInfo.InvariantCulture),
                    dollars.ToString("0.########", CultureInfo.InvariantCulture),
                    outcome,
                    "ManualFlatten",
                    entrySignal ?? string.Empty,
                    string.Empty,
                    string.Empty,
                    string.IsNullOrEmpty(templateNumber) ? "0" : templateNumber,
                    barsPeriodType ?? string.Empty,
                    barsPeriodValue ?? string.Empty,
                    string.Empty, string.Empty, string.Empty, string.Empty, string.Empty, string.Empty,
                    string.Empty, string.Empty, string.Empty, string.Empty,
                    string.Empty, string.Empty, string.Empty,
                    reversal ? "true" : "false",
                    rowEntryTime.ToString("o", CultureInfo.InvariantCulture),
                    Account != null ? Account.Name : string.Empty
                });

                lock (dashboardTradeLogLock) {
                    EnsureDashboardTradeLogHeader(DashboardTradeLogPath());
                    File.AppendAllText(DashboardTradeLogPath(), line + Environment.NewLine);
                }
            }
            catch (Exception error) {
                D("Reconciled dashboard trade TSV log failed: " + error.Message);
            }
        }

        private void AppendDashboardTradeOutcome(double exitPrice, int quantity, string exitSignal, DateTime fillTime, bool wasLong) {
            if (entryPrice <= 0 || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            try {
                bool isLong = wasLong;
                double points = isLong ? exitPrice - entryPrice : entryPrice - exitPrice;
                int tradeQuantity = Math.Max(1, quantity);
                double dollars = points * CurrentInstrument.MasterInstrument.PointValue * tradeQuantity;
                string outcome = dollars > 0 ? "WIN" : dollars < 0 ? "LOSS" : "FLAT";
                string direction = isLong ? "LONG" : "SHORT";
                string path = DashboardTradeLogPath();
                string barsPeriodType = string.Empty;
                string barsPeriodValue = string.Empty;
                try {
                    if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null) {
                        barsPeriodType = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
                        barsPeriodValue = BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value.ToString(CultureInfo.InvariantCulture);
                    }
                }
                catch {
                }
                // Use activeContext's symbol in multi-symbol mode for consistency.
                string ticker = (activeContext != null && activeContext.Symbol != string.Empty)
                    ? activeContext.Symbol
                    : (CurrentInstrument.MasterInstrument != null ? CurrentInstrument.MasterInstrument.Name : Name);
                string line = string.Join("\t", new[] {
                    fillTime.ToString("o", CultureInfo.InvariantCulture),
                    ticker,
                    direction,
                    entryPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    exitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    tradeQuantity.ToString(CultureInfo.InvariantCulture),
                    dollars.ToString("0.########", CultureInfo.InvariantCulture),
                    outcome,
                    exitSignal ?? string.Empty,
                    activeEntrySignal ?? string.Empty,
                    activeMlPrediction ?? string.Empty,
                    activeMlTrigger ?? string.Empty,
                    _activeTemplateNumber.ToString(CultureInfo.InvariantCulture),
                    barsPeriodType,
                    barsPeriodValue,
                    MfiLongMax.ToString("0.##", CultureInfo.InvariantCulture),
                    MfiShortMin.ToString("0.##", CultureInfo.InvariantCulture),
                    RsiLongMax.ToString("0.##", CultureInfo.InvariantCulture),
                    RsiShortMin.ToString("0.##", CultureInfo.InvariantCulture),
                    StochRsiLowerLine.ToString("0.##", CultureInfo.InvariantCulture),
                    StochRsiUpperLine.ToString("0.##", CultureInfo.InvariantCulture),
                    PullbackTicks.ToString(CultureInfo.InvariantCulture),
                    MlExitHoldThreshold.ToString("0.##", CultureInfo.InvariantCulture),
                    MinBarsBeforeMlExit.ToString(CultureInfo.InvariantCulture),
                    MinUnrealizedRForMlExit.ToString("0.##", CultureInfo.InvariantCulture),
                    _lastPullbackAtr.ToString("0.########", CultureInfo.InvariantCulture),
                    _lastPullbackAtrAvg.ToString("0.########", CultureInfo.InvariantCulture),
                    _lastPullbackAtrRatio.ToString("0.###", CultureInfo.InvariantCulture),
                    activeMlReversal ? "true" : "false",
                    entryFillTime > DateTime.MinValue ? entryFillTime.ToString("o", CultureInfo.InvariantCulture) : string.Empty,
                    Account != null ? Account.Name : string.Empty
                });

                lock (dashboardTradeLogLock) {
                    EnsureDashboardTradeLogHeader(path);
                    File.AppendAllText(path, line + Environment.NewLine);
                }

                LogLiveTemplateSample(exitPrice, quantity, fillTime, wasLong);
            }
            catch (Exception error) {
                D("Dashboard trade TSV log failed: " + error.Message);
            }
        }

        // Live sibling of LogTemplateShadowSample: same endpoint/base fields, shadow=false plus realized-outcome extras.
        private void LogLiveTemplateSample(double exitPrice, int quantity, DateTime resolvedTime, bool wasLong) {
            try {
                double points = wasLong ? exitPrice - entryPrice : entryPrice - exitPrice;
                double pointValue = CurrentInstrument.MasterInstrument.PointValue;
                double dollars = points * pointValue * Math.Max(1, quantity);
                // Initial-risk points, not oneRPoints (that's the profit-locking ladder step derived
                // from LadderRiskDollars1R) -- matches the entry-time calc in OnExecutionUpdateCore and
                // LogTemplateShadowSample's TemplateRiskDollars1R-derived risk, so live and shadow
                // r_multiple values are directly comparable.
                double liveRiskPoints = Math.Max(1, Math.Round(RiskDollars1R / (pointValue * Math.Max(1, quantity)) / TickSize, MidpointRounding.AwayFromZero)) * TickSize;
                double rMultiple = liveRiskPoints > 0 ? points / liveRiskPoints : 0.0;
                TemplateParams p = GetTemplateParams(_activeTemplateNumber);
                DateTime setupTime = activeSetupTimestamp > DateTime.MinValue ? activeSetupTimestamp : entryFillTime;
                string setupDirection = !string.IsNullOrEmpty(activeMlSetupDirection)
                    ? activeMlSetupDirection
                    : (wasLong ? "long" : "short");
                int barsHeld = entryBar != int.MinValue ? Math.Max(0, CurrentContextBar - entryBar) : 0;

                string payload = new JsonWriter()
                    .Str("symbol", CurrentInstrument.MasterInstrument.Name)
                    .Str("trigger", activeMlTrigger ?? string.Empty)
                    .Str("setup_timestamp", setupTime.ToString("o", CultureInfo.InvariantCulture))
                    .Str("resolved_timestamp", resolvedTime.ToString("o", CultureInfo.InvariantCulture))
                    .Raw("template_number", _activeTemplateNumber.ToString(CultureInfo.InvariantCulture))
                    .Raw("selectivity", FormatJsonDouble(p.Selectivity))
                    .Str("setup_direction", setupDirection)
                    .Raw("r_multiple", FormatJsonDouble(rMultiple))
                    .Raw("dollars", FormatJsonDouble(dollars))
                    .Raw("shadow", "false")
                    .Str("bars_period", CurrentBarsPeriod != null ? CurrentBarsPeriod.ToString() : string.Empty)
                    .Raw("entry_price", FormatJsonDouble(entryPrice))
                    .Raw("exit_price", FormatJsonDouble(exitPrice))
                    .Raw("mfe_points", FormatJsonDouble(maxFavorableExcursionPoints))
                    .Raw("mae_points", FormatJsonDouble(maxAdverseExcursionPoints))
                    .Raw("bars_held", barsHeld.ToString(CultureInfo.InvariantCulture))
                    // Boolean, unlike AppendDashboardTradeOutcome's three-way WIN/LOSS/FLAT outcome above --
                    // an exact-breakeven close (dollars == 0) counts as not-a-win here.
                    .Raw("win", dollars > 0 ? "true" : "false")
                    .Raw("window", string.IsNullOrEmpty(activeMlWindowJson) ? "[]" : activeMlWindowJson)
                    .ToString();

                FireAndForgetPostJson(TrimTrailingSlash(MlServiceUrl) + "/log-template-sample", payload);
            }
            catch (Exception error) {
                if (ShouldPrintMlHttpError()) D("Template live sample log failed: " + error.Message);
            }
        }

        private bool ShouldPrintMlHttpError() {
            DateTime now = DateTime.UtcNow;
            lock (mlHttpErrorPrintLock) {
                if ((now - lastMlHttpErrorPrintUtc).TotalSeconds < MlHttpErrorPrintThrottleSeconds)
                    return false;

                lastMlHttpErrorPrintUtc = now;
                return true;
            }
        }
        private string PostJson(string url, string json) {
            byte[] body = Encoding.UTF8.GetBytes(json);
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = "POST";
            request.ContentType = "application/json";
            request.Timeout = Math.Max(100, MlHttpTimeoutMs);
            request.ReadWriteTimeout = Math.Max(100, MlHttpTimeoutMs);
            request.ContentLength = body.Length;

            using (Stream stream = request.GetRequestStream())
                stream.Write(body, 0, body.Length);

            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                return reader.ReadToEnd();
        }

        private string ExtractJsonString(string json, string key) {
            int start = FindJsonValueOffset(json, key);
            if (start < 0)
                return string.Empty;
            start = json.IndexOf('"', start);
            if (start < 0)
                return string.Empty;
            int end = json.IndexOf('"', start + 1);
            if (end < 0)
                return string.Empty;
            return json.Substring(start + 1, end - start - 1).ToLowerInvariant();
        }


        private double ExtractJsonDouble(string json, string key) {
            int start = FindJsonValueOffset(json, key);
            if (start < 0)
                return 0.0;
            int end = start;
            while (end < json.Length && "0123456789+-.eE".IndexOf(json[end]) >= 0)
                end++;
            double value;
            return double.TryParse(json.Substring(start, end - start), NumberStyles.Float, CultureInfo.InvariantCulture, out value)
                ? value
                : 0.0;
        }


        private static string JsonEscape(string value) {
            return (value ?? string.Empty).Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private string TrimTrailingSlash(string value) {
            return string.IsNullOrWhiteSpace(value) ? "http://localhost:8765" : value.TrimEnd('/');
        }

        private string FormatJsonDouble(double value) {
            if (double.IsNaN(value) || double.IsInfinity(value))
                value = 0.0;
            return value.ToString("0.########", CultureInfo.InvariantCulture);
        }


        private string GetJson(string url) {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = "GET";
            request.Timeout = Math.Max(100, MlHttpTimeoutMs);
            request.ReadWriteTimeout = Math.Max(100, MlHttpTimeoutMs);

            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                return reader.ReadToEnd();
        }

        private int ExtractJsonInt(string json, string key, int defaultValue) {
            int start = FindJsonValueOffset(json, key);
            if (start < 0)
                return defaultValue;
            int end = start;
            while (end < json.Length && "0123456789+-".IndexOf(json[end]) >= 0)
                end++;
            int value;
            return int.TryParse(json.Substring(start, end - start), NumberStyles.Integer, CultureInfo.InvariantCulture, out value)
                ? value
                : defaultValue;
        }


        private bool ExtractJsonBool(string json, string key) {
            int start = FindJsonValueOffset(json, key);
            if (start < 0)
                return false;
            while (start < json.Length && char.IsWhiteSpace(json[start]))
                start++;
            return json.IndexOf("true", start, StringComparison.OrdinalIgnoreCase) == start;
        }


        private double SafeMlExitValue(double value) {
            return double.IsNaN(value) || double.IsInfinity(value) ? 0.0 : value;
        }

        private double ClampMlExit(double value, double min, double max) {
            if (value < min)
                return min;
            if (value > max)
                return max;
            return value;
        }

        private bool MlExitTrackingValid() {
            return !string.IsNullOrEmpty(_exitTradeId)
                && _exitEntryPrice > 0
                && _exitOneRPoints >= TickSize
                && (_exitDirection == "long" || _exitDirection == "short");
        }

        private bool MlExitSampleLoggingActive() {
            return EnableMlExitSampleLogging || _mlExitRecommendedPhase >= 1;
        }

        private bool MlExitRecommendationsActive() {
            return EnableMlExitRecommendations || _mlExitRecommendedPhase >= 2;
        }

        private string MlExitDataSeriesTypeName() {
            try {
                if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null)
                    return BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.BarsPeriodType.ToString();
            }
            catch {
            }

            return "unknown";
        }

        private double MlExitDataSeriesValue() {
            try {
                if (BarsArray != null && BarsArray.Length > 0 && BarsArray[CurrentBarsInProgressIndex()] != null && BarsArray[CurrentBarsInProgressIndex()].BarsPeriod != null)
                    return BarsArray[CurrentBarsInProgressIndex()].BarsPeriod.Value;
            }
            catch {
            }

            return 0.0;
        }

        private double MlExitDataSeriesTypeFeature(string typeName) {
            string text = (typeName ?? string.Empty).Trim().ToLowerInvariant();
            if (text == "tick")
                return -0.75;
            if (text == "minute")
                return -0.25;
            if (text == "second")
                return -0.50;
            if (text == "range")
                return 0.25;
            if (text == "day")
                return 0.50;
            if (text == "volume")
                return 0.75;
            return 0.0;
        }

        private double MlExitDataSeriesValueFeature(double value) {
            double raw = Math.Max(1.0, value);
            return ClampMlExit(Math.Log10(raw) / 4.0, 0.0, 1.0);
        }

        private double MlExitSymbolHashFeature(string symbol) {
            unchecked {
                uint h = 17;
                string text = (symbol ?? string.Empty).ToUpperInvariant();
                for (int i = 0; i < text.Length; i++)
                    h = h * 31 + text[i];
                return ClampMlExit((h % 20001) / 10000.0 - 1.0, -1.0, 1.0);
            }
        }

        private double MlExitDollarsPerTickFeature() {
            double dollarsPerTick = CurrentInstrument == null || CurrentInstrument.MasterInstrument == null
                ? 1.0
                : CurrentInstrument.MasterInstrument.PointValue * TickSize;
            return ClampMlExit(Math.Log10(Math.Max(1e-9, dollarsPerTick)) - 1.0, -2.0, 2.0);
        }

        private double MlExitUnrealizedR(double currentPrice) {
            if (!MlExitTrackingValid())
                return 0.0;
            double points = _exitDirection == "long"
                ? currentPrice - _exitEntryPrice
                : _exitEntryPrice - currentPrice;
            return SafeMlExitValue(points / _exitOneRPoints);
        }

        private double MlExitBarDurationSeconds() {
            if (CurrentContextBar < 1)
                return 60.0;
            double seconds = Math.Abs((Time[0] - Time[1]).TotalSeconds);
            return seconds >= 1.0 ? seconds : 1.0;
        }

        private bool BuildMlExitFeatures(double currentPrice, out string featuresJson, out string contextJson, out double unrealizedR, out double barDurationSec, out string dataSeriesType, out double dataSeriesValue, out double[] featureRow) {
            featuresJson = string.Empty;
            contextJson = string.Empty;
            unrealizedR = 0.0;
            barDurationSec = 60.0;
            dataSeriesType = MlExitDataSeriesTypeName();
            dataSeriesValue = MlExitDataSeriesValue();
            featureRow = null;

            if (!MlExitTrackingValid() || CurrentContextBar < Math.Max(2, BarsRequiredToTrade) || atr == null || bb == null || temaIndicator == null)
                return false;

            double atrValue = SafeMlExitValue(atr[0]);
            if (atrValue <= 0 || currentPrice <= 0)
                return false;

            unrealizedR = MlExitUnrealizedR(currentPrice);
            barDurationSec = MlExitBarDurationSeconds();

            double bbDenom = SafeMlExitValue(bb.Upper[0] - bb.Middle[0]);
            double bbPosition = Math.Abs(bbDenom) > 1e-9
                ? (currentPrice - bb.Middle[0]) / bbDenom
                : 0.0;
            double temaSlope = CurrentContextBar > 0 ? (temaIndicator[0] - temaIndicator[1]) / atrValue : 0.0;
            double minutesSinceMidnight = Time[0].Hour * 60.0 + Time[0].Minute;
            double timeAngle = 2.0 * Math.PI * minutesSinceMidnight / 1440.0;
            double avgBarSpeed = ClampMlExit(60.0 / Math.Max(1.0, barDurationSec), 0.0, 3.0);
            double directionScalar = _exitDirection == "long" ? 1.0 : _exitDirection == "short" ? -1.0 : 0.0;
            string symbol = ResolveMasterInstrumentSymbol();

            double[] features = new double[] {
                unrealizedR,
                _exitBarsHeld / 50.0,
                atrValue / currentPrice,
                (currentPrice - sessionVwap) / atrValue,
                bbPosition,
                temaSlope,
                0.0,
                Math.Sin(timeAngle),
                Math.Cos(timeAngle)
            };

            double[] context = new double[] {
                ClampMlExit(_exitBarsHeld / 50.0, 0.0, 5.0),
                ClampMlExit(unrealizedR, -5.0, 5.0),
                directionScalar,
                avgBarSpeed,
                MlExitSymbolHashFeature(symbol),
                MlExitDollarsPerTickFeature(),
                MlExitDataSeriesTypeFeature(dataSeriesType),
                MlExitDataSeriesValueFeature(dataSeriesValue)
            };

            featureRow = new double[features.Length];
            StringBuilder featuresBuilder = new StringBuilder();
            featuresBuilder.Append("[");
            for (int i = 0; i < features.Length; i++) {
                double safeValue = SafeMlExitValue(features[i]);
                featureRow[i] = safeValue;
                if (i > 0)
                    featuresBuilder.Append(",");
                featuresBuilder.Append(FormatJsonDouble(safeValue));
            }
            featuresBuilder.Append("]");
            featuresJson = featuresBuilder.ToString();

            StringBuilder contextBuilder = new StringBuilder();
            contextBuilder.Append("[");
            for (int i = 0; i < context.Length; i++) {
                if (i > 0)
                    contextBuilder.Append(",");
                contextBuilder.Append(FormatJsonDouble(SafeMlExitValue(context[i])));
            }
            contextBuilder.Append("]");
            contextJson = contextBuilder.ToString();
            return true;
        }

        private string BuildMlExitSampleJson(int label, string regime, double currentPrice) {
            string featuresJson;
            string contextJson;
            double unrealizedR;
            double barDurationSec;
            string dataSeriesType;
            double dataSeriesValue;
            double[] featureRow;
            if (!BuildMlExitFeatures(currentPrice, out featuresJson, out contextJson, out unrealizedR, out barDurationSec, out dataSeriesType, out dataSeriesValue, out featureRow))
                return string.Empty;

            string symbol = ResolveMasterInstrumentSymbol();
            return new JsonWriter()
                .Str("trade_id", _exitTradeId)
                .Str("timestamp", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))
                .Str("symbol", symbol)
                .Str("direction", _exitDirection)
                .Raw("bars_held", _exitBarsHeld.ToString(CultureInfo.InvariantCulture))
                .Raw("unrealized_r", FormatJsonDouble(unrealizedR))
                .Raw("bar_duration_sec", FormatJsonDouble(barDurationSec))
                .Str("data_series_type", dataSeriesType)
                .Raw("data_series_value", FormatJsonDouble(dataSeriesValue))
                .Raw("features", featuresJson)
                .Raw("label", label.ToString(CultureInfo.InvariantCulture))
                .Str("regime", regime)
                .Raw("entry_price", FormatJsonDouble(_exitEntryPrice))
                .Raw("one_r_points", FormatJsonDouble(_exitOneRPoints))
                .Str("sample_date", CurrentClockTime().ToString("yyyy-MM-dd", CultureInfo.InvariantCulture))
                .Raw("template_number", _activeTemplateNumber.ToString(CultureInfo.InvariantCulture))
                .ToString();

        }

        private string BuildMlExitPredictJson(double currentPrice, out double unrealizedR) {
            unrealizedR = 0.0;
            string featuresJson;
            string contextJson;
            double barDurationSec;
            string dataSeriesType;
            double dataSeriesValue;
            double[] featureRow;
            if (!BuildMlExitFeatures(currentPrice, out featuresJson, out contextJson, out unrealizedR, out barDurationSec, out dataSeriesType, out dataSeriesValue, out featureRow))
                return string.Empty;

            // Send the trade's full held-history sequence, matching exit_model.py training data; falls back to length-1 if empty.
            string sequenceJson = _exitFeatureHistory.Count > 0
                ? SerializeFeatureHistoryJson(_exitFeatureHistory)
                : "[" + featuresJson + "]";

            string symbol = ResolveMasterInstrumentSymbol();
            return new JsonWriter()
                .Str("symbol", symbol)
                .Str("direction", _exitDirection)
                .Raw("bars_held", _exitBarsHeld.ToString(CultureInfo.InvariantCulture))
                .Raw("unrealized_r", FormatJsonDouble(unrealizedR))
                .Raw("entry_price", FormatJsonDouble(_exitEntryPrice))
                .Raw("one_r_points", FormatJsonDouble(_exitOneRPoints))
                .Str("data_series_type", dataSeriesType)
                .Raw("data_series_value", FormatJsonDouble(dataSeriesValue))
                .Raw("sequence", sequenceJson)
                .Raw("context", contextJson)
                .ToString();

        }

        private string SerializeFeatureHistoryJson(List<double[]> history) {
            StringBuilder builder = new StringBuilder();
            builder.Append("[");
            for (int i = 0; i < history.Count; i++) {
                if (i > 0)
                    builder.Append(",");
                builder.Append("[");
                double[] row = history[i];
                for (int k = 0; k < row.Length; k++) {
                    if (k > 0)
                        builder.Append(",");
                    builder.Append(FormatJsonDouble(row[k]));
                }
                builder.Append("]");
            }
            builder.Append("]");
            return builder.ToString();
        }

        // Parses the exact shape from SerializeFeatureHistoryJson(): [[d,d,...],...]. Not a general JSON parser.
        private List<double[]> ParseFeatureHistoryJson(string json) {
            List<double[]> history = new List<double[]>();
            if (string.IsNullOrEmpty(json))
                return history;

            var depth = 0;
            int rowStart = -1;
            for (int i = 0; i < json.Length; i++) {
                char c = json[i];
                if (c == '[') {
                    depth++;
                    if (depth == 2)
                        rowStart = i + 1;
                }
                else if (c == ']') {
                    if (depth == 2 && rowStart >= 0) {
                        string rowContent = json.Substring(rowStart, i - rowStart);
                        if (rowContent.Length > 0) {
                            string[] parts = rowContent.Split(',');
                            double[] row = new double[parts.Length];
                            for (int k = 0; k < parts.Length; k++) {
                                double value;
                                double.TryParse(parts[k].Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out value);
                                row[k] = value;
                            }
                            history.Add(row);
                        }
                        rowStart = -1;
                    }
                    depth--;
                }
            }
            return history;
        }

        private string PendingExitSamplePath() {
            return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TemaLimit_pending_exit_samples.jsonl");
        }

        // Retry, then spool to disk rather than drop. Runs off the trading thread.
        private void PostExitSampleDurable(string url, string json, string tradeId) {
            if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(json))
                return;

            System.Threading.Tasks.Task.Run(() => {
                string lastError = string.Empty;
                for (int attempt = 1; attempt <= MlExitSamplePostAttempts; attempt++) {
                    try {
                        PostJson(url, json);
                        return;
                    }
                    catch (Exception ex) {
                        lastError = ex.Message;
                        if (attempt < MlExitSamplePostAttempts) {
                            try { System.Threading.Thread.Sleep(MlExitSamplePostBackoffMs * attempt); }
                            catch (Exception) { }
                        }
                    }
                }
                SpoolExitSample(url, json, tradeId, lastError);
            });
        }

        // One line per undelivered sample: "<url>\t<json>". Replayed by ReplayPendingExitSamples.
        private void SpoolExitSample(string url, string json, string tradeId, string reason) {
            try {
                string line = url + "\t" + json.Replace("\r", " ").Replace("\n", " ");
                string path = PendingExitSamplePath();
                lock (pendingExitSampleLock) {
                    // Bound the spool so a long outage cannot grow it without limit.
                    if (File.Exists(path) && File.ReadAllLines(path).Length >= MlExitSampleSpoolMaxLines)
                        return;
                    File.AppendAllText(path, line + Environment.NewLine);
                }
                TriggerCustomEvent(o => Print(OutputTimePrefix() + "ML EXIT SAMPLE SPOOLED trade=" + tradeId
                    + " (" + reason + ") -- queued for replay"), null);
            }
            catch (Exception error) {
                D("Exit-sample spool failed: " + error.Message);
            }
        }

        // Redeliver spooled exit samples. Throttled, off-thread, and static-guarded so
        // concurrent strategy instances do not replay the shared file at the same time.
        private void ReplayPendingExitSamples() {
            DateTime nowUtc = DateTime.UtcNow;
            lock (pendingExitSampleLock) {
                if (exitSampleReplayRunning)
                    return;
                if ((nowUtc - lastExitSampleReplayUtc).TotalSeconds < MlExitSampleReplayThrottleSeconds)
                    return;
                string probe = PendingExitSamplePath();
                if (!File.Exists(probe))
                    return;
                lastExitSampleReplayUtc = nowUtc;
                exitSampleReplayRunning = true;
            }

            System.Threading.Tasks.Task.Run(() => {
                int delivered = 0;
                try {
                    string path = PendingExitSamplePath();
                    string[] lines;
                    lock (pendingExitSampleLock) {
                        if (!File.Exists(path)) { return; }
                        lines = File.ReadAllLines(path);
                    }

                    List<string> stillPending = new List<string>();
                    foreach (string line in lines) {
                        if (string.IsNullOrEmpty(line))
                            continue;
                        int tab = line.IndexOf('\t');
                        if (tab <= 0) continue;          // malformed -> drop, cannot be replayed
                        string url = line.Substring(0, tab);
                        string json = line.Substring(tab + 1);
                        try {
                            PostJson(url, json);
                            delivered++;
                        }
                        catch (Exception) {
                            stillPending.Add(line);      // service still down; keep for next pass
                        }
                    }

                    lock (pendingExitSampleLock) {
                        if (stillPending.Count == 0)
                            File.Delete(path);
                        else
                            File.WriteAllText(path, string.Join(Environment.NewLine, stillPending.ToArray()) + Environment.NewLine);
                    }

                    if (delivered > 0) {
                        int count = delivered;
                        int left = stillPending.Count;
                        TriggerCustomEvent(o => Print(OutputTimePrefix() + "ML EXIT SAMPLE REPLAY: delivered "
                            + count + ", still pending " + left), null);
                    }
                }
                catch (Exception error) {
                    D("Exit-sample replay failed: " + error.Message);
                }
                finally {
                    lock (pendingExitSampleLock) { exitSampleReplayRunning = false; }
                }
            });
        }

        private void FireAndForgetPostJson(string url, string json) {
            if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(json))
                return;

            System.Threading.Tasks.Task.Run(() => {
                try {
                    PostJson(url, json);
                }
                catch (Exception ex) {
                    if (ShouldPrintMlHttpError()) TriggerCustomEvent(o => Print(OutputTimePrefix() + "ML POST FAILED: " + url + " - " + ex.Message), null);
                }
            });
        }

        // Plain-text POST to ntfy.sh with the Title/Priority/Tags headers the watchdog scripts already use.
        private string PostNtfy(string title, string message, string tags) {
            string topic = NtfyTopic;
            if (string.IsNullOrEmpty(topic))
                return "ntfy not configured";

            byte[] body = Encoding.UTF8.GetBytes(message);
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create("https://ntfy.sh/" + topic);
            request.Method = "POST";
            request.ContentType = "text/plain; charset=utf-8";
            request.Timeout = Math.Max(100, MlHttpTimeoutMs);
            request.ReadWriteTimeout = Math.Max(100, MlHttpTimeoutMs);
            request.Headers["Title"] = title;
            request.Headers["Priority"] = "default";
            request.Headers["Tags"] = tags;
            request.ContentLength = body.Length;

            using (Stream stream = request.GetRequestStream())
                stream.Write(body, 0, body.Length);

            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                return reader.ReadToEnd();
        }

        private void FireAndForgetNtfy(string title, string message, string tags) {
            if (string.IsNullOrEmpty(message))
                return;

            System.Threading.Tasks.Task.Run(() => {
                try {
                    PostNtfy(title, message, tags);
                }
                catch (Exception ex) {
                    if (ShouldPrintMlHttpError()) TriggerCustomEvent(o => Print(OutputTimePrefix() + "NTFY POST FAILED: " + ex.Message), null);
                }
            });
        }

        private bool IsNtfyNotifiableAccount(Account account) {
            EnsureNtfyConfigLoaded();
            return NtfyConfigured && account != null && !string.IsNullOrEmpty(account.Name)
                && NtfyNotifiableAccountNames.Contains(account.Name);
        }

        private string FormatOrderPrice(Order order, double price) {
            return order != null && order.Instrument != null && order.Instrument.MasterInstrument != null
                ? order.Instrument.MasterInstrument.FormatPrice(price)
                : price.ToString("0.00", CultureInfo.InvariantCulture);
        }

        private string NtfyOrderActionLabel(OrderAction action) {
            return (action == OrderAction.Buy || action == OrderAction.BuyToCover) ? "Buy" : "Sell";
        }

        private string NtfyOrderTypeLabel(OrderType type) {
            if (type == OrderType.Limit)
                return "Limit";
            if (type == OrderType.StopMarket)
                return "Stop";
            if (type == OrderType.StopLimit)
                return "Stop-Limit";
            return type.ToString();
        }

        private string NtfyExitReasonLabel(string orderName) {
            switch (ExitReasonForFill(orderName)) {
                case "ml": return "ML Exit";
                case "time": return "Time Exit";
                case "profit": return "Profit Target";
                case "daily_loss": return "Daily Max Loss";
                case "manual": return "Manual Exit";
                case "stop": return "Stop Loss";
                default: return "Unknown";
            }
        }

        private string NtfyPositionLabel(MarketPosition position, int quantity) {
            if (position == MarketPosition.Flat)
                return "Flat";
            return (position == MarketPosition.Long ? "Long " : "Short ") + Math.Abs(quantity).ToString(CultureInfo.InvariantCulture);
        }

        private void NotifyPendingOrderCreated(Order order) {
            string message =
                "Account: " + order.Account.Name + "\n"
                + "Instrument: " + order.Instrument.FullName + "\n"
                + "Action: " + NtfyOrderActionLabel(order.OrderAction) + "\n"
                + "Type: " + NtfyOrderTypeLabel(order.OrderType) + "\n"
                + "Qty: " + order.Quantity.ToString(CultureInfo.InvariantCulture) + "\n"
                + "Price: " + FormatOrderPrice(order, order.OrderType == OrderType.Limit ? order.LimitPrice : order.StopPrice) + "\n"
                + "Signal: " + order.Name + "\n"
                + "Time: " + CurrentClockTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);

            FireAndForgetNtfy("🟡 Pending Order", message, "large_orange_circle");
        }

        private void NotifyEntryOrderFilled(Execution execution, double fillPrice, int fillQuantity, MarketPosition positionAfter) {
            Order order = execution.Order;
            bool isLong = order.OrderAction == OrderAction.Buy;

            string message =
                "Account: " + order.Account.Name + "\n"
                + "Instrument: " + order.Instrument.FullName + "\n"
                + "Direction: " + (isLong ? "Long" : "Short") + "\n"
                + "Qty: " + fillQuantity.ToString(CultureInfo.InvariantCulture) + "\n"
                + "Price: " + FormatOrderPrice(order, fillPrice) + "\n"
                + "Avg Fill: " + FormatOrderPrice(order, order.AverageFillPrice) + "\n"
                + "Position: " + NtfyPositionLabel(positionAfter, CurrentPosition.Quantity) + "\n"
                + "Time: " + CurrentClockTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);

            FireAndForgetNtfy("🟢 Entry Filled", message, "large_green_circle");
        }

        private void NotifyExitOrderFilled(Execution execution, double fillPrice, int fillQuantity, MarketPosition positionAfter) {
            Order order = execution.Order;
            double pnlDollars = 0.0;
            if (entryPrice > 0 && CurrentInstrument != null && CurrentInstrument.MasterInstrument != null) {
                bool closedWasLong = order.OrderAction == OrderAction.Sell;
                double points = closedWasLong ? fillPrice - entryPrice : entryPrice - fillPrice;
                pnlDollars = points * CurrentInstrument.MasterInstrument.PointValue * Math.Max(1, fillQuantity);
            }
            string pnlText = (pnlDollars >= 0 ? "+$" : "-$") + Math.Abs(pnlDollars).ToString("0.##", CultureInfo.InvariantCulture);

            string message =
                "Account: " + order.Account.Name + "\n"
                + "Instrument: " + order.Instrument.FullName + "\n"
                + "Exit: " + NtfyExitReasonLabel(order.Name) + "\n"
                + "Qty: " + fillQuantity.ToString(CultureInfo.InvariantCulture) + "\n"
                + "Price: " + FormatOrderPrice(order, fillPrice) + "\n"
                + "PnL: " + pnlText + "\n"
                + "Position: " + NtfyPositionLabel(positionAfter, CurrentPosition.Quantity) + "\n"
                + "Time: " + CurrentClockTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);

            FireAndForgetNtfy("🔴 Exit Filled", message, "red_circle");
        }

        private void TryRefreshMlExitPhase() {
            if (!IsActiveContextBarsInProgress() || State != State.Realtime || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            DateTime date = Time[0].Date;
            if (_mlExitPhaseCheckDate == date || _mlExitPhaseCheckPending)
                return;

            _mlExitPhaseCheckDate = date;
            _mlExitPhaseCheckPending = true;
            int phaseContextBip = CurrentBarsInProgressIndex();
            // data_series_type/value must be sent so the server resolves the SAME group the
            // exit samples log under (exit_group_key). The old symbol-only URL made the server
            // group this as {SYMBOL}_UNKNOWN -- a phantom group with zero samples -- so the
            // daily phase check answered phase 1 forever regardless of the real group's state.
            // FormatJsonDouble matches BuildMlExitSampleJson's data_series_value formatting
            // ("500", never "500.0") so both endpoints normalize to the identical group key.
            string url = TrimTrailingSlash(MlExitServerUrl) + "/ml-exit-phase?symbol=" + Uri.EscapeDataString(CurrentInstrument.MasterInstrument.Name)
                + "&data_series_type=" + Uri.EscapeDataString(MlExitDataSeriesTypeName())
                + "&data_series_value=" + Uri.EscapeDataString(FormatJsonDouble(MlExitDataSeriesValue()));

            System.Threading.Tasks.Task.Run(() => {
                try {
                    string response = GetJson(url);
                    int phase = ExtractJsonInt(response, "recommended_phase", 0);
                    bool phase3Unlocked = ExtractJsonBool(response, "phase3_unlocked");
                    string reason = ExtractJsonString(response, "reason");
                    TriggerCustomEvent(o => RunWithContext(phaseContextBip, () => ApplyMlExitPhase(phase, phase3Unlocked, reason)), null);
                }
                catch {
                    // Clear the latched date so a transient outage retries later instead of staying stale until tomorrow.
                    TriggerCustomEvent(o => RunWithContext(phaseContextBip, () => { _mlExitPhaseCheckPending = false; _mlExitPhaseCheckDate = DateTime.MinValue; }), null);
                }
            });
        }

        private void ApplyMlExitPhase(int phase, bool phase3Unlocked, string reason) {
            _mlExitPhaseCheckPending = false;
            int cleanPhase = Math.Max(0, Math.Min(3, phase));
            bool changed = cleanPhase != _mlExitRecommendedPhase || phase3Unlocked != _mlExitPhase3Unlocked;
            _mlExitRecommendedPhase = cleanPhase;
            _mlExitPhase3Unlocked = phase3Unlocked;

            if (changed)
                Print(OutputTimePrefix() + "ML Exit phase " + _mlExitRecommendedPhase + " " + OutputContext() + ": " + reason + " phase3_unlocked=" + _mlExitPhase3Unlocked);
        }

        private void HandleMlExitModelOnBar() {
            if (!IsActiveContextBarsInProgress() || State != State.Realtime || CurrentContextBar < BarsRequiredToTrade || EffectiveMarketPosition() == MarketPosition.Flat)
                return;

            if (!MlExitTrackingValid())
                return;

            if (_lastMlExitSampleBar != CurrentContextBar) {
                _exitBarsHeld++;
                _lastMlExitSampleBar = CurrentContextBar;

                // Append this bar's row to held-history before any read, so a same-bar prediction includes it as the latest step.
                string historyFeaturesJson, historyContextJson, historyDataSeriesType;
                double historyUnrealizedR, historyBarDurationSec, historyDataSeriesValue;
                double[] historyFeatureRow;
                if (BuildMlExitFeatures(Close[0], out historyFeaturesJson, out historyContextJson, out historyUnrealizedR, out historyBarDurationSec, out historyDataSeriesType, out historyDataSeriesValue, out historyFeatureRow))
                    _exitFeatureHistory.Add(historyFeatureRow);

                if (MlExitSampleLoggingActive()) {
                    DateTime nowClock = CurrentClockTime();
                    if ((nowClock - _lastMlExitSampleLogTime) >= MlExitSampleMinInterval) {
                        _lastMlExitSampleLogTime = nowClock;
                        string sampleJson = BuildMlExitSampleJson(1, "open", Close[0]);
                        FireAndForgetPostJson(TrimTrailingSlash(MlExitServerUrl) + "/log-exit-sample", sampleJson);
                    }
                }
            }

            if (_lastMlExitPredictionBar == CurrentContextBar)
                return;

            if (!MlExitRecommendationsActive() && !EnableMlExitControl)
                return;

            RequestMlExitPrediction();
        }

        private void RequestMlExitPrediction() {
            double unrealizedR;
            string predictJson = BuildMlExitPredictJson(Close[0], out unrealizedR);
            if (string.IsNullOrEmpty(predictJson))
                return;

            _lastMlExitPredictionBar = CurrentContextBar;
            string wouldExitJson = BuildMlExitSampleJson(1, "ml_would_exit", Close[0]);
            string predictUrl = TrimTrailingSlash(MlExitServerUrl) + "/predict-exit";
            string sampleUrl = TrimTrailingSlash(MlExitServerUrl) + "/log-exit-sample";
            int signalBar = CurrentContextBar;
            string tradeId = _exitTradeId;
            int barsHeld = _exitBarsHeld;
            bool controlRequested = EnableMlExitControl;
            double minR = MinUnrealizedRForMlExit;
            int minBars = MinBarsBeforeMlExit;
            double localHoldThreshold = MlExitHoldThreshold;
            int exitContextBip = CurrentBarsInProgressIndex();

            System.Threading.Tasks.Task.Run(() => {
                try {
                    string response = PostJson(predictUrl, predictJson);
                    string recommendation = ExtractJsonString(response, "recommendation");
                    double holdConfidence = ExtractJsonDouble(response, "hold_confidence");
                    double exitConfidence = ExtractJsonDouble(response, "exit_confidence");
                    bool modelReady = ExtractJsonBool(response, "model_ready");
                    bool serverEnabled = ExtractJsonBool(response, "exit_model_enabled");
                    bool serverPhase3Unlocked = ExtractJsonBool(response, "phase3_unlocked");
                    bool recommendsExit = recommendation == "exit" && holdConfidence <= localHoldThreshold;

                    // Keep _mlExitPhase3Unlocked in sync with every /predict-exit response, not just the daily check.
                    TriggerCustomEvent(o => RunWithContext(exitContextBip, () => { _mlExitPhase3Unlocked = serverPhase3Unlocked; }), null);

                    if (recommendsExit) {
                        if (!controlRequested && !string.IsNullOrEmpty(wouldExitJson)) {
                            try { PostJson(sampleUrl, wouldExitJson); } catch { }
                        }

                        TriggerCustomEvent(o => RunWithContext(exitContextBip, () => {
                            Print(OutputTimePrefix() + "ML Exit [" + tradeId + "]: EXIT " + OutputContext()
                                + " hold=" + holdConfidence.ToString("0.000", CultureInfo.InvariantCulture)
                                + " exit=" + exitConfidence.ToString("0.000", CultureInfo.InvariantCulture)
                                + " bars=" + barsHeld.ToString(CultureInfo.InvariantCulture)
                                + " r=" + unrealizedR.ToString("0.00", CultureInfo.InvariantCulture));
                        }), null);
                    }

                    if (controlRequested
                        && recommendsExit
                        && modelReady
                        && serverEnabled
                        && serverPhase3Unlocked
                        && barsHeld >= minBars
                        && unrealizedR >= minR) {
                        TriggerCustomEvent(o => RunWithContext(exitContextBip, () => SubmitMlExitOrderFromSignal(signalBar, holdConfidence, exitConfidence, unrealizedR)), null);
                    }
                    else if (controlRequested && serverPhase3Unlocked && serverEnabled && modelReady && !_mlExitArmedPrinted) {
                        TriggerCustomEvent(o => RunWithContext(exitContextBip, () => {
                            if (!_mlExitArmedPrinted) {
                                _mlExitArmedPrinted = true;
                                Print(OutputTimePrefix() + "ML Exit control armed " + OutputContext());
                            }
                        }), null);
                    }
                }
                catch {
                }
            });
        }

        private void SubmitMlExitOrderFromSignal(int signalBar, double holdConfidence, double exitConfidence, double unrealizedR) {
            if (!EnableMlExitControl || !_mlExitPhase3Unlocked || _mlExitSubmitted || signalBar != CurrentContextBar)
                return;

            if (_lastMlExitControlBar != int.MinValue && CurrentContextBar - _lastMlExitControlBar < Math.Max(1, MlExitSignalCooldownBars))
                return;

            MarketPosition position = EffectiveMarketPosition();
            if (position == MarketPosition.Flat)
                return;

            _lastExitReason = "ml";
            _mlExitSubmitted = true;
            _lastMlExitControlBar = CurrentContextBar;

            if (position == MarketPosition.Long) {
                if (EnableMultiSymbolMode)
                    ExitLong(CurrentBarsInProgressIndex(), EffectiveQuantity(), "MLExit", EntrySignalForPosition());
                else
                    ExitLong("MLExit", EntrySignalForPosition());
            }
            else if (position == MarketPosition.Short) {
                if (EnableMultiSymbolMode)
                    ExitShort(CurrentBarsInProgressIndex(), EffectiveQuantity(), "MLExit", EntrySignalForPosition());
                else
                    ExitShort("MLExit", EntrySignalForPosition());
            }

            string symbol = ResolveMasterInstrumentSymbol();
            string json = new JsonWriter()
                .Str("trade_id", _exitTradeId)
                .Str("symbol", symbol)
                .Str("timestamp", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture))
                .Raw("hold_confidence", FormatJsonDouble(holdConfidence))
                .Raw("exit_confidence", FormatJsonDouble(exitConfidence))
                .Raw("bars_held", _exitBarsHeld.ToString(CultureInfo.InvariantCulture))
                .Raw("unrealized_r", FormatJsonDouble(unrealizedR))
                .ToString();

            FireAndForgetPostJson(TrimTrailingSlash(MlExitServerUrl) + "/log-ml-exit", json);
            Print(OutputTimePrefix() + "ML EXIT SUBMITTED " + OutputContext()
                + ": hold=" + holdConfidence.ToString("0.000", CultureInfo.InvariantCulture)
                + " exit=" + exitConfidence.ToString("0.000", CultureInfo.InvariantCulture));
        }

        private void StartMlExitTradeTracking(OrderAction action, double fillPrice, double initialStopPrice) {
            if (!string.IsNullOrEmpty(_exitTradeId) || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return;

            _exitEntryPrice = fillPrice;
            _exitOneRPoints = Math.Abs(fillPrice - initialStopPrice);
            if (_exitOneRPoints < TickSize)
                _exitOneRPoints = TickSize;
            _exitDirection = action == OrderAction.Buy ? "long" : "short";
            _exitTradeId = DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture) + "_" + CurrentInstrument.MasterInstrument.Name;
            _exitBarsHeld = 0;
            _exitFeatureHistory = new List<double[]>();
            _lastExitReason = "unknown";
            _lastMlExitSampleBar = int.MinValue;
            _lastMlExitSampleLogTime = DateTime.MinValue;
            _lastMlExitPredictionBar = int.MinValue;
            _lastMlExitControlBar = int.MinValue;
            _mlExitSubmitted = false;
            _mlExitArmedPrinted = false;
        }

        private void ResetMlExitTracking() {
            _exitTradeId = string.Empty;
            _exitBarsHeld = 0;
            _exitEntryPrice = 0.0;
            _exitOneRPoints = 0.0;
            _exitDirection = string.Empty;
            _exitFeatureHistory = new List<double[]>();
            _lastExitReason = "unknown";
            _lastMlExitSampleBar = int.MinValue;
            _lastMlExitSampleLogTime = DateTime.MinValue;
            _lastMlExitPredictionBar = int.MinValue;
            _lastMlExitControlBar = int.MinValue;
            _mlExitSubmitted = false;
            _mlExitArmedPrinted = false;
        }

        private void MarkExitReason(string exitSignal) {
            string signal = exitSignal ?? string.Empty;
            if (signal.IndexOf("MLExit", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "ml";
            else if (signal.IndexOf("TimeExit", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "time";
            else if (signal.IndexOf("TakeProfit", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "profit";
            else if (signal.IndexOf("DailyMaxLoss", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "daily_loss";
            else if (signal.IndexOf("EnableReset", StringComparison.OrdinalIgnoreCase) >= 0 || signal.IndexOf("EnableAccountClose", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "manual";
            else if (signal.IndexOf("Stop loss", StringComparison.OrdinalIgnoreCase) >= 0)
                _lastExitReason = "stop";
            else if (string.IsNullOrEmpty(_lastExitReason))
                _lastExitReason = "unknown";
        }

        private string ExitReasonForFill(string orderName) {
            string before = _lastExitReason;
            MarkExitReason(orderName);
            if (!string.IsNullOrEmpty(_lastExitReason))
                return _lastExitReason;
            return string.IsNullOrEmpty(before) ? "unknown" : before;
        }

        private void LogMlExitFinalSample(double exitPrice, string orderName) {
            if (!MlExitTrackingValid()) {
                Print(OutputTimePrefix() + "ML EXIT SAMPLE SKIPPED " + OutputContext() + ": tracking invalid. tradeId=" + _exitTradeId + " entryPrice=" + _exitEntryPrice + " oneR=" + _exitOneRPoints + " direction=" + _exitDirection);
                return;
            }

            // Double-log guard. A ManualFlatten logs at the MARK price, and a
            // strategy exit fill can land afterwards and win the race -- that is
            // exactly what CorrectManualFlattenLog exists to net out on the
            // trade-record side. Without this, such a trade would contribute TWO
            // label-0 rows, and label-0 is the scarce class the exit model is
            // starved of (ES_3LINEBREAK has 4 in ~70k), so a duplicate does real
            // damage to the very data we are short of. First writer wins: the
            // fill path corrects the trade record, not the sample. The features
            // are read at flatten time either way, so the row stays honest --
            // only exitPrice may be a mark rather than a fill.
            if (!_exitSampleLoggedTradeIds.Add(_exitTradeId)) {
                Print(OutputTimePrefix() + "ML EXIT SAMPLE DEDUPED " + OutputContext() + ": trade=" + _exitTradeId + " already logged (this call: " + orderName + ")");
                return;
            }
            if (_exitSampleLoggedTradeIds.Count > ExitSampleLoggedIdsMax)
                _exitSampleLoggedTradeIds.Clear();

            string reason = ExitReasonForFill(orderName);
            string json = BuildMlExitSampleJson(0, reason, exitPrice);
            if (string.IsNullOrEmpty(json)) {
                Print(OutputTimePrefix() + "ML EXIT SAMPLE SKIPPED " + OutputContext() + ": BuildMlExitFeatures failed. atr=" + (atr != null ? atr[0].ToString(CultureInfo.InvariantCulture) : "null") + " exitPrice=" + exitPrice + " currentBar=" + CurrentContextBar);
                return;
            }

            // Durable: this is the single-shot label-0 row. Retries, then spools to disk.
            PostExitSampleDurable(TrimTrailingSlash(MlExitServerUrl) + "/log-exit-sample", json, _exitTradeId);
            Print(OutputTimePrefix() + "ML EXIT SAMPLE SENT " + OutputContext() + ": trade=" + _exitTradeId + " reason=" + reason);
        }

        private bool CrossedAbove(ISeries<double> input, ISeries<double> line) {
            return CurrentContextBar > 0 && input[1] < line[1] && input[0] >= line[0];
        }

        private bool CrossedBelow(ISeries<double> input, ISeries<double> line) {
            return CurrentContextBar > 0 && input[1] > line[1] && input[0] <= line[0];
        }

        private string CrossSignalName(bool isLong) {
            bool vwapCrossUp = CrossedAbove(temaIndicator, sessionVwapSeries);
            bool vwapCrossDown = CrossedBelow(temaIndicator, sessionVwapSeries);
            bool midBbCrossUp = CrossedAbove(temaIndicator, bb.Middle);
            bool midBbCrossDown = CrossedBelow(temaIndicator, bb.Middle);
            bool vwapAboveUpperBand = sessionVwapSeries[0] > bb.Upper[0];
            bool vwapBelowLowerBand = sessionVwapSeries[0] < bb.Lower[0];

            if (isLong) {
                if (vwapCrossDown && vwapBelowLowerBand)
                    return "LVR";

                if (vwapCrossUp && !vwapAboveUpperBand)
                    return "LV";

                return midBbCrossUp ? "LM" : "L";
            }

            if (vwapCrossUp && vwapAboveUpperBand)
                return "SVR";

            if (vwapCrossDown && !vwapBelowLowerBand)
                return "SV";

            return midBbCrossDown ? "SM" : "S";
        }

        private string CrossSignalSource(bool isLong) {
            string signalName = CrossSignalName(isLong);

            if (signalName == "LV")
                return "VWAP long cross";

            if (signalName == "SV")
                return "VWAP short cross";

            if (signalName == "LM")
                return "MidBB long cross";

            if (signalName == "SM")
                return "MidBB short cross";

            if (signalName == "LVR")
                return "VWAP long rejection";

            if (signalName == "SVR")
                return "VWAP short rejection";

            return isLong ? "Long cross" : "Short cross";
        }

        private string FormatDebugPrice(double value) {
            return CurrentInstrument.MasterInstrument.FormatPrice(value);
        }

        private void DrawStrategyValues() {
            if (ChartControl == null || !ShowStrategyValues || temaIndicator == null || sessionVwapSeries == null)
                return;

            string text = "TEMA: " + FormatDebugPrice(temaIndicator[0])
                + "\nVWAP: " + FormatDebugPrice(sessionVwapSeries[0]);

            Draw.TextFixed(this, "StrategyValuesTopRight", text, TextPosition.TopRight,
                Brushes.White, labelFont, Brushes.Transparent, Brushes.Transparent, 0);
        }

        private void DrawCrossDebugLabel(string signalName, string source) {
            string text = signalName
                + "  " + source
                + "\nTime: " + Time[0]
                + "\nTEMA[1]: " + FormatDebugPrice(temaIndicator[1])
                + "   TEMA[0]: " + FormatDebugPrice(temaIndicator[0])
                + "\nVWAP[1]: " + FormatDebugPrice(sessionVwapSeries[1])
                + "   VWAP[0]: " + FormatDebugPrice(sessionVwapSeries[0])
                + "\nMidBB[1]: " + FormatDebugPrice(bb.Middle[1])
                + "   MidBB[0]: " + FormatDebugPrice(bb.Middle[0])
                + "\nClose[0]: " + FormatDebugPrice(Close[0])
                + "   Volume[0]: " + Volume[0];
            Brush brush = signalName.StartsWith("L") ? Brushes.DeepSkyBlue : Brushes.Magenta;

            Draw.TextFixed(this, "CrossDebugValues", text, TextPosition.BottomLeft,
                brush, labelFont, Brushes.Transparent, Brushes.Transparent, 0);
        }

        private void UpdateSessionVwap() {
            if (IsCurrentContextFirstBarOfSession) {
                cumulativeTypicalVolume = 0;
                cumulativeVolume = 0;
            }

            double volume = Math.Max(0.0, (double)Volume[0]);
            double typicalPrice = (High[0] + Low[0] + Close[0]) / 3.0;

            cumulativeTypicalVolume += typicalPrice * volume;
            cumulativeVolume += volume;
            sessionVwap = cumulativeVolume <= 0 ? Close[0] : cumulativeTypicalVolume / cumulativeVolume;

            if (sessionVwapSeries != null)
                sessionVwapSeries[0] = sessionVwap;
        }

        private void DrawStrategyVwap() {
            if (ChartControl == null || !ShowStrategyVwap || sessionVwapSeries == null || CurrentContextBar < 1 || IsCurrentContextFirstBarOfSession)
                return;

            Draw.Line(this, "StrategyVWAP_" + CurrentBarsInProgressIndex() + "_" + CurrentContextBar, 1, sessionVwapSeries[1], 0, sessionVwapSeries[0], Brushes.Gray);
        }

        private bool StochRsiCrossPasses(bool isLong) {
            if (!EnableStochRsiCrossFilter)
                return true;

            if (stochRsi == null || CurrentContextBar < Math.Max(1, StochRsiPeriod))
                return false;

            // Long crosses back UP to the lower line; short crosses back DOWN to the upper line.
            return StochCrossPassesCore(stochRsi, isLong, isLong ? StochRsiLowerLine : StochRsiUpperLine, StochRsiCrossLookbackBars, 0);
        }
        private bool StartupEntrySignalsCleared(bool longSignal, bool shortSignal) {
            if (!RequireFreshSignalAfterEnable)
                return true;
            if (startupEntrySignalsClear)
                return true;

            if (!longSignal && !shortSignal)
                startupEntrySignalsClear = true;

            return false;
        }

        private void LogNormalNearMiss(bool isLong, bool temaCrossPass, bool stochPass) {
            bool mfiPass = MfiFilterPassesForNearMiss(isLong);
            LogNearMiss("TEMA/BB", isLong, temaCrossPass, stochPass, mfiPass);
        }

        private void LogCrossNearMiss(bool isLong, bool crossEnabled, bool crossPass, bool stochPass) {
            if (!crossEnabled)
                return;

            bool mfiPass = MfiFilterPassesForNearMiss(isLong);
            LogNearMiss("TEMA/VWAP/MidBB", isLong, crossPass, stochPass, mfiPass);
        }

        private bool MfiFilterPassesForNearMiss(bool isLong) {
            return !EnableMfiFilter || MfiFilterPasses(isLong);
        }

        private void LogNearMiss(string setupName, bool isLong, bool setupPass, bool stochPass, bool mfiPass) {
            int missing = 0;
            string missingName = string.Empty;

            if (!setupPass) {
                missing++;
                missingName = "setup";
            }
            if (!stochPass) {
                missing++;
                missingName = "StochRSI";
            }
            if (!mfiPass) {
                missing++;
                missingName = "MFI";
            }

            if (missing != 1 || missingName == "setup" || nearMissLogBar == CurrentContextBar)
                return;

            nearMissLogBar = CurrentContextBar;
            if (DebugMode)
            Print(OutputTimePrefix() + "NEAR MISS " + OutputContext() + ": " + setupName + " " + (isLong ? "LONG" : "SHORT")
                + " missed by 1 filter; missing=" + missingName);
        }
        private bool FinalMfiFilterPasses(bool isLong, string signalName) {
            if (!EnableMfiFilter)
                return true;

            bool passes = MfiFilterPasses(isLong);
            if (!passes)
                D("MFI blocked " + OutputContext() + " " + signalName + " final " + (isLong ? "LONG" : "SHORT") + " entry. MFI[" + MfiPriorBars + "]=" + mfi[Math.Max(0, MfiPriorBars)].ToString("0.00"));

            return passes;
        }



        // === Shared entry-filter predicate cores ===
        // Live entries and the shadow sweep gate on the SAME MFI/RSI/StochRSI logic; keeping one
        // implementation each means the two can never silently drift (which would make shadow
        // training samples reflect different gating than live orders). The only real differences
        // are folded into parameters: baseBarsAgo is 0 for live (reads the forming bar onward) and
        // 1 for shadow (completed bars only), and thresholds/indicator instances are passed in.
        // The public MfiFilterPasses / RsiFilterPasses / StochRsiCrossPasses / Shadow* methods below
        // are thin wrappers over these, so every existing call site is unchanged.

        private bool MfiPassesCore(MFI mfiInstance, bool isLong, double longMax, double shortMin, int priorBars, int baseBarsAgo) {
            int barsToCheck = Math.Min(Math.Max(0, priorBars), CurrentContextBar - baseBarsAgo);

            for (int k = 0; k <= barsToCheck; k++) {
                if (isLong && mfiInstance[baseBarsAgo + k] <= longMax)
                    return true;

                if (!isLong && mfiInstance[baseBarsAgo + k] >= shortMin)
                    return true;
            }

            return false;
        }

        private bool RsiPassesCore(bool isLong, double longMax, double shortMin, int barsAgo) {
            if (rsi == null || CurrentContextBar < barsAgo)
                return true;

            if (isLong)
                return rsi[barsAgo] <= longMax;

            return rsi[barsAgo] >= shortMin;
        }

        // Cross detection: the prior bar sits beyond `line` and the current bar has crossed back to
        // it (>= for long at the lower line, <= for short at the upper line). Scans up to lookbackBars
        // pairs. Callers supply the direction-appropriate line, the indicator instance, and baseBarsAgo.
        private bool StochCrossPassesCore(NinjaTrader.NinjaScript.Indicators.StochRSI stoch, bool isLong, double line, int lookbackBars, int baseBarsAgo) {
            int barsToCheck = Math.Min(Math.Max(0, lookbackBars), CurrentContextBar - 1 - baseBarsAgo);

            for (int k = 0; k <= barsToCheck; k++) {
                int barsAgo = baseBarsAgo + k;

                if (isLong && stoch[barsAgo + 1] < line && stoch[barsAgo] >= line)
                    return true;

                if (!isLong && stoch[barsAgo + 1] > line && stoch[barsAgo] <= line)
                    return true;
            }

            return false;
        }

        private bool MfiFilterPasses(bool isLong) {
            return MfiPassesCore(mfi, isLong, MfiLongMax, MfiShortMin, MfiPriorBars, 0);
        }

        private bool FinalRsiFilterPasses(bool isLong, string signalName) {
            if (!EnableRsiFilter)
                return true;

            bool passes = RsiFilterPasses(isLong);
            if (!passes)
                D("RSI blocked " + OutputContext() + " " + signalName + " final " + (isLong ? "LONG" : "SHORT") + " entry. RSI[0]=" + (rsi != null ? rsi[0].ToString("0.00") : "n/a"));

            return passes;
        }

        private bool RsiFilterPasses(bool isLong) {
            return RsiPassesCore(isLong, RsiLongMax, RsiShortMin, 0);
        }

        private void SubmitEntry(bool isLong, double price, string signalName, string source, string submittedOutputMessage) {
            if (!FinalMfiFilterPasses(isLong, signalName))
                return;

            if (!FinalRsiFilterPasses(isLong, signalName))
                return;

            if (!HasDailyEntryRiskBudget())
                return;

            if (!HasDayMarginBudget(isLong))
                return;

            double pullback = Math.Max(0, PullbackTicks) * TickSize;
            double limitPrice = CurrentInstrument.MasterInstrument.RoundToTickSize(isLong ? price - pullback : price + pullback);

            string orderSignalName = ContextSignalName(signalName);

            PrepareInitialStopBeforeEntry(orderSignalName);

            if (isLong) {
                D("LONG " + source + " signal. Limit entry. Signal=" + signalName + " Signal price=" + price + " Limit price=" + limitPrice + " PullbackTicks=" + PullbackTicks);
                EnterLongLimit(CurrentBarsInProgressIndex(), true, Contracts, limitPrice, orderSignalName);
                if (!string.IsNullOrEmpty(submittedOutputMessage))
                    Print(OutputTimePrefix() + submittedOutputMessage);
            }
            else {
                D("SHORT " + source + " signal. Limit entry. Signal=" + signalName + " Signal price=" + price + " Limit price=" + limitPrice + " PullbackTicks=" + PullbackTicks);
                EnterShortLimit(CurrentBarsInProgressIndex(), true, Contracts, limitPrice, orderSignalName);
                if (!string.IsNullOrEmpty(submittedOutputMessage))
                    Print(OutputTimePrefix() + submittedOutputMessage);
            }
        }

        private void PrepareInitialStopBeforeEntry(string signalName) {
            double pointValue = CurrentInstrument.MasterInstrument.PointValue;
            double rawInitialRiskPoints = RiskDollars1R / (pointValue * Math.Max(1, Contracts));
            double ticks = Math.Max(1, Math.Round(rawInitialRiskPoints / TickSize, MidpointRounding.AwayFromZero));
            SetTrackedStopLoss(signalName, CalculationMode.Ticks, ticks);
        }

        private void ManageOpenPosition() {
            WatchdogHeartbeat("ManageOpenPosition");
            if (!stopInitialized) {
                InitializeStopFromPosition();
                RestoreActiveMlStateIfMissing();
                RestoreExitTrackingIfMissing();
                return;
            }

            if (!EnsureProtectiveStopArmed())
                return;

            double favorablePoints = SanitizePointsExcursion(CalculateFavorablePointsSinceEntry(), "MFE");
            if (favorablePoints > maxFavorableExcursionPoints)
                maxFavorableExcursionPoints = favorablePoints;

            double adversePoints = SanitizePointsExcursion(CalculateAdversePointsSinceEntry(), "MAE");
            if (adversePoints > maxAdverseExcursionPoints)
                maxAdverseExcursionPoints = adversePoints;

            double desiredStopPrice = currentStopPrice;

            if (oneRPoints > 0) {
                double openProfitR = maxFavorableExcursionPoints / oneRPoints;

                if (openProfitR >= 24.0) {
                    if (takeProfitExitPending)
                        return;

                    if (EffectiveMarketPosition() == MarketPosition.Long) {
                        if (SubmitGuardedExitLong("TakeProfit24R", EntrySignalForPosition())) {
                            takeProfitExitPending = true;
                            return;
                        }
                    }
                    else if (EffectiveMarketPosition() == MarketPosition.Short) {
                        if (SubmitGuardedExitShort("TakeProfit24R", EntrySignalForPosition())) {
                            takeProfitExitPending = true;
                            return;
                        }
                    }
                }

                double lockedR = GetLockedRForOpenProfitR(openProfitR);
                if (lockedR > 0) {
                    desiredStopPrice = EffectiveMarketPosition() == MarketPosition.Long
                        ? entryPrice + lockedR * oneRPoints
                        : entryPrice - lockedR * oneRPoints;
                }
            }

            TryUpdateStopSafely(desiredStopPrice);
        }

        // Per-tick ladder trail, driven from OnMarketData. ManageOpenPosition() above only runs from
        // OnBarUpdateCore, which for an AddDataSeries symbol fires on that series' own bars rather than
        // on every tick: over 2026-07-20 07:18-08:24 the primary series took 164 stop moves while YM and
        // RTY took 2 and 11, across a comparable number of trail-eligible trades.
        //
        // The bar path does eventually SEE the peak -- maxFavorableExcursionPoints reads High[0], which
        // holds the whole bar's high -- but by then price has retraced, so the level the ladder wants is
        // above the market and TryUpdateStopSafely's "skip, don't clamp" guard rejects it as illegal.
        // The stop then never moves at all. 2026-07-20 YM 500-tick long: peaked +65 pts (1.08 ladder-R,
        // ladder wanted the stop at 52233) and closed on the 0.5R rung it had caught 10 minutes earlier.
        // Running the ratchet on ticks places each rung while it is still legal, on the way up.
        //
        // Deliberately much narrower than ManageOpenPosition: no stop initialization, no protective-stop
        // re-arm, no 24R take-profit, no watchdog heartbeat. Those stay on the bar cadence, so this path
        // can only ever RAISE an already-working stop -- it cannot open, close, or flatten a position.
        private void UpdateLadderTrailOnTick(double tickPrice) {
            if (!stopInitialized || oneRPoints <= 0 || entryPrice <= 0 || tickPrice <= 0)
                return;

            // Only modify a stop NT already has working. Arming a missing stop is
            // EnsureProtectiveStopArmed's job (it can flatten), and that stays on the bar path.
            if (!protectiveStopWorking || protectiveStopRearmPending || protectiveStopFlattenPending || takeProfitExitPending)
                return;

            MarketPosition position = EffectiveMarketPosition();
            if (position == MarketPosition.Flat)
                return;

            if (double.IsNaN(tradeHighSinceEntry))
                tradeHighSinceEntry = entryPrice;
            if (double.IsNaN(tradeLowSinceEntry))
                tradeLowSinceEntry = entryPrice;

            tradeHighSinceEntry = Math.Max(tradeHighSinceEntry, tickPrice);
            tradeLowSinceEntry = Math.Min(tradeLowSinceEntry, tickPrice);

            double favorablePoints = SanitizePointsExcursion(position == MarketPosition.Long
                ? Math.Max(0.0, tradeHighSinceEntry - entryPrice)
                : Math.Max(0.0, entryPrice - tradeLowSinceEntry), "MFE");
            if (favorablePoints > maxFavorableExcursionPoints)
                maxFavorableExcursionPoints = favorablePoints;

            double adversePoints = SanitizePointsExcursion(position == MarketPosition.Long
                ? Math.Max(0.0, entryPrice - tradeLowSinceEntry)
                : Math.Max(0.0, tradeHighSinceEntry - entryPrice), "MAE");
            if (adversePoints > maxAdverseExcursionPoints)
                maxAdverseExcursionPoints = adversePoints;

            double lockedR = GetLockedRForOpenProfitR(maxFavorableExcursionPoints / oneRPoints);
            if (lockedR <= 0)
                return;

            TryUpdateStopSafely(position == MarketPosition.Long
                ? entryPrice + lockedR * oneRPoints
                : entryPrice - lockedR * oneRPoints);
        }

        // Lock-free, always-current pre-filter for the per-tick trail. RunWithContext takes a lock and
        // copies the whole context in and out, which is far too heavy to run on every tick of every
        // series; Positions[] is NT's own per-series strategy position, so this costs nothing in the
        // common case where the series is flat.
        private bool SeriesHasOpenPosition(int barsInProgressIndex) {
            if (Positions == null || barsInProgressIndex < 0 || barsInProgressIndex >= Positions.Length)
                return false;

            Position seriesPosition = Positions[barsInProgressIndex];
            return seriesPosition != null && seriesPosition.MarketPosition != MarketPosition.Flat;
        }

        private void InitializeFavorableTracking() {
            entryBar = CurrentContextBar;
            tradeHighSinceEntry = entryPrice;
            tradeLowSinceEntry = entryPrice;
            maxFavorableExcursionPoints = 0.0;
            maxAdverseExcursionPoints = 0.0;
        }

        private void ResetFavorableTracking() {
            entryBar = int.MinValue;
            tradeHighSinceEntry = double.NaN;
            tradeLowSinceEntry = double.NaN;
            maxFavorableExcursionPoints = 0.0;
            maxAdverseExcursionPoints = 0.0;
        }

        // Real intrabar moves never plausibly exceed ~a few hundred points on NQ/ES/RTY/YM; a value
        // beyond this is a tracking bug (e.g. stale/cross-context state), not a real market move.
        // Added 2026-07-17 after mae_points was found corrupted in MLService/data/template_live_samples.csv
        // (one ES row logged 22090.75 points of adverse excursion on a trade that moved 2.75 points total) --
        // clamps the bad value to 0 instead of letting it poison /log-template-sample data, and prints once
        // per throttle window so a recurrence is visible in the debug log for further investigation.
        private const double MaxPlausiblePointsExcursion = 2000.0;
        private double SanitizePointsExcursion(double rawPoints, string label) {
            if (rawPoints <= MaxPlausiblePointsExcursion)
                return rawPoints;

            if (ShouldPrintMlHttpError())
                D("Implausible " + label + " excursion clamped to 0: " + rawPoints.ToString("0.##", CultureInfo.InvariantCulture) + " points (entryPrice=" + entryPrice.ToString("0.##", CultureInfo.InvariantCulture) + ")");
            return 0.0;
        }

        private double CalculateFavorablePointsSinceEntry() {
            if (entryPrice <= 0)
                return 0.0;

            MarketPosition position = EffectiveMarketPosition();
            double currentPrice = Close[0];

            if (CurrentContextBar == entryBar) {
                return position == MarketPosition.Long
                    ? Math.Max(0.0, currentPrice - entryPrice)
                    : Math.Max(0.0, entryPrice - currentPrice);
            }

            if (double.IsNaN(tradeHighSinceEntry))
                tradeHighSinceEntry = entryPrice;
            if (double.IsNaN(tradeLowSinceEntry))
                tradeLowSinceEntry = entryPrice;

            tradeHighSinceEntry = Math.Max(tradeHighSinceEntry, High[0]);
            tradeLowSinceEntry = Math.Min(tradeLowSinceEntry, Low[0]);

            return position == MarketPosition.Long
                ? Math.Max(0.0, tradeHighSinceEntry - entryPrice)
                : Math.Max(0.0, entryPrice - tradeLowSinceEntry);
        }

        // Reads the extremes CalculateFavorablePointsSinceEntry() maintains; call it after that method has run for the bar.
        private double CalculateAdversePointsSinceEntry() {
            if (entryPrice <= 0)
                return 0.0;

            MarketPosition position = EffectiveMarketPosition();

            if (CurrentContextBar == entryBar) {
                double currentPrice = Close[0];
                return position == MarketPosition.Long
                    ? Math.Max(0.0, entryPrice - currentPrice)
                    : Math.Max(0.0, currentPrice - entryPrice);
            }

            if (double.IsNaN(tradeHighSinceEntry) || double.IsNaN(tradeLowSinceEntry))
                return 0.0;

            return position == MarketPosition.Long
                ? Math.Max(0.0, entryPrice - tradeLowSinceEntry)
                : Math.Max(0.0, tradeHighSinceEntry - entryPrice);
        }

        // Backstop for "position open, nothing protecting it" -- the queued rejection above, or a stop
        // that went away for any other reason. Runs every tick (Calculate.OnEachTick), so the queued
        // path costs ~1 tick, not a bar. Re-arms at the intended risk level while that level is still
        // legal; once the market has run past it the risk budget is already blown, so it flattens
        // rather than silently re-arming 2-3x further out. Returns false while flattening.
        // Added 2026-07-20 after two ES shorts ran 13+ minutes with no stop at all.
        private bool EnsureProtectiveStopArmed() {
            if (protectiveStopFlattenPending)
                return false;

            if (protectiveStopWorking && !protectiveStopRearmPending)
                return true;

            // A submission is still in flight; wait for its Accepted/Rejected callback.
            if (!double.IsNaN(pendingStopSubmitPrice))
                return true;

            MarketPosition position = EffectiveMarketPosition();
            if (position == MarketPosition.Flat)
                return true;

            double buffer = Math.Max(0, StopSafetyBufferTicks) * TickSize;
            bool stopStillLegal;

            if (position == MarketPosition.Long) {
                double bid = GetCurrentBid(CurrentBarsInProgressIndex());
                double marketRef = bid > 0 ? bid : Close[0];
                double maxLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef - buffer);
                stopStillLegal = currentStopPrice > 0 && currentStopPrice <= maxLegalStop;
            }
            else {
                // Ask, not bid: buy-to-cover stops are validated against the ask (see TryUpdateStopSafely).
                double ask = GetCurrentAsk(CurrentBarsInProgressIndex());
                double marketRef = ask > 0 ? ask : Close[0];
                double minLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef + buffer);
                stopStillLegal = currentStopPrice > 0 && currentStopPrice >= minLegalStop;
            }

            protectiveStopRearmPending = false;

            if (stopStillLegal) {
                // No accepted stop exists right now, so clear the dedupe reference -- otherwise
                // re-arming at the same price as the vanished stop would be skipped as a no-op.
                lastSubmittedStopPrice = double.NaN;
                Print(Name + " no protective stop working; re-arming at " + currentStopPrice);
                SubmitStopLossIfChanged(EntrySignalForPosition(), currentStopPrice);
                return true;
            }

            Print(Name + " no protective stop working and intended level " + currentStopPrice
                + " is no longer legal; flattening");
            protectiveStopFlattenPending = true;

            if (position == MarketPosition.Long)
                SubmitGuardedExitLong("StopRejectedFlatten", EntrySignalForPosition());
            else
                SubmitGuardedExitShort("StopRejectedFlatten", EntrySignalForPosition());

            return false;
        }

        private void TryUpdateStopSafely(double desiredStopPrice) {
            double buffer = Math.Max(0, StopSafetyBufferTicks) * TickSize;
            double newStop = currentStopPrice;

            if (EffectiveMarketPosition() == MarketPosition.Long) {
                double bid = GetCurrentBid(CurrentBarsInProgressIndex());
                double marketRef = bid > 0 ? bid : Close[0];
                double maxLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef - buffer);
                double candidate = Math.Max(currentStopPrice, desiredStopPrice);
                candidate = CurrentInstrument.MasterInstrument.RoundToTickSize(candidate);

                // Skip, don't clamp: clamping to bid-buffer pinned the stop at the minimum legal
                // distance on every favorable tick, so any 6-tick wiggle inside the ChangeOrder
                // round-trip rejected it (2nd SimRenko kill, 2026-07-19 19:17, LONG side). The
                // existing working stop keeps protecting; a legal trail level comes back next tick.
                if (candidate > maxLegalStop)
                    return;

                if (candidate <= currentStopPrice || candidate <= 0 || candidate >= marketRef)
                    return;

                newStop = candidate;
            }
            else if (EffectiveMarketPosition() == MarketPosition.Short) {
                // NT validates buy-to-cover stops against the ASK ("buy stops can't be placed below the
                // market"), sell stops against the bid. The 2026-07-17 switch to bid here inverted that:
                // whenever the spread widened past StopSafetyBufferTicks (routine at Sunday reopen), the
                // clamped stop landed inside the spread and NT rejected the change -- SimRenko/NQ was
                // force-closed and disabled that way on 2026-07-19.
                double ask = GetCurrentAsk(CurrentBarsInProgressIndex());
                double marketRef = ask > 0 ? ask : Close[0];
                double minLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef + buffer);
                double candidate = Math.Min(currentStopPrice, desiredStopPrice);
                candidate = CurrentInstrument.MasterInstrument.RoundToTickSize(candidate);

                // Skip, don't clamp -- same reasoning as the LONG branch above.
                if (candidate < minLegalStop)
                    return;

                if (candidate >= currentStopPrice || candidate <= marketRef)
                    return;

                newStop = candidate;
            }
            else {
                return;
            }

            currentStopPrice = newStop;
            SubmitStopLossIfChanged(EntrySignalForPosition(), currentStopPrice);
        }
        private bool IsEntrySignalName(string signalName) {
            string baseName = BaseSignalName(signalName);
            return baseName == "L" || baseName == "S"
                || baseName == "LV" || baseName == "SV"
                || baseName == "LM" || baseName == "SM"
                || baseName == "LVR" || baseName == "SVR"
                || baseName == "ConfirmLong" || baseName == "ConfirmShort"
                || baseName == "ReversalLong" || baseName == "ReversalShort";
        }

        private bool IsSignalManagedExit(string exitSignal) {
            return exitSignal != null && System.Text.RegularExpressions.Regex.IsMatch(exitSignal, "^(TakeProfit24R|DailyMaxLossExit|TimeExit)");
        }

        private bool IsTerminalOrderState(OrderState orderState) {
            return orderState == OrderState.Filled ||
                   orderState == OrderState.Cancelled ||
                   orderState == OrderState.Rejected;
        }

        private void TrackProtectiveExitOrder(string name, OrderState orderState) {
            if (name == "Stop loss")
                protectiveStopWorking = !IsTerminalOrderState(orderState);
        }

        private bool HasWatchdogPosition() {
            return watchdogTradeDirection != 0 && watchdogTradeQuantity > 0;
        }

        private bool IsFullyFlatAndReconciled() {
            return CurrentPosition.MarketPosition == MarketPosition.Flat
                && !HasWatchdogPosition()
                && !protectiveStopWorking
                && !takeProfitExitPending
                && !dailyLossExitPending;
        }

        private MarketPosition EffectiveMarketPosition() {
            if (CurrentPosition.MarketPosition != MarketPosition.Flat)
                return CurrentPosition.MarketPosition;

            if (watchdogTradeDirection > 0)
                return MarketPosition.Long;

            if (watchdogTradeDirection < 0)
                return MarketPosition.Short;

            return MarketPosition.Flat;
        }

        private double EffectiveEntryPrice() {
            if (CurrentPosition.MarketPosition != MarketPosition.Flat && CurrentPosition.AveragePrice > 0)
                return CurrentPosition.AveragePrice;

            if (entryPrice > 0)
                return entryPrice;

            return watchdogEntryPrice;
        }

        private int EffectiveQuantity() {
            if (CurrentPosition.Quantity > 0)
                return CurrentPosition.Quantity;

            return Math.Max(1, watchdogTradeQuantity);
        }

        private double EffectiveUnrealizedPnl(double markPrice) {
            if (CurrentPosition.MarketPosition != MarketPosition.Flat)
                return CurrentPosition.GetUnrealizedProfitLoss(PerformanceUnit.Currency, markPrice);

            if (!HasWatchdogPosition() || watchdogEntryPrice <= 0 || CurrentInstrument == null || CurrentInstrument.MasterInstrument == null)
                return 0.0;

            double pointValue = CurrentInstrument.MasterInstrument.PointValue;
            if (pointValue <= 0)
                return 0.0;

            double points = watchdogTradeDirection > 0 ? markPrice - watchdogEntryPrice : watchdogEntryPrice - markPrice;
            return points * pointValue * Math.Max(1, watchdogTradeQuantity);
        }
        private void ArmWatchdogFromEntry(OrderAction action, double price, int quantity, string signalName) {
            watchdogTradeDirection = action == OrderAction.Buy ? 1 : action == OrderAction.SellShort ? -1 : 0;
            watchdogTradeQuantity = Math.Max(1, Math.Max(EffectiveQuantity(), quantity));
            watchdogEntryPrice = CurrentPosition.AveragePrice > 0 ? CurrentPosition.AveragePrice : price;
            watchdogEntrySignal = signalName ?? string.Empty;
            watchdogMismatchBar = int.MinValue;
            watchdogLastManagedBar = CurrentContextBar;
        }

        private void ResetWatchdogState() {
            watchdogTradeDirection = 0;
            watchdogTradeQuantity = 0;
            watchdogEntryPrice = 0;
            watchdogEntrySignal = string.Empty;
            watchdogMismatchBar = int.MinValue;
            watchdogLastManagedBar = int.MinValue;
        }

        private void WatchdogHeartbeat(string context) {
            if (!HasWatchdogPosition())
                return;

            watchdogLastManagedBar = CurrentContextBar;

            if (CurrentPosition.MarketPosition != MarketPosition.Flat || watchdogMismatchBar == CurrentContextBar)
                return;

            watchdogMismatchBar = CurrentContextBar;
            Print(Name + " WATCHDOG: tracked " + (watchdogTradeDirection > 0 ? "LONG" : "SHORT")
                + " is still active while strategy Position is Flat. context=" + context
                + " signal=" + watchdogEntrySignal
                + " qty=" + watchdogTradeQuantity
                + " entry=" + watchdogEntryPrice
                + " stop=" + currentStopPrice);
        }
        private void SetTrackedStopLoss(string signalName, CalculationMode mode, double value) {
            try {
                SetStopLoss(signalName, mode, value, false);
                protectiveStopWorking = true;
            }
            catch (Exception ex) {
                Print(Name + " SetStopLoss rejected, keeping existing resting stop. signal=" + signalName
                    + " value=" + value + " error=" + ex.Message);
            }
        }

        private bool SubmitGuardedExitLong(string exitSignal, string fromEntrySignal) {
            if (IsSignalManagedExit(exitSignal) && protectiveStopWorking)
                return false;

            MarkExitReason(exitSignal);
            if (EnableMultiSymbolMode)
                ExitLong(CurrentBarsInProgressIndex(), EffectiveQuantity(), exitSignal, fromEntrySignal);
            else
                ExitLong(exitSignal, fromEntrySignal);
            return true;
        }

        private bool SubmitGuardedExitShort(string exitSignal, string fromEntrySignal) {
            if (IsSignalManagedExit(exitSignal) && protectiveStopWorking)
                return false;

            MarkExitReason(exitSignal);
            if (EnableMultiSymbolMode)
                ExitShort(CurrentBarsInProgressIndex(), EffectiveQuantity(), exitSignal, fromEntrySignal);
            else
                ExitShort(exitSignal, fromEntrySignal);
            return true;
        }

        private void SubmitStopLossIfChanged(string signalName, double price) {
            price = CurrentInstrument.MasterInstrument.RoundToTickSize(price);

            // Dedupe against the in-flight submission if there is one, else against the last stop NT
            // actually Accepted. Recording the price at submit time meant a REJECTED stop still read
            // as "already submitted" and suppressed its own retry, leaving the position naked
            // (2026-07-20 ES). lastSubmittedStopPrice is now only advanced in OnOrderUpdateCore.
            double referenceStopPrice = !double.IsNaN(pendingStopSubmitPrice)
                ? pendingStopSubmitPrice
                : lastSubmittedStopPrice;

            if (!double.IsNaN(referenceStopPrice) && signalName == lastSubmittedStopSignal) {
                double minMove = Math.Max(1, MinStopMoveTicks) * TickSize;
                if (Math.Abs(price - referenceStopPrice) < minMove)
                    return;

                if ((DateTime.UtcNow - lastStopSubmitUtc).TotalMilliseconds < MinStopSubmitIntervalMs)
                    return;
            }

            SetTrackedStopLoss(signalName, CalculationMode.Price, price);
            pendingStopSubmitPrice = price;
            lastSubmittedStopSignal = signalName;
            lastStopUpdateBar = CurrentContextBar;
            lastStopSubmitUtc = DateTime.UtcNow;
        }

        private void InitializeStopFromPosition() {
            entryPrice = EffectiveEntryPrice();

            double pointValue = CurrentInstrument.MasterInstrument.PointValue;
            double rawInitialRiskPoints = RiskDollars1R / (pointValue * EffectiveQuantity());
            double initialRiskTicks = Math.Max(1, Math.Round(rawInitialRiskPoints / TickSize, MidpointRounding.AwayFromZero));
            double initialRiskPoints = initialRiskTicks * TickSize;

            double rawLadderRPoints = LadderRiskDollars1R / (pointValue * EffectiveQuantity());
            double ladderTicks = Math.Max(1, Math.Round(rawLadderRPoints / TickSize, MidpointRounding.AwayFromZero));
            oneRPoints = ladderTicks * TickSize;

            double buffer = Math.Max(0, StopSafetyBufferTicks) * TickSize;
            MarketPosition position = EffectiveMarketPosition();

            // Breach is tested on DESIRED, before the clamp. Testing it after was unsatisfiable --
            // Math.Min(desired, marketRef - buffer) is always < marketRef -- so this branch was dead
            // code and a position whose risk level the market had already passed got a clamped stop
            // pinned to the market instead of an exit. Fixed 2026-07-20.
            if (position == MarketPosition.Long) {
                double desired = CurrentInstrument.MasterInstrument.RoundToTickSize(entryPrice - initialRiskPoints);
                double bid = GetCurrentBid(CurrentBarsInProgressIndex());
                double marketRef = bid > 0 ? bid : Close[0];
                double maxLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef - buffer);

                if (desired >= marketRef) {
                    stopInitialized = true;
                    InitializeFavorableTracking();
                    SubmitGuardedExitLong("InitialStopAlreadyBreached", EntrySignalForPosition());
                    return;
                }

                currentStopPrice = Math.Min(desired, maxLegalStop);
            }
            else if (position == MarketPosition.Short) {
                double desired = CurrentInstrument.MasterInstrument.RoundToTickSize(entryPrice + initialRiskPoints);
                // Ask, not bid: buy-to-cover stops are validated against the ask (see TryUpdateStopSafely).
                double ask = GetCurrentAsk(CurrentBarsInProgressIndex());
                double marketRef = ask > 0 ? ask : Close[0];
                double minLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef + buffer);

                if (desired <= marketRef) {
                    stopInitialized = true;
                    InitializeFavorableTracking();
                    SubmitGuardedExitShort("InitialStopAlreadyBreached", EntrySignalForPosition());
                    return;
                }

                currentStopPrice = Math.Max(desired, minLegalStop);
            }
            else {
                return;
            }

            SubmitStopLossIfChanged(EntrySignalForPosition(), currentStopPrice);
            stopInitialized = true;
            InitializeFavorableTracking();
        }

        private double GetLockedRForOpenProfitR(double openProfitR) {
            if (openProfitR < 0.5) return 0.0;
            if (openProfitR < 0.75) return 0.10 + ((openProfitR - 0.50) / 0.25) * (0.25 - 0.10);
            if (openProfitR < 1.0) return 0.25 + ((openProfitR - 0.75) / 0.25) * (0.50 - 0.25);

            int lowerR = (int)Math.Floor(openProfitR);
            if (lowerR >= 24) return GetLockedRAtIntegerR(24);

            double lockedLow = GetLockedRAtIntegerR(lowerR);
            double lockedHigh = GetLockedRAtIntegerR(lowerR + 1);
            double fraction = openProfitR - lowerR;
            return lockedLow + fraction * (lockedHigh - lockedLow);
        }

        // Index i holds the locked-R value for completedR == i+1; out-of-range falls back to 0.
        private static readonly double[] LockedRAtIntegerRTable = {
            0.50, 1.40, 2.25, 3.08, 3.95, 4.86, 5.81, 6.72, 7.65, 8.60, 9.57, 10.56,
            11.57, 12.60, 13.65, 14.72, 15.81, 16.92, 18.05, 19.20, 20.37, 21.56, 22.77, 24.00
        };

        private double GetLockedRAtIntegerR(int completedR) {
            return (completedR >= 1 && completedR <= LockedRAtIntegerRTable.Length)
                ? LockedRAtIntegerRTable[completedR - 1]
                : 0.0;
        }

        private void ResetDailyBudgetOnEnable() {
            dailyRealizedPnLDollars = 0.0;
            dailyLossLimitHit = false;
            dailyLossExitPending = false;
            hasDailyLossBaseline = true;
            dailyBaselineDate = DateTime.MinValue;
            dailyRiskBlockLogBar = -1;
        }

        private bool HandleFlattenOnEnable() {
            if (!flattenOnEnablePending)
                return false;

            flattenOnEnablePending = false;

            // Log a best-effort final ML exit sample before ResetAllTradeState() wipes _exitTradeId on restart mid-trade.
            if (MlExitTrackingValid())
                LogMlExitFinalSample(Close[0], "EnableReset");

            ResetAllTradeState();

            if (Account != null && CurrentInstrument != null) {
                Position accountPosition = GetAccountPositionForInstrument();

                if (accountPosition != null
                    && accountPosition.MarketPosition != MarketPosition.Flat
                    && accountPosition.Quantity > 0) {
                    OrderAction closeAction = accountPosition.MarketPosition == MarketPosition.Long
                        ? OrderAction.Sell
                        : OrderAction.BuyToCover;
                    int closeQuantity = Math.Abs(accountPosition.Quantity);
                    Order closeOrder = Account.CreateOrder(CurrentInstrument, closeAction, OrderType.Market, TimeInForce.Day, closeQuantity, 0, 0, string.Empty, "EnableAccountClose", null);

                    _lastExitReason = "manual";
                    Account.Submit(new[] { closeOrder });
                }
                else {
                }

                return true;
            }

            if (CurrentPosition.MarketPosition == MarketPosition.Long) {
                _lastExitReason = "manual";
                if (EnableMultiSymbolMode)
                    ExitLong(CurrentBarsInProgressIndex(), EffectiveQuantity(), "EnableResetLong", "");
                else
                    ExitLong("EnableResetLong", "");
                return true;
            }

            if (CurrentPosition.MarketPosition == MarketPosition.Short) {
                _lastExitReason = "manual";
                if (EnableMultiSymbolMode)
                    ExitShort(CurrentBarsInProgressIndex(), EffectiveQuantity(), "EnableResetShort", "");
                else
                    ExitShort("EnableResetShort", "");
                return true;
            }

            return true;
        }

        private Position GetAccountPositionForInstrument() {
            return CurrentInstrument == null ? null : FindAccountPositionForInstrument(CurrentInstrument.FullName);
        }

        private Position FindAccountPositionForInstrument(string instrumentFullName) {
            if (Account == null || Account.Positions == null || string.IsNullOrEmpty(instrumentFullName))
                return null;

            foreach (Position accountPosition in Account.Positions) {
                if (accountPosition == null || accountPosition.Instrument == null)
                    continue;

                if (accountPosition.Instrument.FullName == instrumentFullName)
                    return accountPosition;
            }

            return null;
        }

        // Realized+unrealized daily P&L for an arbitrary symbol context, via Closes[] rather than the bare Close[0] indexer.
        private double ContextDailyPnL(SymbolContext ctx) {
            bool isActive = ctx == activeContext;
            double realized = isActive ? dailyRealizedPnLDollars : ctx.DailyRealizedPnLDollars;

            // Guard: this ticker's series may not have a bar yet; realized-only until it does.
            if (ctx.BarsInProgressIndex >= Closes.Length || Closes[ctx.BarsInProgressIndex].Count == 0)
                return realized;

            double markPrice = Closes[ctx.BarsInProgressIndex][0];

            Position accountPosition = FindAccountPositionForInstrument(ctx.InstrumentFullName);
            if (accountPosition != null && accountPosition.MarketPosition != MarketPosition.Flat)
                return realized + accountPosition.GetUnrealizedProfitLoss(PerformanceUnit.Currency, markPrice);

            int watchdogDirection = isActive ? watchdogTradeDirection : ctx.WatchdogTradeDirection;
            double watchdogEntry = isActive ? watchdogEntryPrice : ctx.WatchdogEntryPrice;
            int watchdogQty = isActive ? watchdogTradeQuantity : ctx.WatchdogTradeQuantity;

            if (watchdogDirection == 0 || watchdogEntry <= 0 || BarsArray == null
                || ctx.BarsInProgressIndex >= BarsArray.Length || BarsArray[ctx.BarsInProgressIndex] == null
                || BarsArray[ctx.BarsInProgressIndex].Instrument == null
                || BarsArray[ctx.BarsInProgressIndex].Instrument.MasterInstrument == null)
                return realized;

            double pointValue = BarsArray[ctx.BarsInProgressIndex].Instrument.MasterInstrument.PointValue;
            if (pointValue <= 0)
                return realized;

            double points = watchdogDirection > 0 ? markPrice - watchdogEntry : watchdogEntry - markPrice;
            return realized + points * pointValue * Math.Max(1, watchdogQty);
        }

        // Sum of realized+unrealized P&L across enabled tickers for the combined loss check; falls back to this instance's own P&L in single-instrument mode.
        private double CombinedDailyPnL() {
            if (symbolContexts.Count == 0)
                return CurrentDailyTotalPnL();

            double total = 0.0;
            foreach (SymbolContext ctx in symbolContexts)
                total += ContextDailyPnL(ctx);
            return total;
        }

        // Flattens and blocks every enabled ticker for the day, independent of each ticker's own 3x-1R max loss.
        private void TriggerCombinedDailyLossLimit(double combinedPnL) {
            combinedLossLimitHit = true;
            D("COMBINED DAILY MAX LOSS HIT across all tickers - flattening all. CombinedPnL=" + combinedPnL);

            if (symbolContexts.Count == 0) {
                // Single-instrument mode: flatten this instance directly.
                HandleDailyLossLimit(combinedPnL);
                return;
            }

            foreach (SymbolContext ctx in symbolContexts) {
                // activeContext is already loaded for this bar; call directly to avoid RunWithContext discarding unsaved mutations.
                if (ctx == activeContext) {
                    HandleDailyLossLimit(ContextDailyPnL(ctx));
                    continue;
                }

                SymbolContext capturedCtx = ctx;
                RunWithContext(capturedCtx.BarsInProgressIndex, () => HandleDailyLossLimit(ContextDailyPnL(capturedCtx)));
            }
        }

        private void UpdateDailyLossLimit() {
            if (dailyBaselineDate != Time[0].Date) {
                dailyRealizedPnLDollars = 0.0;
                dailyLossLimitHit = false;
                dailyLossExitPending = false;
                hasDailyLossBaseline = true;
                dailyBaselineDate = Time[0].Date;
            }

            // Combined-limit reset uses the primary series' clock (Times[0][0]) so all tickers share one reset instant.
            DateTime combinedDate = Times[0].Count > 0 ? Times[0][0].Date : Time[0].Date;
            if (combinedBaselineDate != combinedDate) {
                combinedBaselineDate = combinedDate;
                combinedLossLimitHit = false;
            }

            double dailyTotalPnL = CurrentDailyTotalPnL();

            // Layer 1: per-ticker daily max loss = 3x this template's 1R risk; applies unconditionally once a template is loaded.
            if (RiskDollars1R > 0 && dailyTotalPnL <= -3.0 * RiskDollars1R)
                HandleDailyLossLimit(dailyTotalPnL);

            // Layer 2: combined limit across all enabled tickers, independent of Layer 1.
            if (!combinedLossLimitHit && DailyLossLimit != 0) {
                double combinedPnL = CombinedDailyPnL();
                if (combinedPnL <= DailyLossLimit)
                    TriggerCombinedDailyLossLimit(combinedPnL);
            }
        }

        private double CurrentDailyTotalPnL() {
            double realized = dailyRealizedPnLDollars;
            double unrealized = EffectiveUnrealizedPnl(Close[0]);

            return realized + unrealized;
        }

        private int dailyRiskBlockLogBar = -1;

        private bool HasDailyEntryRiskBudget() {
            double required = Math.Max(0, DailyEntryRiskDollars) + Math.Max(0, DailyEntrySlippageDollars);

            if (RiskDollars1R > 0) {
                double perTickerRemaining = CurrentDailyTotalPnL() - (-3.0 * RiskDollars1R);
                if (perTickerRemaining < required)
                    return LogDailyRiskBlock(perTickerRemaining, required);
            }

            if (DailyLossLimit != 0) {
                double combinedRemaining = CombinedDailyPnL() - DailyLossLimit;
                if (combinedRemaining < required)
                    return LogDailyRiskBlock(combinedRemaining, required);
            }

            return true;
        }

        private bool LogDailyRiskBlock(double remaining, double required) {
            if (dailyRiskBlockLogBar != CurrentContextBar) {
                dailyRiskBlockLogBar = CurrentContextBar;
                if (DebugMode)
                    Print(OutputTimePrefix() + "ENTRY BLOCKED " + OutputContext() + ": Daily budget " + remaining.ToString("C") + " < required " + required.ToString("C"));
            }
            return false;
        }

        // === Max day margin cap ===
        // Caps how much day-trading (intraday) margin may be committed at one time. This is a
        // concurrent-exposure cap, not a cumulative daily tally: with a $1500 cap and ES/RTY/YM at
        // $500 each, all three can be open together but a fourth is blocked until one closes.
        //
        // Margin rates are read live from NinjaTrader on every entry attempt rather than hard-coded,
        // because the broker can change a contract's day margin at any time (often ahead of a
        // holiday or an expiry) and the cap has to move with it.
        //
        // "Committed" counts every open position on the ACCOUNT, not just this strategy's: other
        // temalimit instances, other strategies and manual trades all draw on the same account
        // margin, so anything less would let the cap be exceeded from outside. Working entry orders
        // are added on top -- without that, several symbols could each clear the gate in the same
        // instant and then all fill, landing well past the cap.
        private int dayMarginBlockLogBar = -1;

        private bool HasDayMarginBudget(bool isLong) {
            if (!EnableMaxDayMargin || MaxDayMarginDollars <= 0)
                return true;

            try {
                double perContract = PerContractDayMargin(CurrentInstrument, isLong);

                // Fail closed on an unknown rate. Treating it as $0 would silently exempt that
                // instrument from the cap; blocking is visible and names its own fix.
                // Message names the real fix. The old text said "set it in Instruments > X > Risk",
                // which cannot work: NinjaTrader has no per-instrument margin field, and Account.Risk
                // is NULL on every account, so that route never populates anything this reads.
                if (perContract <= 0)
                    return LogDayMarginBlock("no day margin known for " + ResolveTickerName()
                        + " - add it to HardcodedDayMargin in temalimit.cs, or turn off Enable Max Day Margin");

                double required = perContract * Math.Max(1, Contracts);

                string unmeasuredSymbol;
                double committed = CommittedDayMargin(out unmeasuredSymbol);

                // Same reasoning as the unknown-rate case above, one step out: if any open position
                // cannot be priced, the committed total is an under-count and the cap is unenforceable.
                if (unmeasuredSymbol != null)
                    return LogDayMarginBlock("an open " + unmeasuredSymbol + " position has no day margin configured in"
                        + " NinjaTrader, so committed margin cannot be measured - set it in Instruments > "
                        + unmeasuredSymbol + " > Risk, or turn off Enable Max Day Margin");

                if (committed + required > MaxDayMarginDollars)
                    return LogDayMarginBlock(committed.ToString("C") + " already committed + " + required.ToString("C")
                        + " for this " + ResolveTickerName() + " entry exceeds the " + MaxDayMarginDollars.ToString("C") + " cap");

                return true;
            }
            catch (Exception ex) {
                // A position/risk collection mutated mid-read, or an unanticipated null. Fail closed
                // rather than open an unmeasured position; the next signal re-checks from scratch.
                return LogDayMarginBlock("margin check failed, blocking to stay safe: " + ex.Message);
            }
        }

        // Reports the first open position it cannot price via unmeasuredSymbol, so the caller can
        // refuse to act on a total it knows is incomplete rather than treating that symbol as free.
        private double CommittedDayMargin(out string unmeasuredSymbol) {
            unmeasuredSymbol = null;
            double total = 0.0;

            if (Account != null && Account.Positions != null) {
                foreach (Position accountPosition in Account.Positions) {
                    if (accountPosition == null || accountPosition.Instrument == null
                        || accountPosition.MarketPosition == MarketPosition.Flat)
                        continue;

                    double rate = PerContractDayMargin(accountPosition.Instrument, accountPosition.MarketPosition == MarketPosition.Long);

                    if (rate <= 0 && unmeasuredSymbol == null)
                        unmeasuredSymbol = accountPosition.Instrument.MasterInstrument != null
                            ? accountPosition.Instrument.MasterInstrument.Name : "?";

                    total += rate * Math.Abs(accountPosition.Quantity);
                }
            }

            return total + PendingEntryDayMargin();
        }

        // Our own working entry orders. Mirrors ContextDailyPnL's active-context handling: the live
        // entryOrder field is authoritative for the context currently loaded, ctx.EntryOrder for the rest.
        private double PendingEntryDayMargin() {
            if (symbolContexts.Count == 0)
                return PendingOrderDayMargin(entryOrder);

            double total = 0.0;
            foreach (SymbolContext ctx in symbolContexts)
                total += PendingOrderDayMargin(ctx == activeContext ? entryOrder : ctx.EntryOrder);

            return total;
        }

        // Unfilled remainder only: a partial fill already shows up as an account position, so
        // counting the full order quantity would double-count the filled part.
        private double PendingOrderDayMargin(Order order) {
            if (order == null || order.Instrument == null || !IsWorkingEntryOrderState(order.OrderState))
                return 0.0;

            int remaining = order.Quantity - order.Filled;
            if (remaining <= 0)
                return 0.0;

            return PerContractDayMargin(order.Instrument, order.OrderAction == OrderAction.Buy) * remaining;
        }

        // States where the order can still take margin. Cancel-pending/submitted are excluded: those
        // are on their way out, and holding their margin would block a replacement entry.
        private bool IsWorkingEntryOrderState(OrderState state) {
            return state == OrderState.Initialized
                || state == OrderState.Submitted
                || state == OrderState.Accepted
                || state == OrderState.AcceptedByRisk
                || state == OrderState.Working
                || state == OrderState.PartFilled
                || state == OrderState.TriggerPending
                || state == OrderState.ChangePending
                || state == OrderState.ChangeSubmitted;
        }

        // Hardcoded per-contract day margin, in account currency. NinjaTrader supplies nothing usable:
        // Accounts.Risk is NULL for every account in db/NinjaTrader.sqlite (sim AND live alike), and
        // MasterInstruments has no margin column at all, so both RiskByMasterInstrument lookups below
        // always miss. That made HasDayMarginBudget fail closed on every entry -- account <account> placed
        // ZERO orders on 2026-07-20 while an identically-configured sim account placed 37. Setting it in
        // "Instruments > NQ > Risk" (what the old block message told you to do) cannot fix it, because
        // nothing there feeds Account.Risk. Broker intraday rates as of 2026-07-20; update on a change.
        private static readonly Dictionary<string, double> HardcodedDayMargin =
            new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase) {
                { "NQ",  1000.0 },
                { "ES",   500.0 },
                { "YM",   500.0 },
                { "RTY",  500.0 }
            };

        // Live per-contract day margin. The hardcoded table above wins outright -- it is the only
        // source that actually returns a number here. The NinjaTrader lookups are kept as a fallback
        // for any symbol not in the table: buy and sell rates are read separately because they can
        // legitimately differ; if the side-specific value is unset we fall back to the other side,
        // then to initial margin, so a partly-filled risk record still yields a usable number
        // instead of failing the entry outright.
        private double PerContractDayMargin(Instrument instrument, bool isLong) {
            if (Account == null || instrument == null || instrument.MasterInstrument == null)
                return 0.0;

            double hardcoded;
            if (HardcodedDayMargin.TryGetValue(instrument.MasterInstrument.Name, out hardcoded) && hardcoded > 0)
                return hardcoded;

            InstrumentRisk risk = null;

            if (Account.RiskByMasterInstrument != null)
                Account.RiskByMasterInstrument.TryGetValue(instrument.MasterInstrument, out risk);

            if (risk == null && Account.Risk != null && Account.Risk.ByMasterInstrument != null)
                Account.Risk.ByMasterInstrument.TryGetValue(instrument.MasterInstrument, out risk);

            if (risk == null)
                return 0.0;

            double margin = isLong ? risk.BuyIntradayMargin : risk.SellIntradayMargin;

            if (margin <= 0)
                margin = Math.Max(risk.BuyIntradayMargin, risk.SellIntradayMargin);

            if (margin <= 0)
                margin = risk.InitialMargin;

            return Math.Max(0, margin);
        }

        // Prints regardless of DebugMode: this is a hard trading block, and the misconfiguration
        // case in particular is otherwise invisible. Rate-limited to once per bar.
        private bool LogDayMarginBlock(string detail) {
            if (dayMarginBlockLogBar != CurrentContextBar) {
                dayMarginBlockLogBar = CurrentContextBar;
                Print(OutputTimePrefix() + "ENTRY BLOCKED " + OutputContext() + ": Max day margin - " + detail);
            }
            return false;
        }

        private void HandleDailyLossLimit(double dailyTotalPnL) {
            dailyLossLimitHit = true;
            D("DAILY LOSS LIMIT HIT - flattening. PnL=" + dailyTotalPnL);

            if (entryOrder != null && CanRequestEntryCancel())
                CancelOrder(entryOrder);

            if (dailyLossExitPending)
                return;

            if (EffectiveMarketPosition() == MarketPosition.Long) {
                dailyLossExitPending = true;
                SubmitGuardedExitLong("DailyMaxLossExitLong", EntrySignalForPosition());
            }
            else if (EffectiveMarketPosition() == MarketPosition.Short) {
                dailyLossExitPending = true;
                SubmitGuardedExitShort("DailyMaxLossExitShort", EntrySignalForPosition());
            }
        }


        private bool TryGetSessionEndTimes(out DateTime noNewTradesTime, out DateTime flattenTime) {
            int bip = CurrentBarsInProgressIndex();
            if (CurrentContextBar < 0) {
                noNewTradesTime = DateTime.MaxValue;
                flattenTime = DateTime.MaxValue;
                return false;
            }
            return TryGetSessionEndTimes(bip, Time[0], out noNewTradesTime, out flattenTime);
        }

        // asOf-based overload so idle multi-symbol contexts can be evaluated against wall-clock time.
        private bool TryGetSessionEndTimes(int bip, DateTime asOf, out DateTime noNewTradesTime, out DateTime flattenTime) {
            noNewTradesTime = DateTime.MaxValue;
            flattenTime = DateTime.MaxValue;

            if (BarsArray == null || bip < 0 || bip >= BarsArray.Length || BarsArray[bip] == null)
                return false;

            SessionIterator sessionIterator;
            if (!sessionIteratorsByBarsInProgress.TryGetValue(bip, out sessionIterator) || sessionIterator == null) {
                sessionIterator = new SessionIterator(BarsArray[bip]);
                sessionIteratorsByBarsInProgress[bip] = sessionIterator;
            }

            sessionIterator.GetNextSession(asOf, true);
            DateTime sessionEnd = sessionIterator.ActualSessionEnd;

            if (sessionEnd == DateTime.MinValue || sessionEnd == DateTime.MaxValue)
                return false;

            noNewTradesTime = sessionEnd.AddMinutes(-30);
            flattenTime = sessionEnd.AddMinutes(-18);
            return true;
        }

        // Heartbeat for quiet multi-symbol contexts near the close; driven from OnMarketData so it fires without new bars.
        private void CheckIdleContextSessionFlatten(DateTime asOf) {
            if (!EnableTimeWindow)
                return;

            foreach (SymbolContext ctx in symbolContexts) {
                DateTime noNewTradesTime, flattenTime;
                if (!TryGetSessionEndTimes(ctx.BarsInProgressIndex, asOf, out noNewTradesTime, out flattenTime) || asOf < flattenTime)
                    continue;

                RunWithContext(ctx.BarsInProgressIndex, () => {
                    if (entryOrder != null && CanRequestEntryCancel())
                        CancelOrder(entryOrder);

                    if (EffectiveMarketPosition() == MarketPosition.Long) {
                        _lastExitReason = "time";
                        ExitLong(CurrentBarsInProgressIndex(), EffectiveQuantity(), "TimeExitLong", EntrySignalForPosition());
                    }
                    else if (EffectiveMarketPosition() == MarketPosition.Short) {
                        _lastExitReason = "time";
                        ExitShort(CurrentBarsInProgressIndex(), EffectiveQuantity(), "TimeExitShort", EntrySignalForPosition());
                    }
                });
            }
        }

        private DateTime _lastIdleFlattenCheck = DateTime.MinValue;
        private DateTime _lastOpenTradeStatusHeartbeat = DateTime.MinValue;
        // Manual-flatten logging state: when this instance went realtime (rows older than this in an
        // open-trades file are stale leftovers from a dead instance), which files were already
        // reconciled, and which ticker+account keys already logged a ManualFlatten for the current
        // desync (also read by OnExecutionUpdateCore to suppress a duplicate log if a real exit fill
        // races in).
        private DateTime realtimeStartTime = DateTime.MinValue;
        // Set at State.Configure, read by PrintLifecycleState to report historical-load duration.
        private DateTime enableStartTime = DateTime.MinValue;
        private readonly HashSet<string> reconciledOpenTradeFiles = new HashSet<string>();
        private readonly HashSet<string> manualFlattenLoggedKeys = new HashSet<string>();
        // Mark price each ManualFlatten log used, keyed like manualFlattenLoggedKeys, so a strategy-owned
        // exit fill that races in afterward can correct the row to the real fill (see
        // CorrectManualFlattenLog).
        private readonly Dictionary<string, double> manualFlattenLoggedMarkExit = new Dictionary<string, double>();

        // MUST run before anything reads EffectiveMarketPosition() and acts on "the account is flat but
        // we still hold a position". On a strategy-owned exit fill, OnExecutionUpdateCore leaves watchdog
        // state armed on purpose -- cleanup is deferred to ResetPositionState, far below the top of
        // OnBarUpdateCore -- so until that runs, EffectiveMarketPosition() still reports the CLOSED
        // direction while the account is already flat. That is exactly the "account went flat under us"
        // signature, so WriteOpenTradeStatus logged a phantom ManualFlatten duplicating the trade the fill
        // had just logged, double-counting dailyRealizedPnLDollars and the modes-4/5 template ledger.
        // Requiring the ACCOUNT to be flat too keeps this off restart-recovered positions, where the
        // watchdog is the only copy of the trade and the account still holds it. Genuine external flattens
        // are unaffected: they leave CurrentPosition non-Flat, so EffectiveMarketPosition() resolves on its
        // first branch and never consults the watchdog.
        // Called from BOTH readers. Fixing only OnBarUpdateCore was not enough: the OnMarketData heartbeat
        // below fires every 5 s and beat the next bar on slow series (observed on a 1-minute YM trade --
        // stop filled 08:05:38, next OnBarUpdate 08:06:00, heartbeat logged the phantom in between), while
        // fast tick/volume series happened to reach OnBarUpdate first and looked clean.
        private void ReconcileWatchdogClosedByExitFill() {
            if (CurrentPosition.MarketPosition != MarketPosition.Flat || !HasWatchdogPosition())
                return;

            Position reconcilePosition = GetAccountPositionForInstrument();
            if (reconcilePosition != null && reconcilePosition.MarketPosition != MarketPosition.Flat && reconcilePosition.Quantity > 0)
                return;

            D("WATCHDOG: Clearing watchdog state closed by a strategy exit fill (strategy and account both flat).");
            ResetWatchdogState();
        }

        // Renko/LineBreak/Range/Kagi/PointAndFigure only fire OnBarUpdate on a new bar; heartbeat off OnMarketData keeps position status current between bars.
        private void RefreshOpenTradeStatusHeartbeat(DateTime asOf) {
            if ((asOf - _lastOpenTradeStatusHeartbeat).TotalSeconds < 5)
                return;
            _lastOpenTradeStatusHeartbeat = asOf;

            if (EnableMultiSymbolMode) {
                foreach (SymbolContext ctx in symbolContexts) {
                    RunWithContext(ctx.BarsInProgressIndex, () => {
                        ReconcileWatchdogClosedByExitFill();
                        if (EffectiveMarketPosition() != MarketPosition.Flat)
                            WriteOpenTradeStatus();
                        else
                            ClearOpenTradeStatus();
                    });
                }
            }
            else {
                ReconcileWatchdogClosedByExitFill();
                if (EffectiveMarketPosition() != MarketPosition.Flat)
                    WriteOpenTradeStatus();
                else
                    ClearOpenTradeStatus();
            }
        }

        protected override void OnMarketData(MarketDataEventArgs e) {
            if (State != State.Realtime)
                return;

            // Ladder trail runs per tick, not just per bar -- see UpdateLadderTrailOnTick for why.
            // Last only: bid/ask churn would burn the context switch without moving the excursion.
            if (e.MarketDataType == MarketDataType.Last && e.Price > 0 && SeriesHasOpenPosition(BarsInProgress)) {
                double tickPrice = e.Price;
                if (EnableMultiSymbolMode)
                    RunWithContext(BarsInProgress, () => UpdateLadderTrailOnTick(tickPrice));
                else
                    UpdateLadderTrailOnTick(tickPrice);
            }

            RefreshOpenTradeStatusHeartbeat(e.Time);

            if (!EnableMultiSymbolMode || symbolContexts.Count == 0)
                return;

            if ((e.Time - _lastIdleFlattenCheck).TotalSeconds < 5)
                return;

            _lastIdleFlattenCheck = e.Time;
            CheckIdleContextSessionFlatten(e.Time);
        }

        private bool HandleBlockedTimeWindow() {
            if (!EnableTimeWindow)
                return false;

            DateTime noNewTradesTime;
            DateTime flattenTime;

            if (!TryGetSessionEndTimes(out noNewTradesTime, out flattenTime))
                return false;

            if (Time[0] < noNewTradesTime)
                return false;

            if (entryOrder != null && CanRequestEntryCancel())
                CancelOrder(entryOrder);

            if (Time[0] >= flattenTime) {
                if (EffectiveMarketPosition() == MarketPosition.Long) {
                    _lastExitReason = "time";
                    if (EnableMultiSymbolMode)
                        ExitLong(CurrentBarsInProgressIndex(), EffectiveQuantity(), "TimeExitLong", EntrySignalForPosition());
                    else
                        ExitLong("TimeExitLong", EntrySignalForPosition());
                }
                else if (EffectiveMarketPosition() == MarketPosition.Short) {
                    _lastExitReason = "time";
                    if (EnableMultiSymbolMode)
                        ExitShort(CurrentBarsInProgressIndex(), EffectiveQuantity(), "TimeExitShort", EntrySignalForPosition());
                    else
                        ExitShort("TimeExitShort", EntrySignalForPosition());
                }
            }

            return true;
        }


        private void StartReentryCooldown() {
            lastExitBar = CurrentContextBar;

            if (ReentryCooldownBars > 0)
                reentryBlockedUntilBar = Math.Max(reentryBlockedUntilBar, CurrentContextBar + ReentryCooldownBars);
        }

        private bool IsReentryCooldownActive() {
            if (ReentryCooldownBars <= 0)
                return false;

            if (reentryBlockedUntilBar != int.MinValue && CurrentContextBar < reentryBlockedUntilBar)
                return true;

            if (lastExitBar < 0)
                return false;

            return CurrentContextBar - lastExitBar < ReentryCooldownBars;
        }
        private bool HasWorkingEntryOrder() {
            return entryOrder != null
                && (entryOrder.OrderState == OrderState.Initialized
                    || entryOrder.OrderState == OrderState.Submitted
                    || entryOrder.OrderState == OrderState.Accepted
                    || entryOrder.OrderState == OrderState.Working
                    || entryOrder.OrderState == OrderState.ChangePending
                    || entryOrder.OrderState == OrderState.ChangeSubmitted
                    || entryOrder.OrderState == OrderState.CancelPending
                    || entryOrder.OrderState == OrderState.CancelSubmitted);
        }

        // Tracks the best (closest-to-limit) price seen while entryOrder is still working. AppendNoFillLog
        // uses this to report how many ticks short a no-fill actually missed by, instead of approximating
        // from the placement price alone. Per-tick close, NOT the bar's Low/High: the submission bar's
        // extreme includes ticks from BEFORE the order existed, so a dip earlier in that bar made the miss
        // read ~0 and fed false "would have filled" positives into the Clamp Band Reassess automation.
        // Calculate.OnEachTick means Closes[idx][0] samples every tick from submission onward.
        private void UpdateEntryOrderClosestApproach() {
            if (entryOrder == null)
                return;

            int idx = CurrentBarsInProgressIndex();
            bool isLong = entryOrder.OrderAction == OrderAction.Buy;
            double tickPrice = Closes[idx][0];

            if (entryOrderClosestApproachPrice <= 0.0) {
                entryOrderClosestApproachPrice = tickPrice;
                return;
            }

            entryOrderClosestApproachPrice = isLong
                ? Math.Min(entryOrderClosestApproachPrice, tickPrice)
                : Math.Max(entryOrderClosestApproachPrice, tickPrice);
        }

        private bool CanRequestEntryCancel() {
            return entryOrder != null
                && (entryOrder.OrderState == OrderState.Initialized
                    || entryOrder.OrderState == OrderState.Submitted
                    || entryOrder.OrderState == OrderState.Accepted
                    || entryOrder.OrderState == OrderState.Working);
        }

        // True once the active template falls outside the current session's range (overnight/regular boundary crossed mid-order).
        private bool ActiveTemplateOutsideCurrentSessionBounds() {
            int minTemplate, maxTemplate;
            GetActiveSessionTemplateBounds(out minTemplate, out maxTemplate);
            return _activeTemplateNumber < minTemplate || _activeTemplateNumber > maxTemplate;
        }

        private void CancelExpiredEntryOrder() {
            if (!CanRequestEntryCancel() || entryOrderSubmittedTime == DateTime.MinValue)
                return;

            if (EnableSessionBasedTemplateRange && ActiveTemplateOutsideCurrentSessionBounds()) {
                D("ENTRY LIMIT cancelled: session boundary crossed, template " + _activeTemplateNumber + " no longer in the active session's range.");
                AppendNoFillLog(entryOrder);
                CancelOrder(entryOrder);
                entryOrderSubmittedTime = DateTime.MinValue;

                // Re-clamp into the new session's template range so the next entry doesn't immediately re-trigger this same cancel.
                ApplyTemplate(_activeTemplateNumber);
                ArmTemplateNoFillTimer(false);
                SaveTemplateState();
                return;
            }

            if (EntryOrderExpireMinutes <= 0)
                return;

            if (CurrentClockTime() < entryOrderSubmittedTime.AddMinutes(EntryOrderExpireMinutes))
                return;

            D("ENTRY LIMIT expired. Canceling order " + entryOrder.Name + " after " + EntryOrderExpireMinutes + " minute(s).");
            AppendNoFillLog(entryOrder);
            StartExpireWatch(entryOrder);
            CancelOrder(entryOrder);
            entryOrderSubmittedTime = DateTime.MinValue;
        }
        private DateTime CurrentClockTime() {
            if (State == State.Realtime)
                return DateTime.Now;

            // Time[0] is not indexable before the first OnBarUpdate (CurrentBar == -1): NinjaTrader throws
            // "'barsAgo' needed to be between 0 and -1 but was 0". State.DataLoaded prints reach here
            // (PrintCompileNotificationIfNeeded, the "Template N applied" line), and because OnStateChange
            // swallows exceptions that throw used to abort the rest of OnStateDataLoaded -- leaving the band
            // indicators unbuilt and template rotation uninitialized. Wall clock is the right stand-in there.
            try {
                if (CurrentBar < 0)
                    return DateTime.Now;
                return Time[0];
            }
            catch (Exception) {
                return DateTime.Now;
            }
        }

        // Cached once per context-bar so shadow trades (TryOpenShadowTrades) and a live entry
        // submitted on the same bar (SubmitMlDirectedEntry) share an identical setup_timestamp --
        // two independent CurrentClockTime() reads would otherwise never match, silently keeping
        // /log-template-sample's shadow and live rows for the same real-world setup from ever
        // grouping together.
        private DateTime GetCurrentBarSetupTimestamp() {
            if (currentBarSetupTimestampBar != CurrentContextBar) {
                currentBarSetupTimestamp = CurrentClockTime();
                currentBarSetupTimestampBar = CurrentContextBar;
            }
            return currentBarSetupTimestamp;
        }

        protected override void OnOrderUpdate(
            Order order,
            double limitPrice,
            double stopPrice,
            int quantity,
            int filled,
            double averageFillPrice,
            OrderState orderState,
            DateTime time,
            ErrorCode error,
            string nativeError) {
            if (EnableMultiSymbolMode) {
                SymbolContext ctx = ContextForOrder(order);
                if (ctx != null) {
                    RunWithContext(ctx.BarsInProgressIndex, () => OnOrderUpdateCore(order, limitPrice, stopPrice, quantity, filled, averageFillPrice, orderState, time, error, nativeError));
                    return;
                }
            }

            OnOrderUpdateCore(order, limitPrice, stopPrice, quantity, filled, averageFillPrice, orderState, time, error, nativeError);
        }

        private void OnOrderUpdateCore(
            Order order,
            double limitPrice,
            double stopPrice,
            int quantity,
            int filled,
            double averageFillPrice,
            OrderState orderState,
            DateTime time,
            ErrorCode error,
            string nativeError) {
            if (order == null)
                return;

            if (orderState == OrderState.Working
                && (order.OrderType == OrderType.Limit || order.OrderType == OrderType.StopMarket || order.OrderType == OrderType.StopLimit)
                && IsNtfyNotifiableAccount(order.Account)
                && _ntfyNotifiedPendingOrders.Add(order))
                NotifyPendingOrderCreated(order);

            if (IsTerminalOrderState(orderState))
                _ntfyNotifiedPendingOrders.Remove(order);

            TrackProtectiveExitOrder(order.Name, orderState);

            // With RealtimeErrorHandling=IgnoreAllErrors (see SetDefaults), NT no longer flattens and
            // disables the instance on order errors, so the two stop-loss failure modes are ours to
            // handle. Everything else (entry-order rejects, exit bookkeeping) is covered below.
            if (order.Name == "Stop loss") {
                if (error == ErrorCode.UnableToChangeOrder) {
                    // Rejected ChangeOrder (market moved through the new level in flight): NT keeps
                    // the old stop WORKING at its old price -- the stopPrice arg still carries it.
                    // Resync bookkeeping so the next trail computes off reality, and keep trading.
                    currentStopPrice = stopPrice;
                    lastSubmittedStopPrice = stopPrice;
                    pendingStopSubmitPrice = double.NaN;
                    Print(Name + " stop ChangeOrder rejected; keeping working stop at " + stopPrice
                        + " (" + nativeError + ")");
                }
                else if (orderState == OrderState.Rejected) {
                    // Queued, NOT handled inline. This callback lands BEFORE the position is
                    // registered (2026-07-20 log: reject 00:27:13.127, PositionUpdate 00:27:13.129),
                    // and a managed ExitLong/ExitShort with no position yet on that entry signal is
                    // silently ignored -- which is exactly how two ES shorts ran unprotected. With
                    // Calculate.OnEachTick, EnsureProtectiveStopArmed() picks this up a tick later,
                    // against fresh bid/ask, and decides re-arm vs flatten.
                    pendingStopSubmitPrice = double.NaN;
                    protectiveStopRearmPending = true;
                    Print(Name + " stop-loss order REJECTED with no working stop; re-arm queued ("
                        + nativeError + ")");
                }
                else if (orderState == OrderState.Accepted || orderState == OrderState.Working) {
                    lastSubmittedStopPrice = stopPrice;
                    pendingStopSubmitPrice = double.NaN;
                    protectiveStopRearmPending = false;
                }
                else if (orderState == OrderState.Cancelled) {
                    pendingStopSubmitPrice = double.NaN;
                }
            }

            // Cooldown ownership lives in OnExecutionUpdateCore() only, to avoid duplicating the start call.

            if (IsEntrySignalName(order.Name)
                && (order.OrderAction == OrderAction.Buy || order.OrderAction == OrderAction.SellShort)) {
                if (entryOrder == null || entryOrder != order) {
                    entryOrderSubmittedTime = CurrentClockTime();
                    entryOrderSubmittedMarketPrice = Closes[CurrentBarsInProgressIndex()][0];
                    entryOrderClosestApproachPrice = entryOrderSubmittedMarketPrice;
                }

                entryOrder = order;

                if (orderState == OrderState.Filled
                    || orderState == OrderState.Cancelled
                    || orderState == OrderState.Rejected) {
                    entryOrderSubmittedTime = DateTime.MinValue;
                    entryOrderSubmittedMarketPrice = 0.0;
                    entryOrderClosestApproachPrice = 0.0;
                    entryOrder = null;

                    if ((orderState == OrderState.Cancelled || orderState == OrderState.Rejected) && order.Filled == 0)
                        ClearMlTradeState(order.Instrument.FullName);
                }
            }

            if (orderState == OrderState.Filled
                || orderState == OrderState.Cancelled
                || orderState == OrderState.Rejected) {
                if (order.Name == "TakeProfit24R")
                    takeProfitExitPending = false;
                if (order.Name == "DailyMaxLossExitLong" || order.Name == "DailyMaxLossExitShort")
                    dailyLossExitPending = false;
                if (order.Name == "MLExit" && (orderState == OrderState.Cancelled || orderState == OrderState.Rejected))
                    _mlExitSubmitted = false;
            }
        }

        protected override void OnExecutionUpdate(
            Execution execution,
            string executionId,
            double price,
            int quantity,
            MarketPosition marketPosition,
            string orderId,
            DateTime time) {
            if (EnableMultiSymbolMode) {
                SymbolContext ctx = ContextForExecution(execution);
                if (ctx != null) {
                    RunWithContext(ctx.BarsInProgressIndex, () => OnExecutionUpdateCore(execution, executionId, price, quantity, marketPosition, orderId, time));
                    return;
                }
            }

            OnExecutionUpdateCore(execution, executionId, price, quantity, marketPosition, orderId, time);
        }

        private void OnExecutionUpdateCore(
            Execution execution,
            string executionId,
            double price,
            int quantity,
            MarketPosition marketPosition,
            string orderId,
            DateTime time) {
            if (execution == null || execution.Order == null)
                return;

            // Ntfy push per fill (partial or full), gated on executionId so a re-delivered callback can't double-send.
            if (IsNtfyNotifiableAccount(execution.Order.Account) && _ntfyNotifiedExecutionIds.Add(executionId)) {
                bool ntfyIsEntryExecution = IsEntrySignalName(execution.Order.Name)
                    && (execution.Order.OrderAction == OrderAction.Buy || execution.Order.OrderAction == OrderAction.SellShort);
                bool ntfyIsExitExecution = !ntfyIsEntryExecution
                    && (execution.Order.OrderAction == OrderAction.Sell || execution.Order.OrderAction == OrderAction.BuyToCover);

                if (ntfyIsEntryExecution)
                    NotifyEntryOrderFilled(execution, price, quantity, marketPosition);
                else if (ntfyIsExitExecution)
                    NotifyExitOrderFilled(execution, price, quantity, marketPosition);
            }

            // Gate on Order.Filled/Order.Quantity so multi-fragment fills only re-arm/log trade state once.
            bool orderComplete = execution.Order.Filled >= execution.Order.Quantity;

            bool isEntryFill =
                IsEntrySignalName(execution.Order.Name)
                && (execution.Order.OrderAction == OrderAction.Buy || execution.Order.OrderAction == OrderAction.SellShort)
                && orderComplete;

            bool isExitFill = !isEntryFill
                && (execution.Order.OrderAction == OrderAction.Sell || execution.Order.OrderAction == OrderAction.BuyToCover)
                && orderComplete;

            bool lastExitWasWin = false;

            if (isExitFill) {
                // Direction of the closed position from the order action itself, not activeMlIsLong (unreliable if entry predates this instance).
                bool closedWasLong = execution.Order.OrderAction == OrderAction.Sell;
                if (execution.Order.OrderType == OrderType.StopMarket || execution.Order.OrderType == OrderType.StopLimit) {
                    // Average fill covers multi-fragment stop fills; falls back to this fragment's price.
                    double stopFillPrice = execution.Order.AverageFillPrice > 0 ? execution.Order.AverageFillPrice : price;
                    AppendSlippageLog(execution.Order, stopFillPrice, closedWasLong);
                }
                LogMlTradeOutcome(price);
                // If LogManualFlattenIfNeeded already logged this trade (account flat was observed
                // before this fill callback landed), don't append a duplicate -- instead correct the
                // row it wrote to this fill's real exit signal and price, and true-up the daily PnL it
                // accumulated at the mark. Everything else (ML outcome, slippage, template ledger)
                // still runs once here.
                bool manualFlattenAlreadyLogged = manualFlattenLoggedKeys.Remove(ManualExitTickerAccountKey());
                if (!manualFlattenAlreadyLogged) {
                    AppendDashboardTradeOutcome(price, execution.Order.Quantity, execution.Order.Name, time, closedWasLong);
                    AccumulateDailyRealizedPnL(price, execution.Order.Quantity, closedWasLong);
                }
                else {
                    CorrectManualFlattenLog(price, execution.Order.Quantity, execution.Order.Name, closedWasLong);
                }
                // Fold realized dollars into the per-template ledger (all modes, so 4/5 have history to rank on
                // regardless of the mode that earned it). Same dollars math as AccumulateDailyRealizedPnL, and
                // _activeTemplateNumber is still the template that traded -- rotation is suspended while in a position.
                // Skipped when the manual-flatten path already marked the ledger; CorrectManualFlattenLog above
                // netted that mark to this fill, so writing here again would double-count.
                if (!manualFlattenAlreadyLogged && entryPrice > 0 && CurrentInstrument != null && CurrentInstrument.MasterInstrument != null) {
                    double closedPoints = closedWasLong ? price - entryPrice : entryPrice - price;
                    double closedDollars = closedPoints * CurrentInstrument.MasterInstrument.PointValue * Math.Max(1, execution.Order.Quantity);
                    MarkTemplateRealized(_activeTemplateNumber, closedDollars);
                }
                // Computed before ResetAllTradeState zeroes entryPrice, so the rotation callback still knows win/loss.
                lastExitWasWin = entryPrice > 0
                    && (closedWasLong ? price - entryPrice : entryPrice - price) > 0;
                StartReentryCooldown();
                ClearMlTradeState(CurrentInstrument.FullName);
            }

            if (isEntryFill) {

                RestorePendingMlStateIfMissing();

                activeMlWindowJson = pendingMlWindowJson;
                activeMlTrigger = pendingMlTrigger;
                activeMlPrediction = pendingMlPrediction;
                activeMlConfidence = pendingMlConfidence;
                activeMlSetupDirection = pendingMlSetupDirection;
                activeMlSignal = pendingMlSignal;
                activeMlReversal = pendingMlReversal;
                activeMlIsLong = execution.Order.OrderAction == OrderAction.Buy;
                activeMlSampleLogged = false;
                activeSetupTimestamp = pendingSetupTimestamp;
                pendingMlWindowJson = string.Empty;
                pendingMlTrigger = string.Empty;
                pendingMlPrediction = string.Empty;
                pendingMlConfidence = 0.0;
                pendingMlSetupDirection = string.Empty;
                pendingMlSignal = string.Empty;
                pendingMlReversal = false;
                pendingSetupTimestamp = DateTime.MinValue;

                activeEntrySignal = execution.Order.Name;
                entryPrice = CurrentPosition.AveragePrice > 0 ? CurrentPosition.AveragePrice : price;
                entryFillTime = time;

                double pointValue = CurrentInstrument.MasterInstrument.PointValue;
                int stopQty = Math.Max(1, Math.Max(EffectiveQuantity(), quantity));
                ArmWatchdogFromEntry(execution.Order.OrderAction, price, stopQty, activeEntrySignal);
                double rawInitialRiskPoints = RiskDollars1R / pointValue / stopQty;
                double initialRiskTicks = Math.Max(1, Math.Round(rawInitialRiskPoints / TickSize, MidpointRounding.AwayFromZero));
                double initialRiskPoints = initialRiskTicks * TickSize;

                double rawLadderRPoints = LadderRiskDollars1R / pointValue / stopQty;
                double ladderTicks = Math.Max(1, Math.Round(rawLadderRPoints / TickSize, MidpointRounding.AwayFromZero));
                oneRPoints = ladderTicks * TickSize;

                // Clamp against the live bid/ask (same guard InitializeStopFromPosition uses on restart recovery) so a
                // fast fill can't leave us arming a stop the market has already blown through -- that's what got
                // temalimit/<order-id> rejected and killed on 2026-07-15 ("stop orders can't be placed above the market").
                double buffer = Math.Max(0, StopSafetyBufferTicks) * TickSize;
                bool initialStopBreached;

                // Breach is tested on DESIRED, before the clamp -- see InitializeStopFromPosition for
                // why testing the clamped value could never fire. Fixed 2026-07-20.
                if (execution.Order.OrderAction == OrderAction.Buy) {
                    double desired = CurrentInstrument.MasterInstrument.RoundToTickSize(entryPrice - initialRiskPoints);
                    double bid = GetCurrentBid(CurrentBarsInProgressIndex());
                    double marketRef = bid > 0 ? bid : Close[0];
                    double maxLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef - buffer);
                    initialStopBreached = desired >= marketRef;
                    currentStopPrice = Math.Min(desired, maxLegalStop);
                }
                else {
                    double desired = CurrentInstrument.MasterInstrument.RoundToTickSize(entryPrice + initialRiskPoints);
                    // Ask, not bid: buy-to-cover stops are validated against the ask (see TryUpdateStopSafely).
                    double ask = GetCurrentAsk(CurrentBarsInProgressIndex());
                    double marketRef = ask > 0 ? ask : Close[0];
                    double minLegalStop = CurrentInstrument.MasterInstrument.RoundToTickSize(marketRef + buffer);
                    initialStopBreached = desired <= marketRef;
                    currentStopPrice = Math.Max(desired, minLegalStop);
                }

                StartMlExitTradeTracking(execution.Order.OrderAction, entryPrice, currentStopPrice);

                // Saved after StartMlExitTradeTracking so _exitEntryPrice/_exitOneRPoints/_exitDirection are populated.
                SaveMlTradeState(true, orderId, executionId);

                if (ShowEntryLabels)
                    DrawEntryFillLabel(execution.Order.OrderAction == OrderAction.Buy, entryPrice);

                stopInitialized = true;
                InitializeFavorableTracking();

                if (initialStopBreached) {
                    if (execution.Order.OrderAction == OrderAction.Buy)
                        SubmitGuardedExitLong("InitialStopAlreadyBreached", activeEntrySignal);
                    else
                        SubmitGuardedExitShort("InitialStopAlreadyBreached", activeEntrySignal);
                }
                else {
                    SubmitStopLossIfChanged(activeEntrySignal, currentStopPrice);
                }
            }

            // NT8 passes the EXECUTION's side (Long for buys, Short for sells) as the
            // marketPosition parameter -- it is never Flat -- so the old
            // "marketPosition == MarketPosition.Flat" gate here could never pass and this
            // whole block was dead since inception: no label-0 exit sample was ever logged
            // (all 185k exit_samples rows were label-1 "open" as of 2026-07-18), and
            // OnTradeClosedForTemplateRotation/ResetAllTradeState/ClearOpenTradeStatus never
            // ran on a close (flat-bar ResetPositionState quietly covered the cleanup).
            // ResetAllTradeState/ClearOpenTradeStatus stay off deliberately -- ResetPositionState
            // (runs every flat bar) already covers that cleanup, and calling ResetAllTradeState here
            // too would clear entryOrder/pendingMl* immediately instead of on the next flat bar, an
            // untested timing change not needed to fix rotation.
            // Close-based template rotation turned ON 2026-07-18 (user request, all accounts) --
            // was believed to already be live but had never run: TemplateMode 3 accounts start
            // actually cycling the unused-template pool on every close instead of only on no-fill
            // timeout; TemplateMode 1/2 accounts start reacting to win/loss (stay+longer-window on
            // win, step back on loss) layered on top of no-fill rotation, which was previously the
            // only thing moving templates.
            if (isExitFill) {
                LogMlExitFinalSample(price, execution.Order.Name);
                OnTradeClosedForTemplateRotation(lastExitWasWin);
            }
        }

        private void DrawEntryFillLabel(bool isLong, double price) {
            string tag = "temaEntry_" + CurrentContextBar + "_" + CurrentClockTime().Ticks;
            string text = !string.IsNullOrEmpty(activeEntrySignal)
                ? activeEntrySignal
                : (isLong ? "L" : "S");
            if (ChartControl == null)
                return;

            Brush brush = isLong ? Brushes.LimeGreen : Brushes.OrangeRed;
            double y = isLong ? Low[0] - 8 * TickSize : High[0] + 8 * TickSize;

            Draw.Text(this, tag, false, text + " " + price.ToString("0.00"), 0, y, 0,
                brush, labelFont, System.Windows.TextAlignment.Center, Brushes.Transparent, Brushes.Transparent, 0);
        }

        // Lighter than ResetAllTradeState(): runs every flat bar but must not clear entryOrder/pendingMl* while an entry order is still working.
        private void ResetPositionState() {
            if (CurrentPosition.MarketPosition != MarketPosition.Flat)
                return;

            ResetWatchdogState();

            stopInitialized = false;
            entryPrice = 0;
            oneRPoints = 0;
            currentStopPrice = 0;
            ResetFavorableTracking();
            lastSubmittedStopPrice = double.NaN;
            pendingStopSubmitPrice = double.NaN;
            lastSubmittedStopSignal = string.Empty;
            protectiveStopWorking = false;
            protectiveStopRearmPending = false;
            protectiveStopFlattenPending = false;
            activeEntrySignal = string.Empty;
            activeMlWindowJson = string.Empty;
            activeMlTrigger = string.Empty;
            activeMlPrediction = string.Empty;
            activeMlConfidence = 0.0;
            activeMlSetupDirection = string.Empty;
            activeMlSignal = string.Empty;
            activeMlReversal = false;
            activeMlIsLong = false;
            activeMlSampleLogged = false;
            takeProfitExitPending = false;
            ResetMlExitTracking();
        }

        private void ResetAllTradeState() {
            ResetWatchdogState();
            stopInitialized = false;
            entryPrice = 0;
            oneRPoints = 0;
            currentStopPrice = 0;
            ResetFavorableTracking();
            lastSubmittedStopPrice = double.NaN;
            pendingStopSubmitPrice = double.NaN;
            lastSubmittedStopSignal = string.Empty;
            protectiveStopWorking = false;
            protectiveStopRearmPending = false;
            protectiveStopFlattenPending = false;
            activeEntrySignal = string.Empty;
            activeMlWindowJson = string.Empty;
            activeMlTrigger = string.Empty;
            activeMlPrediction = string.Empty;
            activeMlConfidence = 0.0;
            activeMlSetupDirection = string.Empty;
            activeMlSignal = string.Empty;
            activeMlReversal = false;
            activeMlIsLong = false;
            activeMlSampleLogged = false;
            pendingMlWindowJson = string.Empty;
            pendingMlTrigger = string.Empty;
            pendingMlPrediction = string.Empty;
            pendingMlConfidence = 0.0;
            pendingMlSetupDirection = string.Empty;
            pendingMlSignal = string.Empty;
            pendingMlReversal = false;
            takeProfitExitPending = false;
            entryOrderSubmittedTime = DateTime.MinValue;
            entryOrder = null;
            ResetMlExitTracking();
        }

        private string EntrySignalForPosition() {
            if (!string.IsNullOrEmpty(activeEntrySignal))
                return activeEntrySignal;

            return EffectiveMarketPosition() == MarketPosition.Short ? "S" : "L";
        }

        private bool ConfigIsValid() {
            if (TemaLength < 1 || BBLength < 1 || BBStdDev <= 0 || MfiPeriod < 1 || MfiPriorBars < 0)
                return false;

            if (MfiLongMax < 0 || MfiLongMax > 100 || MfiShortMin < 0 || MfiShortMin > 100)
                return false;

            if (RsiLongMax < 0 || RsiLongMax > 100 || RsiShortMin < 0 || RsiShortMin > 100)
                return false;

            if (RiskDollars1R <= 0 || LadderRiskDollars1R <= 0 || Contracts <= 0)
                return false;

            if (EnableMultiSymbolMode) {
                if ((EnableSymbol2 && string.IsNullOrWhiteSpace(Symbol2Name))
                    || (EnableSymbol3 && string.IsNullOrWhiteSpace(Symbol3Name))
                    || (EnableSymbol4 && string.IsNullOrWhiteSpace(Symbol4Name)))
                    return false;
            }

            return true;
        }

        private string OutputContext() {
            string instrumentName = CurrentInstrument != null ? CurrentInstrument.FullName : "n/a";
            int contextBip = CurrentBarsInProgressIndex();
            string seriesText = "Series " + contextBip;

            try {
                if (BarsArray != null
                    && contextBip >= 0
                    && contextBip < BarsArray.Length
                    && BarsArray[contextBip] != null
                    && BarsArray[contextBip].BarsPeriod != null)
                    seriesText += ": " + BarsArray[contextBip].BarsPeriod;
            }
            catch {
            }

            return "[Strategy=" + Name + " | Ticker=" + instrumentName + " | " + seriesText + "]";
        }
        private string OutputTimePrefix() {
            return CurrentClockTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " | ";
        }

        private void D(string message) {
            if (DebugMode)
                // CurrentClockTime(), not Time[0]: D() is reachable from State.DataLoaded catch blocks
                // (ExportTemplateReferenceIfNeeded, ComputeAtrBoundPullbackTicks), where Time[0] throws
                // and would mask the error being reported. Same DateTime, so the printed format is unchanged.
                Print(CurrentClockTime() + " " + Name + " - " + message);
        }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int TemaLength { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int BBLength { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double BBStdDev { get; set; }

        // Not a UI input; fixed true by ApplyTemplate().
        [Browsable(false)]
        public bool EnableStochRsiCrossFilter { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int StochRsiPeriod { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double StochRsiLowerLine { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double StochRsiUpperLine { get; set; }
        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int StochRsiCrossLookbackBars { get; set; }
        [NinjaScriptProperty] [Display(Name = "Require Fresh Signal After Enable", GroupName = "Entry", Order = 0)]
        public bool RequireFreshSignalAfterEnable { get; set; }

        // Not a UI input; fixed true by ApplyTemplate().
        [Browsable(false)]
        public bool EnableMfiFilter { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int MfiPeriod { get; set; }
        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int MfiPriorBars { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double MfiLongMax { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double MfiShortMin { get; set; }

        // Not a UI input; fixed true by ApplyTemplate().
        [Browsable(false)]
        public bool EnableRsiFilter { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double RsiLongMax { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double RsiShortMin { get; set; }

        // Not a UI input; fixed true by ApplyTemplate().
        [Browsable(false)]
        public bool EnableTemaVwapMidBbCrossEntry { get; set; }
        [NinjaScriptProperty] [Display(Name = "Show Strategy VWAP", GroupName = "Entry", Order = 7)]
        public bool ShowStrategyVwap { get; set; }
        [NinjaScriptProperty] [Display(Name = "Show Strategy Values", GroupName = "Entry", Order = 8)]
        public bool ShowStrategyValues { get; set; }

        
        [NinjaScriptProperty] [Display(Name = "Enable ML Direction Service", GroupName = "ML Direction", Order = 0)]
        public bool EnableMlDirectionService { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Trade Logging", GroupName = "ML Direction", Order = 1)]
        public bool EnableMlTradeLogging { get; set; }
        [NinjaScriptProperty] [Display(Name = "ML Service URL", GroupName = "ML Direction", Order = 2)]
        public string MlServiceUrl { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double MlMinConfidence { get; set; }
        [NinjaScriptProperty] [Range(5, 200)] [Display(Name = "ML Window Bars", GroupName = "ML Direction", Order = 4)]
        public int MlWindowBars { get; set; }
        [NinjaScriptProperty] [Range(100, 10000)] [Display(Name = "ML HTTP Timeout Ms", GroupName = "ML Direction", Order = 5)]
        public int MlHttpTimeoutMs { get; set; }
        [NinjaScriptProperty] [Range(1, 3600)] [Display(Name = "No Trade Log Interval (sec)", GroupName = "ML Direction", Order = 6)]
        public int NoTradeLogIntervalSeconds { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Historical Backfill", GroupName = "ML Direction", Order = 5)]
        public bool EnableMlHistoricalBackfill { get; set; }
        [NinjaScriptProperty] [Range(1, 200)] [Display(Name = "ML Backfill Horizon Bars", GroupName = "ML Direction", Order = 6)]
        public int MlBackfillHorizonBars { get; set; }
        [NinjaScriptProperty] [Range(0, 1000)] [Display(Name = "ML Backfill Min Move Ticks", GroupName = "ML Direction", Order = 7)]
        public int MlBackfillMinMoveTicks { get; set; }
        [NinjaScriptProperty] [Range(1, 10000)] [Display(Name = "ML Backfill Max Samples", GroupName = "ML Direction", Order = 8)]
        public int MlBackfillMaxSamples { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Exit Sample Logging", GroupName = "ML Exit Model", Order = 0)]
        public bool EnableMlExitSampleLogging { get; set; }
        [NinjaScriptProperty] [Display(Name = "ML Exit Server URL", GroupName = "ML Exit Model", Order = 1)]
        public string MlExitServerUrl { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Exit Recommendations", GroupName = "ML Exit Model", Order = 2)]
        public bool EnableMlExitRecommendations { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Exit Control", GroupName = "ML Exit Model", Order = 3)]
        public bool EnableMlExitControl { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double MlExitHoldThreshold { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int MinBarsBeforeMlExit { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public double MinUnrealizedRForMlExit { get; set; }
        [NinjaScriptProperty] [Range(1, 50)] [Display(Name = "ML Exit Signal Cooldown Bars", GroupName = "ML Exit Model", Order = 7)]
        public int MlExitSignalCooldownBars { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int PullbackTicks { get; set; }

        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int EntryOrderExpireMinutes { get; set; }

        // Not a UI input; controlled by ApplyTemplate() (per-ticker range).
        [Browsable(false)]
        public double RiskDollars1R { get; set; }

        // Not a UI input; shared value with DailyEntryRiskDollars via ApplyTemplate().
        [Browsable(false)]
        public double LadderRiskDollars1R { get; set; }

        // Not a UI input; fixed at 1 by ApplyTemplate().
        [Browsable(false)]
        public int Contracts { get; set; }

        // Combined P&L across all enabled tickers vs. this limit, independent of each ticker's own automatic 3x-1R max loss.
        [NinjaScriptProperty] [Range(-100000.0, 0.0)] [Display(Name = "Combined Daily Max Loss - All Tickers (0 disabled)", GroupName = "Risk", Order = 3)]
        public double DailyLossLimit { get; set; }

        // Caps total day margin committed at once across the whole account; per-contract rates are
        // read live from NinjaTrader's instrument risk settings. See HasDayMarginBudget.
        [NinjaScriptProperty] [Display(Name = "Enable Max Day Margin", GroupName = "Risk", Order = 4)]
        public bool EnableMaxDayMargin { get; set; }

        [NinjaScriptProperty] [Range(0.0, 1000000.0)] [Display(Name = "Max Day Margin - All Tickers ($, 0 disabled)", GroupName = "Risk", Order = 5)]
        public double MaxDayMarginDollars { get; set; }

        // Not a UI input; shared value with LadderRiskDollars1R via ApplyTemplate().
        [Browsable(false)]
        public double DailyEntryRiskDollars { get; set; }

        // Not a UI input; controlled by ApplyTemplate() (10% of shared risk value).
        [Browsable(false)]
        public double DailyEntrySlippageDollars { get; set; }
        // Not a UI input; controlled by ApplyTemplate().
        [Browsable(false)]
        public int ReentryCooldownBars { get; set; }

        // Hidden from UI; fixed at its SetDefaults value (mechanical, not signal-quality).
        [Browsable(false)]
        public int MinStopMoveTicks { get; set; }

        // Hidden from UI; fixed at its SetDefaults value (mechanical, not signal-quality).
        [Browsable(false)]
        public int StopSafetyBufferTicks { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Time Window", GroupName = "Session", Order = 0)]
        public bool EnableTimeWindow { get; set; }

        // Retired: the no-trade/flatten window is computed in TryGetSessionEndTimes; hidden rather than removed for workspace compatibility.
        [Browsable(false)]
        public int SessionExitTime { get; set; }
        [Browsable(false)]
        public int ResumeTime { get; set; }
        [NinjaScriptProperty] [Display(Name = "Debug Mode", GroupName = "Debug", Order = 0)]
        public bool DebugMode { get; set; }
        [NinjaScriptProperty] [Display(Name = "Show Entry Labels", GroupName = "Debug", Order = 1)]
        public bool ShowEntryLabels { get; set; }
        [NinjaScriptProperty] [Range(1, AbsoluteMaxTemplateNumber)] [Display(Name = "Template Number", GroupName = "1. Template Rotation", Order = 0)]
        public int TemplateNumber { get; set; }
        [NinjaScriptProperty] [Range(0, 5)] [Display(Name = "Template Mode (0=Manual, 1=Rotate, 2=Custom Range, 3=Unused Only, 4=Losers First, 5=Winners First)", GroupName = "1. Template Rotation", Order = 1)]
        public int TemplateMode { get; set; }


        [NinjaScriptProperty] [Range(1, AbsoluteMaxTemplateNumber)] [Display(Name = "Max Template Number", GroupName = "1. Template Rotation", Order = 3)]
        public int MaxTemplateNumber { get; set; }
        [NinjaScriptProperty] [Display(Name = "Print Template Changes", GroupName = "1. Template Rotation", Order = 4)]
        public bool PrintTemplateChanges { get; set; }


        [NinjaScriptProperty] [Display(Name = "Custom Template Ranges (e.g. 20-25,1-5)", GroupName = "1. Template Rotation", Order = 12)]
        public string CustomTemplateRanges { get; set; }
        [NinjaScriptProperty] [Display(Name = "Use Custom Rotation Timing", GroupName = "1. Template Rotation", Order = 13)]
        public bool UseCustomRotationTiming { get; set; }
        [NinjaScriptProperty] [Range(0.1, 1440.0)] [Display(Name = "Custom No-Fill Window (minutes)", GroupName = "1. Template Rotation", Order = 14)]
        public double CustomNoFillWindowMinutes { get; set; }
        [NinjaScriptProperty] [Range(0.1, 1440.0)] [Display(Name = "Custom Winner Window (minutes)", GroupName = "1. Template Rotation", Order = 15)]
        public double CustomWinnerWindowMinutes { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable ML Template Selection", GroupName = "1. Template Rotation", Order = 16)]
        public bool EnableMlTemplateSelection { get; set; }

        // Hidden from UI: superseded by Custom Template Ranges (Template Mode 2); values stay fixed at SetDefaults.
        [Browsable(false)]
        public bool EnableSessionBasedTemplateRange { get; set; }
        [Browsable(false)]
        public int RegularMinTemplateNumber { get; set; }
        [Browsable(false)]
        public int RegularMaxTemplateNumber { get; set; }
        [Browsable(false)]
        public int OvernightMinTemplateNumber { get; set; }
        [Browsable(false)]
        public int OvernightMaxTemplateNumber { get; set; }
        [Browsable(false)]
        public int RegularSessionStartTimeLocal { get; set; }
        [Browsable(false)]
        public int RegularSessionEndTimeLocal { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Shadow Evaluation", GroupName = "Shadow Evaluation", Order = 0)]
        public bool EnableShadowEvaluation { get; set; }
        [NinjaScriptProperty] [Range(0, 10)] [Display(Name = "Shadow Fill-Through Ticks", GroupName = "Shadow Evaluation", Order = 1)]
        public int ShadowFillThroughTicks { get; set; }
        [NinjaScriptProperty] [Range(5, 500)] [Display(Name = "Shadow Max Hold Bars", GroupName = "Shadow Evaluation", Order = 2)]
        public int ShadowMaxHoldBars { get; set; }


        [NinjaScriptProperty] [Display(Name = "Enable Multi Symbol Mode", GroupName = "Multi Symbol", Order = 0)]
        public bool EnableMultiSymbolMode { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Primary Symbol", GroupName = "Multi Symbol", Order = 1)]
        public bool EnableSymbol1 { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Symbol 2", GroupName = "Multi Symbol", Order = 2)]
        public bool EnableSymbol2 { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 2 Name", GroupName = "Multi Symbol", Order = 3)]
        public string Symbol2Name { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Symbol 3", GroupName = "Multi Symbol", Order = 4)]
        public bool EnableSymbol3 { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 3 Name", GroupName = "Multi Symbol", Order = 5)]
        public string Symbol3Name { get; set; }
        [NinjaScriptProperty] [Display(Name = "Enable Symbol 4", GroupName = "Multi Symbol", Order = 6)]
        public bool EnableSymbol4 { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 4 Name", GroupName = "Multi Symbol", Order = 7)]
        public string Symbol4Name { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 2 Bars Type", GroupName = "Multi Symbol", Order = 8)]
        public BarsPeriodType Symbol2BarsPeriodType { get; set; }
        [NinjaScriptProperty] [Range(1, 100000)] [Display(Name = "Symbol 2 Bars Value", GroupName = "Multi Symbol", Order = 9)]
        public int Symbol2BarsPeriodValue { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 3 Bars Type", GroupName = "Multi Symbol", Order = 10)]
        public BarsPeriodType Symbol3BarsPeriodType { get; set; }
        [NinjaScriptProperty] [Range(1, 100000)] [Display(Name = "Symbol 3 Bars Value", GroupName = "Multi Symbol", Order = 11)]
        public int Symbol3BarsPeriodValue { get; set; }
        [NinjaScriptProperty] [Display(Name = "Symbol 4 Bars Type", GroupName = "Multi Symbol", Order = 12)]
        public BarsPeriodType Symbol4BarsPeriodType { get; set; }
        [NinjaScriptProperty] [Range(1, 100000)] [Display(Name = "Symbol 4 Bars Value", GroupName = "Multi Symbol", Order = 13)]
        public int Symbol4BarsPeriodValue { get; set; }
    }
}