#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

// Trend-catching strategy for the TCN service on port 8767 (separate from the
// BB/VWAP entry model on 8765 and its LSTM+Transformer exit model). Entry
// gate is rule-based (Donchian breakout, SuperTrend, LinReg slope, ADX,
// Choppiness, Relative Volume); Order Flow Delta and ATR are ML-only
// features, not hard filters. Same gate thresholds and feature set for every
// symbol -- only the per-symbol model weights differ (see MLService_Trend),
// so training data never crosses symbol boundaries.
//
// Multi-instrument: CL, 6E, NQ, ES, FDAX, NKD, BTC, GC, HG each trade fully
// independently on this one strategy instance, following the same
// BarsInProgress-indexed pattern proven in GEX.cs. Attach this strategy to a
// CL chart (Order Flow Delta bars, sized to match ClTrendDeltaValue below,
// default 200/200) -- CL is BarsInProgress 0 implicitly; the other 8 are
// added via AddDataSeries in State.Configure, each with its own delta size
// (Xx TrendDeltaValue parameters) since raw order-flow delta doesn't scale
// across instruments the way ATR-normalized thresholds do.
namespace NinjaTrader.NinjaScript.Strategies
{
    public class TrendTcnStrategy : Strategy
    {
        // PRIMARY series (BarsInProgress 0) must be CL, Order Flow Delta
        // sized to match ClTrendDeltaValue (default 200/200) -- set the
        // instrument to CL when adding this strategy.
        // To use a DIFFERENT instrument as primary instead: swap which
        // constant equals 0 below (and which one becomes 8), move that
        // instrument's AddDataSeries(...) call out of State.Configure, and
        // update ValidateStartupConfiguration()'s expected-root check to
        // match. This mirrors the same hardcoded-primary convention already
        // used in fulltwenties.cs/twentyfourseven.cs in this codebase.
        private const int CL = 0, E6 = 1, NQ = 2, ES = 3, FDAX = 4, NKD = 5, BTC = 6, GC = 7, HG = 8;
        private const int InstrumentCount = 9;
        private static readonly string[] ShortNames = { "CL", "6E", "NQ", "ES", "FDAX", "NKD", "BTC", "GC", "HG" };

        // Rollover conventions, reused from fulltwenties.cs's proven system
        // rather than a generic approximation. MonthlyGeneric (HG) has no
        // proven convention in this codebase and is a rougher fallback --
        // verify HG's resolved symbol carefully before trusting it live.
        private enum ContractRolloverCycle
        {
            Quarterly,
            EvenMonths,
            MonthlyCrude,
            MonthlyLastFriday, // CME Bitcoin futures (BTC/MBT): every month, expires last Friday of the month
            MonthlyGeneric
        }

        private bool startupValidationFailed;


        private readonly object mlHttpErrorPrintLock = new object();
        private DateTime lastMlHttpErrorPrintUtc = DateTime.MinValue;
        private const int MlHttpErrorPrintThrottleSeconds = 30;
        private class PendingCandidate
        {
            public int SignalBar;
            public string Direction;
            public double PriceAtSignal;
            public double AtrAtSignal;
            public double[][] Window;
            public bool IsNearMiss;
            public int MatchCount = 6;
        }

        private sealed class InstrumentTrendState
        {
            public string ShortName;
            public int Bip;
            public bool UsesEarlyFlatten;
            public DateTime NoNewTradesTime;
            public DateTime FlattenTime;
            public SessionIterator SessionIterator;

            // indicators (bound to BarsArray[Bip])
            public ADX Adx;
            public ATR Atr;
            public ATR SuperTrendAtr;
            public ChoppinessIndex Chop;
            public DonchianChannel Donchian;
            public LinRegSlope LinRegSlope;
            public SMA VolumeSma;

            // manual SuperTrend state (no built-in NT8 indicator for this)
            public double StFinalUpper = double.NaN;
            public double StFinalLower = double.NaN;
            public int StDirection = 1; // 1 = uptrend, -1 = downtrend

            // ML feature window / pending training candidates
            public readonly List<double[]> FeatureWindow = new List<double[]>();
            public readonly List<PendingCandidate> PendingCandidates = new List<PendingCandidate>();
            public int LastPredictionBar = int.MinValue;
            public int LastDecayCheckBar = int.MinValue;

            // active trade
            public string ActiveDirection = string.Empty;
            public string ActiveEntrySignal = string.Empty;
            public double ActiveEntryPrice;
            public DateTime ActiveEntryFillTime = DateTime.MinValue;
            public int ActiveQuantity;
            public int ActiveEntryBar = int.MinValue;
            public double ActiveExitPrice;
            public int ActiveExitQuantity;
            public string ActiveExitSignal = string.Empty;
            public DateTime ActiveExitTime = DateTime.MinValue;
            public int LastNearMissBar = int.MinValue;
        }

        private readonly InstrumentTrendState[] inst = new InstrumentTrendState[InstrumentCount];
        private static readonly object dashboardTradeLogLock = new object();

        #region Parameters
        [NinjaScriptProperty]
        [Display(Name = "Debug Mode", GroupName = "Debug", Order = 0)]
        public bool DebugMode { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "ML Service URL", GroupName = "Trend ML", Order = 0)]
        public string MlServiceUrl { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Data Series Label", GroupName = "Trend ML", Order = 1,
            Description = "Sent to the service as bars_period, e.g. 'Order Flow Delta'. Keep identical across all charts of the same series to avoid splitting training data.")]
        public string DataSeriesLabel { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 1.0)]
        [Display(Name = "Min Confidence", GroupName = "Trend ML", Order = 2)]
        public double MinConfidence { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 1.0)]
        [Display(Name = "Confidence Decay Exit Threshold", GroupName = "Trend ML", Order = 3)]
        public double ConfidenceDecayThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Feature Window Size", GroupName = "Trend ML", Order = 4,
            Description = "Must match WINDOW_SIZE in MLService_Trend/trend_model.py")]
        public int WindowSize { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Label Lookahead Bars", GroupName = "Trend ML", Order = 5,
            Description = "Bars to wait after a candidate fires before scoring it +2ATR/-1ATR for training.")]
        public int LabelLookaheadBars { get; set; }

        [NinjaScriptProperty]
        [Range(100, 60000)]
        [Display(Name = "ML HTTP Timeout (ms)", GroupName = "Trend ML", Order = 6)]
        public int MlHttpTimeoutMs { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Sample Logging", GroupName = "Trend ML", Order = 7)]
        public bool EnableSampleLogging { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Donchian Period", GroupName = "Entry Filters", Order = 0)]
        public int DonchianPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "SuperTrend ATR Period", GroupName = "Entry Filters", Order = 1)]
        public int SuperTrendAtrPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "SuperTrend Multiplier", GroupName = "Entry Filters", Order = 2)]
        public double SuperTrendMultiplier { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "LinReg Slope Period", GroupName = "Entry Filters", Order = 3)]
        public int LinRegSlopePeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.001, 5.0)]
        [Display(Name = "LinReg Slope ATR Multiple", GroupName = "Entry Filters", Order = 4,
            Description = "Long requires slope > this * ATR; short requires slope < -this * ATR.")]
        public double LinRegSlopeAtrMultiple { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "ADX Period", GroupName = "Entry Filters", Order = 5)]
        public int AdxPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 100.0)]
        [Display(Name = "ADX Threshold", GroupName = "Entry Filters", Order = 6)]
        public double AdxThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Choppiness Period", GroupName = "Entry Filters", Order = 7)]
        public int ChoppinessPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 100.0)]
        [Display(Name = "Choppiness Threshold (max)", GroupName = "Entry Filters", Order = 8)]
        public double ChoppinessThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Relative Volume Period", GroupName = "Entry Filters", Order = 9)]
        public int RelVolumePeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Relative Volume Threshold (min)", GroupName = "Entry Filters", Order = 10)]
        public double RelVolumeThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "ATR Period", GroupName = "Entry Filters", Order = 11)]
        public int AtrPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Stop ATR Multiplier", GroupName = "Risk", Order = 0)]
        public double StopAtrMultiplier { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Target ATR Multiplier", GroupName = "Risk", Order = 1)]
        public double TargetAtrMultiplier { get; set; }

        [NinjaScriptProperty]
        [Range(1, 1000)]
        [Display(Name = "Contracts", GroupName = "Risk", Order = 2)]
        public int Contracts { get; set; }

        // Per-instrument Order Flow Delta sizing. Raw delta/reversal counts
        // don't scale across instruments the way ATR-normalized thresholds
        // do -- ES's volume dwarfs BTC's -- so each instrument gets its own
        // pair instead of sharing one global value.
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "CL Trend Delta / Reversal", GroupName = "Data Series", Order = 0,
            Description = "CL is the PRIMARY series -- set the chart's own Order Flow Delta bar type to this value manually; this field is used only to validate it matches at startup.")]
        public int ClTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "6E Trend Delta / Reversal", GroupName = "Data Series", Order = 1)]
        public int E6TrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "NQ Trend Delta / Reversal", GroupName = "Data Series", Order = 2)]
        public int NqTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "ES Trend Delta / Reversal", GroupName = "Data Series", Order = 3)]
        public int EsTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "FDAX Trend Delta / Reversal", GroupName = "Data Series", Order = 4)]
        public int FdaxTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "NKD Trend Delta / Reversal", GroupName = "Data Series", Order = 5)]
        public int NkdTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "BTC Trend Delta / Reversal", GroupName = "Data Series", Order = 6)]
        public int BtcTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "GC Trend Delta / Reversal", GroupName = "Data Series", Order = 7)]
        public int GcTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "HG Trend Delta / Reversal", GroupName = "Data Series", Order = 8)]
        public int HgTrendDeltaValue { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "6E Symbol", GroupName = "Instruments", Order = 0)]
        public string E6Symbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "NQ Symbol", GroupName = "Instruments", Order = 1)]
        public string NqSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "ES Symbol", GroupName = "Instruments", Order = 2)]
        public string EsSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "FDAX Symbol", GroupName = "Instruments", Order = 3)]
        public string FdaxSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "NKD Symbol", GroupName = "Instruments", Order = 4)]
        public string NkdSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "BTC Symbol", GroupName = "Instruments", Order = 5)]
        public string BtcSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "GC Symbol", GroupName = "Instruments", Order = 6)]
        public string GcSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "HG Symbol", GroupName = "Instruments", Order = 7)]
        public string HgSymbol { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Auto-Resolve Contract Month", GroupName = "Instruments", Order = 8)]
        public bool AutoResolveContractMonth { get; set; }

        [NinjaScriptProperty]
        [Range(0, 30)]
        [Display(Name = "Rollover Days Before Expiry", GroupName = "Instruments", Order = 9,
            Description = "Days before each instrument's approximate expiry date (3rd Friday for quarterly, last Friday for BTC, 3rd-last business day for GC, ~3 business days before the 25th of the prior month for CL, 1st of contract month for HG). Approximation, not an exact exchange notice-date calendar -- verify resolved symbols before trusting live.")]
        public int RolloverDaysBeforeExpiry { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Flatten Hour", GroupName = "Session", Order = 0,
            Description = "Applies to CL, 6E, NQ, ES, NKD, BTC, GC, HG.")]
        public int FlattenHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Flatten Minute", GroupName = "Session", Order = 1)]
        public int FlattenMinute { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "FDAX Flatten Hour", GroupName = "Session", Order = 2,
            Description = "FDAX only -- its intraday margin end time is earlier than the other 8.")]
        public int EarlyFlattenHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "FDAX Flatten Minute", GroupName = "Session", Order = 3)]
        public int EarlyFlattenMinute { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable CL", GroupName = "Ticker Enable", Order = 0)]
        public bool EnableCl { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable 6E", GroupName = "Ticker Enable", Order = 1)]
        public bool EnableE6 { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable NQ", GroupName = "Ticker Enable", Order = 2)]
        public bool EnableNq { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable ES", GroupName = "Ticker Enable", Order = 3)]
        public bool EnableEs { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable FDAX", GroupName = "Ticker Enable", Order = 4)]
        public bool EnableFdax { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable NKD", GroupName = "Ticker Enable", Order = 5)]
        public bool EnableNkd { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable BTC", GroupName = "Ticker Enable", Order = 6)]
        public bool EnableBtc { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable GC", GroupName = "Ticker Enable", Order = 7)]
        public bool EnableGc { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable HG", GroupName = "Ticker Enable", Order = 8)]
        public bool EnableHg { get; set; }
        #endregion

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Trend-catching TCN strategy: Donchian/SuperTrend/ADX/Choppiness/RelVol entry gate, Order Flow Delta + ATR as ML-only features, confirmed by the port-8767 trend ML service. Multi-instrument: CL, 6E, NQ, ES, FDAX, NKD, BTC, GC, HG each trade independently.";
                Name = "TrendTcnStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.UniqueEntries;
                IsExitOnSessionCloseStrategy = false;
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage = 0;
                StartBehavior = StartBehavior.WaitUntilFlat;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                IncludeTradeHistoryInBacktest = true;
                BarsRequiredToTrade = 60;
                IsInstantiatedOnEachOptimizationIteration = true;

                MlServiceUrl = "http://localhost:8767";
                DataSeriesLabel = "Order Flow Delta";
                MinConfidence = 0.65;
                ConfidenceDecayThreshold = 0.45;
                WindowSize = 30;
                LabelLookaheadBars = 20;
                MlHttpTimeoutMs = 1500;
                EnableSampleLogging = true;
                DebugMode = false;

                DonchianPeriod = 20;
                SuperTrendAtrPeriod = 10;
                SuperTrendMultiplier = 3.0;
                LinRegSlopePeriod = 14;
                LinRegSlopeAtrMultiple = 0.15;
                AdxPeriod = 14;
                AdxThreshold = 22;
                ChoppinessPeriod = 14;
                ChoppinessThreshold = 55;
                RelVolumePeriod = 20;
                RelVolumeThreshold = 1.2;
                AtrPeriod = 14;

                StopAtrMultiplier = 1.5;
                TargetAtrMultiplier = 2.0;
                Contracts = 1;

                ClTrendDeltaValue = 200;
                E6TrendDeltaValue = 150;
                NqTrendDeltaValue = 400;
                EsTrendDeltaValue = 500;
                FdaxTrendDeltaValue = 100;
                NkdTrendDeltaValue = 50;
                BtcTrendDeltaValue = 20;
                GcTrendDeltaValue = 150;
                HgTrendDeltaValue = 50;

                E6Symbol = "6E";
                NqSymbol = "NQ";
                EsSymbol = "ES";
                FdaxSymbol = "FDAX";
                NkdSymbol = "NKD";
                BtcSymbol = "BTC";
                GcSymbol = "GC";
                HgSymbol = "HG";
                AutoResolveContractMonth = true;
                RolloverDaysBeforeExpiry = 8;

                FlattenHour = 15;
                FlattenMinute = 42;
                EarlyFlattenHour = 14;
                EarlyFlattenMinute = 42;

                EnableCl = true;
                EnableE6 = true;
                EnableNq = true;
                EnableEs = true;
                EnableFdax = true;
                EnableNkd = true;
                EnableBtc = true;
                EnableGc = true;
                EnableHg = true;
            }
            else if (State == State.Configure)
            {
                AddDataSeries(ResolveSymbol(E6), DeltaPeriodFor(E6TrendDeltaValue));     // BarsInProgress 1
                AddDataSeries(ResolveSymbol(NQ), DeltaPeriodFor(NqTrendDeltaValue));      // BarsInProgress 2
                AddDataSeries(ResolveSymbol(ES), DeltaPeriodFor(EsTrendDeltaValue));      // BarsInProgress 3
                AddDataSeries(ResolveSymbol(FDAX), DeltaPeriodFor(FdaxTrendDeltaValue));  // BarsInProgress 4
                AddDataSeries(ResolveSymbol(NKD), DeltaPeriodFor(NkdTrendDeltaValue));    // BarsInProgress 5
                AddDataSeries(ResolveSymbol(BTC), DeltaPeriodFor(BtcTrendDeltaValue));    // BarsInProgress 6
                AddDataSeries(ResolveSymbol(GC), DeltaPeriodFor(GcTrendDeltaValue));      // BarsInProgress 7
                AddDataSeries(ResolveSymbol(HG), DeltaPeriodFor(HgTrendDeltaValue));      // BarsInProgress 8
            }
            else if (State == State.DataLoaded)
            {
                ValidateStartupConfiguration();
                if (startupValidationFailed)
                    return;

                for (int i = 0; i < InstrumentCount; i++)
                    inst[i] = CreateInstrumentState(i);

                inst[FDAX].UsesEarlyFlatten = true;

                for (int i = 0; i < InstrumentCount; i++)
                {
                    string resolved = BarsArray[i] != null && BarsArray[i].Instrument != null ? BarsArray[i].Instrument.FullName : "?";
                    Print("TrendTcn instrument " + ShortNames[i] + " resolved to " + resolved);
                }
            }
        }

        private static BarsPeriod DeltaPeriodFor(int deltaValue)
        {
            return new BarsPeriod { BarsPeriodType = BarsPeriodType.Delta, Value = deltaValue, Value2 = deltaValue };
        }

        private InstrumentTrendState CreateInstrumentState(int bip)
        {
            return new InstrumentTrendState
            {
                ShortName = ShortNames[bip],
                Bip = bip,
                Adx = ADX(BarsArray[bip], AdxPeriod),
                Atr = ATR(BarsArray[bip], AtrPeriod),
                SuperTrendAtr = ATR(BarsArray[bip], SuperTrendAtrPeriod),
                Chop = ChoppinessIndex(BarsArray[bip], ChoppinessPeriod),
                Donchian = DonchianChannel(BarsArray[bip], DonchianPeriod),
                LinRegSlope = LinRegSlope(BarsArray[bip], LinRegSlopePeriod),
                VolumeSma = SMA(Volumes[bip], RelVolumePeriod),
            };
        }

        // ------------------------------------------------ Symbol resolution

        private string ResolveSymbol(int bip)
        {
            switch (bip)
            {
                case E6: return ResolveDataSeriesSymbol(E6Symbol, "6E", ContractRolloverCycle.Quarterly);
                case NQ: return ResolveDataSeriesSymbol(NqSymbol, "NQ", ContractRolloverCycle.Quarterly);
                case ES: return ResolveDataSeriesSymbol(EsSymbol, "ES", ContractRolloverCycle.Quarterly);
                case FDAX: return ResolveDataSeriesSymbol(FdaxSymbol, "FDAX", ContractRolloverCycle.Quarterly);
                case NKD: return ResolveDataSeriesSymbol(NkdSymbol, "NKD", ContractRolloverCycle.Quarterly);
                case BTC: return ResolveDataSeriesSymbol(BtcSymbol, "BTC", ContractRolloverCycle.MonthlyLastFriday);
                case GC: return ResolveDataSeriesSymbol(GcSymbol, "GC", ContractRolloverCycle.EvenMonths);
                case HG: return ResolveDataSeriesSymbol(HgSymbol, "HG", ContractRolloverCycle.MonthlyGeneric);
                default: return string.Empty;
            }
        }

        // Reused verbatim (renamed for clarity) from fulltwenties.cs's proven
        // rollover system rather than a generic approximation invented here.
        private string ResolveDataSeriesSymbol(string configuredSymbol, string defaultRoot, ContractRolloverCycle cycle)
        {
            string symbol = string.IsNullOrWhiteSpace(configuredSymbol) ? defaultRoot : configuredSymbol.Trim();
            if (!AutoResolveContractMonth)
                return symbol;

            if (symbol.IndexOf(' ') >= 0)
                return symbol;

            string root = symbol.ToUpperInvariant();
            string suffix = ResolveContractSuffix(DateTime.Now.Date, cycle);
            return root + " " + suffix;
        }

        private string ResolveContractSuffix(DateTime today, ContractRolloverCycle cycle)
        {
            int year = today.Year;
            int month = today.Month;

            for (int i = 0; i < 36; i++)
            {
                DateTime candidate = new DateTime(year, month, 1).AddMonths(i);
                if (!IsContractMonth(candidate.Month, cycle))
                    continue;

                DateTime rolloverDate = ApproximateExpiryDate(candidate.Year, candidate.Month, cycle).AddDays(-RolloverDaysBeforeExpiry);
                if (today < rolloverDate)
                    return candidate.ToString("MM-yy", CultureInfo.InvariantCulture);
            }

            return today.AddMonths(1).ToString("MM-yy", CultureInfo.InvariantCulture);
        }

        private static bool IsContractMonth(int month, ContractRolloverCycle cycle)
        {
            if (cycle == ContractRolloverCycle.MonthlyCrude || cycle == ContractRolloverCycle.MonthlyLastFriday || cycle == ContractRolloverCycle.MonthlyGeneric)
                return true;
            if (cycle == ContractRolloverCycle.EvenMonths)
                return month == 2 || month == 4 || month == 6 || month == 8 || month == 10 || month == 12;

            return month == 3 || month == 6 || month == 9 || month == 12;
        }

        private static DateTime ApproximateExpiryDate(int year, int month, ContractRolloverCycle cycle)
        {
            if (cycle == ContractRolloverCycle.MonthlyCrude)
            {
                DateTime priorMonth25th = new DateTime(year, month, 1).AddMonths(-1);
                priorMonth25th = new DateTime(priorMonth25th.Year, priorMonth25th.Month, 25);
                return AddBusinessDays(priorMonth25th, -3);
            }

            if (cycle == ContractRolloverCycle.EvenMonths)
                return ThirdLastBusinessDay(year, month);

            if (cycle == ContractRolloverCycle.MonthlyLastFriday)
                return LastFriday(year, month);

            if (cycle == ContractRolloverCycle.MonthlyGeneric)
                return ThirdLastBusinessDay(year, month); // HG (copper): last trade is ~3rd-to-last business day of the contract month per COMEX calendar; can be off by a day around a mid-week US market holiday (e.g. Thanksgiving), since IsBusinessDay only excludes weekends

            return ThirdFriday(year, month);
        }

        private static DateTime ThirdFriday(int year, int month)
        {
            DateTime day = new DateTime(year, month, 1);
            while (day.DayOfWeek != DayOfWeek.Friday)
                day = day.AddDays(1);

            return day.AddDays(14);
        }

        private static DateTime LastFriday(int year, int month)
        {
            DateTime day = new DateTime(year, month, DateTime.DaysInMonth(year, month));
            while (day.DayOfWeek != DayOfWeek.Friday)
                day = day.AddDays(-1);

            return day;
        }

        private static DateTime ThirdLastBusinessDay(int year, int month)
        {
            DateTime day = new DateTime(year, month, DateTime.DaysInMonth(year, month));
            int businessDaysSeen = 0;
            while (true)
            {
                if (IsBusinessDay(day))
                {
                    businessDaysSeen++;
                    if (businessDaysSeen == 3)
                        return day;
                }

                day = day.AddDays(-1);
            }
        }

        private static DateTime AddBusinessDays(DateTime start, int businessDays)
        {
            int step = businessDays < 0 ? -1 : 1;
            int remaining = Math.Abs(businessDays);
            DateTime day = start;

            while (remaining > 0)
            {
                day = day.AddDays(step);
                if (IsBusinessDay(day))
                    remaining--;
            }

            return day;
        }

        private static bool IsBusinessDay(DateTime day)
        {
            return day.DayOfWeek != DayOfWeek.Saturday && day.DayOfWeek != DayOfWeek.Sunday;
        }

        private void ValidateStartupConfiguration()
        {
            if (BarsArray == null || BarsArray.Length < InstrumentCount || Instruments == null || Instruments.Length < InstrumentCount)
            {
                Print("TrendTcn: expected " + InstrumentCount + " data series but NinjaTrader did not load them. Strategy disabled.");
                startupValidationFailed = true;
                return;
            }

            bool primaryIsCl = Instruments[CL].MasterInstrument.Name.Equals("CL", StringComparison.OrdinalIgnoreCase);
            if (!primaryIsCl)
            {
                Print("TrendTcn: apply this strategy to a CL chart (Order Flow Delta, " + ClTrendDeltaValue + "/" + ClTrendDeltaValue + "). Loaded primary was "
                    + Instruments[CL].FullName + ". Strategy disabled.");
                startupValidationFailed = true;
                return;
            }

            BarsPeriod clPeriod = BarsArray[CL] != null ? BarsArray[CL].BarsPeriod : null;
            if (clPeriod != null && (clPeriod.Value != ClTrendDeltaValue || clPeriod.Value2 != ClTrendDeltaValue))
            {
                Print("TrendTcn: warning - CL chart bar type is " + clPeriod.Value + "/" + clPeriod.Value2
                    + " but ClTrendDeltaValue parameter is " + ClTrendDeltaValue + "/" + ClTrendDeltaValue
                    + ". Change the chart's Order Flow Delta bar type to match, or update the parameter.");
            }

            for (int i = 1; i < InstrumentCount; i++)
                WarnIfUnexpectedInstrument(i, ShortNames[i]);
        }

        private void WarnIfUnexpectedInstrument(int bip, string expectedRoot)
        {
            if (!Instruments[bip].MasterInstrument.Name.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase))
            {
                Print("TrendTcn: warning - BarsInProgress " + bip + " expected " + expectedRoot
                    + " but loaded " + Instruments[bip].FullName + ". Check contract mapping before trading.");
            }
        }

        private int FindBarsInProgress(Instrument instrument)
        {
            if (instrument == null || BarsArray == null)
                return -1;

            for (int i = 0; i < BarsArray.Length && i < InstrumentCount; i++)
            {
                if (BarsArray[i] != null && BarsArray[i].Instrument != null
                    && string.Equals(BarsArray[i].Instrument.FullName, instrument.FullName, StringComparison.OrdinalIgnoreCase))
                    return i;
            }

            return -1;
        }

        private double TickSizeFor(InstrumentTrendState s)
        {
            if (s != null
                && BarsArray != null
                && s.Bip >= 0
                && s.Bip < BarsArray.Length
                && BarsArray[s.Bip] != null
                && BarsArray[s.Bip].Instrument != null
                && BarsArray[s.Bip].Instrument.MasterInstrument != null
                && BarsArray[s.Bip].Instrument.MasterInstrument.TickSize > 0)
                return BarsArray[s.Bip].Instrument.MasterInstrument.TickSize;

            return TickSize;
        }

        // --------------------------------------------------------- OnBarUpdate

        private readonly HashSet<int> observedBarsInProgress = new HashSet<int>();

        protected override void OnBarUpdate()
        {
            if (startupValidationFailed)
                return;

            int idx = BarsInProgress;
            if (idx < 0 || idx >= InstrumentCount)
                return;

            if (!observedBarsInProgress.Contains(idx))
            {
                observedBarsInProgress.Add(idx);
                string instName = (BarsArray != null && idx < BarsArray.Length && BarsArray[idx] != null && BarsArray[idx].Instrument != null)
                    ? BarsArray[idx].Instrument.FullName
                    : "unknown";
                Print("TrendTcn FIRST BAR RECEIVED: BarsInProgress=" + idx + " instrument=" + instName + " CurrentBars[bip]=" + CurrentBars[idx]);
            }

            InstrumentTrendState s = inst[idx];
            if (s == null || CurrentBars[s.Bip] < BarsRequiredToTrade)
                return;

            UpdateSuperTrend(s);

            double[] featureRow = BuildFeatureRow(s);
            s.FeatureWindow.Add(featureRow);
            if (s.FeatureWindow.Count > WindowSize)
                s.FeatureWindow.RemoveAt(0);

            // Historical bars are used only to warm the feature window. Do not call the ML
            // service, label/log samples, flatten, or submit/manage orders until live data.
            if (State != State.Realtime)
                return;

            // Runs for every instrument on every bar close, not just s's own -- a manual/ATM
            // flatten never routes through OnPositionUpdate for this instance (see
            // FindAccountPositionForInstrument's usage below), so without a cross-instrument
            // sweep a stale dashboard row could sit unreconciled indefinitely if that specific
            // instrument's own (volume-based) bar type is slow to close. Piggybacking on
            // whichever of the 9 series just ticked means the sweep runs far more often than
            // any single slow instrument's own bars would allow.
            ReconcileStaleOpenTradeStatuses();

            if (Positions[s.Bip].MarketPosition != MarketPosition.Flat)
                WriteOpenTradeStatus(s);
            else
                ClearOpenTradeStatus(s);

            EvaluatePendingCandidates(s);

            if (HandleFlattenTime(s))
                return;

            bool sessionTimesReady = UpdateSessionEndTimes(s);
            bool enabled = IsInstrumentEnabledLive(s.Bip);

            if (!enabled && Positions[s.Bip].MarketPosition != MarketPosition.Flat)
            {
                ForceFlattenInstrument(s);
                return;
            }

            if (Positions[s.Bip].MarketPosition != MarketPosition.Flat)
            {
                ManageOpenPosition(s);
                return;
            }

            if (!enabled)
                return;

            if (sessionTimesReady && Times[s.Bip][0] >= s.NoNewTradesTime)
                return;

            if (s.FeatureWindow.Count < WindowSize)
                return;

            bool longGate = CheckLongGate(s);
            bool shortGate = CheckShortGate(s);
            if (!longGate && !shortGate)
            {
                LogGateNearMiss(s);
                return;
            }

            if (s.LastPredictionBar == CurrentBars[s.Bip])
                return;
            s.LastPredictionBar = CurrentBars[s.Bip];

            string direction = longGate ? "long" : "short";
            RegisterPendingCandidate(s, direction);
            RequestTrendPrediction(s, direction);
        }

        // ---------------------------------------------------------- SuperTrend

        private void UpdateSuperTrend(InstrumentTrendState s)
        {
            if (CurrentBars[s.Bip] < SuperTrendAtrPeriod)
                return;

            double atrValue = s.SuperTrendAtr[0];
            double mid = (Highs[s.Bip][0] + Lows[s.Bip][0]) / 2.0;
            double basicUpper = mid + SuperTrendMultiplier * atrValue;
            double basicLower = mid - SuperTrendMultiplier * atrValue;

            if (double.IsNaN(s.StFinalUpper))
            {
                s.StFinalUpper = basicUpper;
                s.StFinalLower = basicLower;
                s.StDirection = Closes[s.Bip][0] >= mid ? 1 : -1;
                return;
            }

            double prevClose = Closes[s.Bip][1];
            double finalUpper = (basicUpper < s.StFinalUpper || prevClose > s.StFinalUpper) ? basicUpper : s.StFinalUpper;
            double finalLower = (basicLower > s.StFinalLower || prevClose < s.StFinalLower) ? basicLower : s.StFinalLower;

            if (s.StDirection == 1 && Closes[s.Bip][0] < finalLower)
                s.StDirection = -1;
            else if (s.StDirection == -1 && Closes[s.Bip][0] > finalUpper)
                s.StDirection = 1;

            s.StFinalUpper = finalUpper;
            s.StFinalLower = finalLower;
        }

        private double SuperTrendValue(InstrumentTrendState s)
        {
            return s.StDirection == 1 ? s.StFinalLower : s.StFinalUpper;
        }

        // ------------------------------------------------------------ Features

        // Order matches FEATURE_NAMES in MLService_Trend/trend_model.py -- do
        // not reorder without updating both sides.
        private double[] BuildFeatureRow(InstrumentTrendState s)
        {
            double atrValue = Math.Max(s.Atr[0], TickSizeFor(s));
            double donchianHigh = s.Donchian.Upper[0];
            double donchianLow = s.Donchian.Lower[0];
            double relVolume = s.VolumeSma[0] > 0 ? Volumes[s.Bip][0] / s.VolumeSma[0] : 1.0;

            return new double[]
            {
                (Closes[s.Bip][0] - donchianHigh) / atrValue,
                (Closes[s.Bip][0] - donchianLow) / atrValue,
                s.StDirection,
                (Closes[s.Bip][0] - SuperTrendValue(s)) / atrValue,
                s.LinRegSlope[0] / atrValue,
                s.Adx[0],
                s.Chop[0],
                relVolume,
                GetOrderFlowDelta(s) / Math.Max(s.VolumeSma[0], 1.0),
                atrValue / Math.Max(Closes[s.Bip][0], TickSizeFor(s)),
            };
        }

        private double GetOrderFlowDelta(InstrumentTrendState s)
        {
            // GetCurrentAskVolume/GetCurrentBidVolume take a BarsInProgress INDEX,
            // not a barsAgo offset -- a literal 0 here would read CL's book for
            // every instrument. Returns 0 on historical bars (no live book).
            try
            {
                return GetCurrentAskVolume(s.Bip) - GetCurrentBidVolume(s.Bip);
            }
            catch
            {
                return 0.0;
            }
        }

        // -------------------------------------------------------- Entry gate

        private bool CheckLongGate(InstrumentTrendState s)
        {
            double atrValue = Math.Max(s.Atr[0], TickSizeFor(s));
            bool breakout = Closes[s.Bip][0] > s.Donchian.Upper[1];
            bool trendUp = s.StDirection == 1;
            bool slopeUp = s.LinRegSlope[0] / atrValue > LinRegSlopeAtrMultiple;
            bool strongEnough = s.Adx[0] >= AdxThreshold;
            bool trending = s.Chop[0] <= ChoppinessThreshold;
            bool volumeOk = s.VolumeSma[0] > 0 && Volumes[s.Bip][0] / s.VolumeSma[0] >= RelVolumeThreshold;
            return breakout && trendUp && slopeUp && strongEnough && trending && volumeOk;
        }

        private bool CheckShortGate(InstrumentTrendState s)
        {
            double atrValue = Math.Max(s.Atr[0], TickSizeFor(s));
            bool breakdown = Closes[s.Bip][0] < s.Donchian.Lower[1];
            bool trendDown = s.StDirection == -1;
            bool slopeDown = s.LinRegSlope[0] / atrValue < -LinRegSlopeAtrMultiple;
            bool strongEnough = s.Adx[0] >= AdxThreshold;
            bool trending = s.Chop[0] <= ChoppinessThreshold;
            bool volumeOk = s.VolumeSma[0] > 0 && Volumes[s.Bip][0] / s.VolumeSma[0] >= RelVolumeThreshold;
            return breakdown && trendDown && slopeDown && strongEnough && trending && volumeOk;
        }

        private void LogGateNearMiss(InstrumentTrendState s)
        {
            if (s == null || s.LastNearMissBar == CurrentBars[s.Bip])
                return;

            double atrValue = Math.Max(s.Atr[0], TickSizeFor(s));
            bool longBreakout = Closes[s.Bip][0] > s.Donchian.Upper[1];
            bool longTrend = s.StDirection == 1;
            bool longSlope = s.LinRegSlope[0] / atrValue > LinRegSlopeAtrMultiple;
            bool strongEnough = s.Adx[0] >= AdxThreshold;
            bool trending = s.Chop[0] <= ChoppinessThreshold;
            bool volumeOk = s.VolumeSma[0] > 0 && Volumes[s.Bip][0] / s.VolumeSma[0] >= RelVolumeThreshold;

            int longMissing = 0;
            string longMissingName = string.Empty;
            CountMissing(!longBreakout, "breakout", ref longMissing, ref longMissingName);
            CountMissing(!longTrend, "trend", ref longMissing, ref longMissingName);
            CountMissing(!longSlope, "slope", ref longMissing, ref longMissingName);
            CountMissing(!strongEnough, "ADX", ref longMissing, ref longMissingName);
            CountMissing(!trending, "chop", ref longMissing, ref longMissingName);
            CountMissing(!volumeOk, "volume", ref longMissing, ref longMissingName);

            bool shortBreakdown = Closes[s.Bip][0] < s.Donchian.Lower[1];
            bool shortTrend = s.StDirection == -1;
            bool shortSlope = s.LinRegSlope[0] / atrValue < -LinRegSlopeAtrMultiple;

            int shortMissing = 0;
            string shortMissingName = string.Empty;
            CountMissing(!shortBreakdown, "breakdown", ref shortMissing, ref shortMissingName);
            CountMissing(!shortTrend, "trend", ref shortMissing, ref shortMissingName);
            CountMissing(!shortSlope, "slope", ref shortMissing, ref shortMissingName);
            CountMissing(!strongEnough, "ADX", ref shortMissing, ref shortMissingName);
            CountMissing(!trending, "chop", ref shortMissing, ref shortMissingName);
            CountMissing(!volumeOk, "volume", ref shortMissing, ref shortMissingName);

            if (longMissing >= 1 && longMissing <= 3 && longMissing <= shortMissing)
            {
                s.LastNearMissBar = CurrentBars[s.Bip];
                if (DebugMode)
                Print(Times[s.Bip][0] + " | NEAR MISS [Strategy=TrendTcn | Ticker=" + Instruments[s.Bip].FullName + " | Series " + s.Bip + "]: LONG missed by " + longMissingName + " (" + (6 - longMissing) + "/6)");
                RegisterPendingCandidate(s, "long", isNearMiss: true, matchCount: 6 - longMissing);
            }
            else if (shortMissing >= 1 && shortMissing <= 3)
            {
                s.LastNearMissBar = CurrentBars[s.Bip];
                if (DebugMode)
                Print(Times[s.Bip][0] + " | NEAR MISS [Strategy=TrendTcn | Ticker=" + Instruments[s.Bip].FullName + " | Series " + s.Bip + "]: SHORT missed by " + shortMissingName + " (" + (6 - shortMissing) + "/6)");
                RegisterPendingCandidate(s, "short", isNearMiss: true, matchCount: 6 - shortMissing);
            }
        }

        private void CountMissing(bool conditionMissing, string name, ref int missing, ref string missingName)
        {
            if (!conditionMissing)
                return;

            missing++;
            missingName = name;
        }

        // ------------------------------------------------------- ML prediction

        private void RequestTrendPrediction(InstrumentTrendState s, string direction)
        {
            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            string symbol = instrument != null && instrument.MasterInstrument != null ? instrument.MasterInstrument.Name : string.Empty;
            string windowJson = BuildWindowJson(s);
            string requestJson = "{"
                + "\"symbol\":\"" + JsonEscape(symbol) + "\","
                + "\"bars_period\":\"" + JsonEscape(DataSeriesLabel) + "\","
                + "\"timestamp\":\"" + JsonEscape(Times[s.Bip][0].ToString("o", CultureInfo.InvariantCulture)) + "\","
                + "\"min_confidence\":" + FormatJsonDouble(MinConfidence) + ","
                + "\"window\":" + windowJson + ","
                + "\"metadata\":{\"gate_direction\":\"" + JsonEscape(direction) + "\"}"
                + "}";

            string predictUrl = TrimTrailingSlash(MlServiceUrl) + "/predict-trend";
            int bip = s.Bip;
            string shortName = s.ShortName;
            int signalBar = CurrentBars[bip];
            double atrAtSignal = s.Atr[0];
            double priceAtSignal = Closes[bip][0];

            System.Threading.Tasks.Task.Run(() =>
            {
                try
                {
                    string response = PostJson(predictUrl, requestJson);
                    string action = ExtractJsonString(response, "action");
                    double confidence = ExtractJsonDouble(response, "confidence");
                    bool modelReady = ExtractJsonBool(response, "model_ready");
                    string status = ExtractJsonString(response, "status");

                    // Quality gate: status mirrors trend_model.py's classify_direction_status(),
                    // the same function that drives the port-8767 dashboard's per-direction status
                    // pill -- only "good_to_use" is allowed to submit a real order, so a direction
                    // the dashboard shows blocked (warming up, low recall, do not use, etc.) can
                    // never trade just because raw action/confidence happened to clear the bar.
                    if (action == direction && confidence >= MinConfidence && status == "good_to_use")
                    {
                        TriggerCustomEvent(o => SubmitEntryFromSignal(bip, signalBar, direction, priceAtSignal, atrAtSignal, confidence), null);
                    }
                    else
                    {
                        if (DebugMode)
                        TriggerCustomEvent(o =>
                            Print(string.Format(CultureInfo.InvariantCulture,
                                "TrendTcn [{0}]: gate={1} ml_action={2} confidence={3:0.000} ready={4} status={5} -- no entry",
                                shortName, direction, action, confidence, modelReady, status)), null);
                    }
                }
                catch (Exception error)
                {
                    if (ShouldPrintMlHttpError()) TriggerCustomEvent(o => Print("TrendTcn predict-trend failed [" + shortName + "]: " + error.Message), null);
                }
            });
        }

        private void SubmitEntryFromSignal(int bip, int signalBar, string direction, double priceAtSignal, double atrAtSignal, double confidence)
        {
            InstrumentTrendState s = inst[bip];
            if (s == null || signalBar != CurrentBars[bip] || Positions[bip].MarketPosition != MarketPosition.Flat)
                return;

            double tickSize = TickSizeFor(s);
            double stopDistance = Math.Max(atrAtSignal * StopAtrMultiplier, tickSize);
            double targetDistance = Math.Max(atrAtSignal * TargetAtrMultiplier, tickSize);
            int stopTicks = Math.Max(1, (int)Math.Round(stopDistance / tickSize, MidpointRounding.AwayFromZero));
            int targetTicks = Math.Max(1, (int)Math.Round(targetDistance / tickSize, MidpointRounding.AwayFromZero));

            if (direction == "long")
            {
                string signal = "TrendLong-" + s.ShortName;
                SetStopLoss(signal, CalculationMode.Ticks, stopTicks, false);
                SetProfitTarget(signal, CalculationMode.Ticks, targetTicks);
                EnterLong(bip, Contracts, signal);
                s.ActiveDirection = "long";
                s.ActiveEntrySignal = signal;
            }
            else
            {
                string signal = "TrendShort-" + s.ShortName;
                SetStopLoss(signal, CalculationMode.Ticks, stopTicks, false);
                SetProfitTarget(signal, CalculationMode.Ticks, targetTicks);
                EnterShort(bip, Contracts, signal);
                s.ActiveDirection = "short";
                s.ActiveEntrySignal = signal;
            }

            // Seed so the first ConfidenceDecay re-check comes 5 bars after entry
            // rather than depending on a leftover value from a prior trade.
            s.LastDecayCheckBar = CurrentBars[bip];

            Print(string.Format(CultureInfo.InvariantCulture,
                "TrendTcn [{0}]: ENTER {1} confidence={2:0.000}", s.ShortName, direction.ToUpperInvariant(), confidence));
        }

        // -------------------------------------------------------- Open position

        private void ManageOpenPosition(InstrumentTrendState s)
        {
            if (string.IsNullOrEmpty(s.ActiveDirection))
                return;

            // Trend invalidation: flatten if SuperTrend flips against the position.
            if (s.ActiveDirection == "long" && s.StDirection == -1)
            {
                ExitLong(s.Bip, Math.Max(1, Positions[s.Bip].Quantity), "TrendInvalidated", s.ActiveEntrySignal);
                return;
            }
            if (s.ActiveDirection == "short" && s.StDirection == 1)
            {
                ExitShort(s.Bip, Math.Max(1, Positions[s.Bip].Quantity), "TrendInvalidated", s.ActiveEntrySignal);
                return;
            }

            // Confidence decay: periodically re-check with the ML service and
            // exit early if it no longer likes this direction. The int.MinValue
            // guard must short-circuit BEFORE the subtraction -- CurrentBars minus
            // int.MinValue overflows negative, which silently disabled this exit.
            if (s.LastDecayCheckBar != int.MinValue && CurrentBars[s.Bip] - s.LastDecayCheckBar < 5)
                return;
            s.LastDecayCheckBar = CurrentBars[s.Bip];
            RequestConfidenceDecayCheck(s);
        }

        private void RequestConfidenceDecayCheck(InstrumentTrendState s)
        {
            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            string symbol = instrument != null && instrument.MasterInstrument != null ? instrument.MasterInstrument.Name : string.Empty;
            string windowJson = BuildWindowJson(s);
            string requestJson = "{"
                + "\"symbol\":\"" + JsonEscape(symbol) + "\","
                + "\"bars_period\":\"" + JsonEscape(DataSeriesLabel) + "\","
                + "\"min_confidence\":0.0,"
                + "\"window\":" + windowJson + ","
                + "\"metadata\":{\"purpose\":\"decay_check\"}"
                + "}";

            string predictUrl = TrimTrailingSlash(MlServiceUrl) + "/predict-trend";
            int bip = s.Bip;
            int signalBar = CurrentBars[bip];
            string direction = s.ActiveDirection;
            string entrySignal = s.ActiveEntrySignal;

            System.Threading.Tasks.Task.Run(() =>
            {
                try
                {
                    string response = PostJson(predictUrl, requestJson);
                    bool modelReady = ExtractJsonBool(response, "model_ready");
                    if (!modelReady)
                        return;

                    double probForDirection = ExtractJsonDouble(response, direction);
                    if (probForDirection > 0 && probForDirection < ConfidenceDecayThreshold)
                    {
                        TriggerCustomEvent(o =>
                        {
                            InstrumentTrendState s2 = inst[bip];
                            if (s2 == null || CurrentBars[bip] != signalBar || s2.ActiveDirection != direction)
                                return;
                            int qty = Math.Max(1, Positions[bip].Quantity);
                            if (direction == "long")
                                ExitLong(bip, qty, "ConfidenceDecay", entrySignal);
                            else
                                ExitShort(bip, qty, "ConfidenceDecay", entrySignal);
                        }, null);
                    }
                }
                catch { }
            });
        }

        // ---------------------------------------------------- Session flatten

        private bool HandleFlattenTime(InstrumentTrendState s)
        {
            UpdateSessionEndTimes(s);

            if (s.FlattenTime == DateTime.MinValue || s.FlattenTime == DateTime.MaxValue)
                return false;

            if (Times[s.Bip][0] >= s.FlattenTime && Positions[s.Bip].MarketPosition != MarketPosition.Flat)
            {
                int qty = Positions[s.Bip].Quantity;
                if (Positions[s.Bip].MarketPosition == MarketPosition.Long)
                    ExitLong(s.Bip, qty, "TrendFlatten", s.ActiveEntrySignal);
                else if (Positions[s.Bip].MarketPosition == MarketPosition.Short)
                    ExitShort(s.Bip, qty, "TrendFlatten", s.ActiveEntrySignal);
                return true;
            }
            return false;
        }


        private bool UpdateSessionEndTimes(InstrumentTrendState s)
        {
            if (s == null || BarsArray == null || s.Bip < 0 || s.Bip >= BarsArray.Length || BarsArray[s.Bip] == null || CurrentBars[s.Bip] < 0)
                return false;

            if (s.SessionIterator == null)
                s.SessionIterator = new SessionIterator(BarsArray[s.Bip]);

            DateTime barTime = Times[s.Bip][0];
            s.SessionIterator.GetNextSession(barTime, true);
            DateTime sessionEnd = s.SessionIterator.ActualSessionEnd;

            if (sessionEnd == DateTime.MinValue || sessionEnd == DateTime.MaxValue)
                return false;

            // The configured wall-clock flatten (FDAX uses the Early pair -- its
            // intraday margin window ends sooner) caps the session-derived time;
            // whichever comes first wins, anchored to the session-end date so
            // overnight sessions flatten on the correct calendar day.
            int flattenHour = s.UsesEarlyFlatten ? EarlyFlattenHour : FlattenHour;
            int flattenMinute = s.UsesEarlyFlatten ? EarlyFlattenMinute : FlattenMinute;
            DateTime configuredFlatten = sessionEnd.Date.AddHours(flattenHour).AddMinutes(flattenMinute);
            DateTime flattenTime = sessionEnd.AddMinutes(-18);
            if (configuredFlatten < flattenTime)
                flattenTime = configuredFlatten;

            s.FlattenTime = flattenTime;
            s.NoNewTradesTime = flattenTime.AddMinutes(-12);
            return true;
        }
        private bool IsInstrumentEnabledLive(int bip)
        {
            switch (bip)
            {
                case CL: return EnableCl;
                case E6: return EnableE6;
                case NQ: return EnableNq;
                case ES: return EnableEs;
                case FDAX: return EnableFdax;
                case NKD: return EnableNkd;
                case BTC: return EnableBtc;
                case GC: return EnableGc;
                case HG: return EnableHg;
                default: return false;
            }
        }

        private void ForceFlattenInstrument(InstrumentTrendState s)
        {
            int qty = Positions[s.Bip].Quantity;
            if (Positions[s.Bip].MarketPosition == MarketPosition.Long)
                ExitLong(s.Bip, qty, "InstrumentDisabled", s.ActiveEntrySignal);
            else if (Positions[s.Bip].MarketPosition == MarketPosition.Short)
                ExitShort(s.Bip, qty, "InstrumentDisabled", s.ActiveEntrySignal);
        }

        // ------------------------------------------------------ Order routing

        protected override void OnExecutionUpdate(Execution execution, string executionId, double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (execution == null || execution.Order == null)
                return;

            int bip = FindBarsInProgress(execution.Instrument);
            if (bip < 0 || bip >= InstrumentCount)
                return;

            InstrumentTrendState s = inst[bip];
            if (s == null)
                return;

            bool isEntryFill = execution.Order.Name == s.ActiveEntrySignal && !string.IsNullOrEmpty(s.ActiveDirection);
            int fillQuantity = Math.Max(0, quantity);
            if (isEntryFill)
            {
                if (fillQuantity > 0)
                {
                    if (s.ActiveQuantity <= 0 || s.ActiveEntryPrice <= 0)
                    {
                        s.ActiveEntryPrice = price;
                        s.ActiveEntryFillTime = time;
                        s.ActiveEntryBar = CurrentBars[s.Bip];
                    }
                    else
                    {
                        s.ActiveEntryPrice = ((s.ActiveEntryPrice * s.ActiveQuantity) + (price * fillQuantity)) / (s.ActiveQuantity + fillQuantity);
                    }

                    s.ActiveQuantity += fillQuantity;
                }
                return;
            }

            if (!string.IsNullOrEmpty(s.ActiveDirection) && s.ActiveEntryPrice > 0 && fillQuantity > 0)
            {
                if (s.ActiveExitQuantity <= 0 || s.ActiveExitPrice <= 0)
                    s.ActiveExitPrice = price;
                else
                    s.ActiveExitPrice = ((s.ActiveExitPrice * s.ActiveExitQuantity) + (price * fillQuantity)) / (s.ActiveExitQuantity + fillQuantity);

                s.ActiveExitQuantity += fillQuantity;
                s.ActiveExitSignal = execution.Order.Name ?? string.Empty;
                s.ActiveExitTime = time;
            }
        }

        protected override void OnPositionUpdate(Position position, double averagePrice, int quantity, MarketPosition marketPosition)
        {
            if (position == null || position.Instrument == null)
                return;

            int bip = FindBarsInProgress(position.Instrument);
            if (bip < 0 || bip >= InstrumentCount)
                return;

            InstrumentTrendState s = inst[bip];
            if (s == null || marketPosition != MarketPosition.Flat || s.ActiveEntryPrice <= 0)
                return;

            double exitPrice = s.ActiveExitPrice > 0 ? s.ActiveExitPrice : averagePrice;
            int tradeQuantity = s.ActiveQuantity > 0 ? s.ActiveQuantity : Math.Max(1, s.ActiveExitQuantity);
            string exitSignal = string.IsNullOrEmpty(s.ActiveExitSignal) ? "PositionFlat" : s.ActiveExitSignal;
            DateTime exitTime = s.ActiveExitTime == DateTime.MinValue ? DateTime.Now : s.ActiveExitTime;

            AppendDashboardTradeOutcome(s, exitPrice, tradeQuantity, exitSignal, exitTime);
            ResetInstrumentTradeState(s);
            ClearOpenTradeStatus(s);
        }

        private void ResetInstrumentTradeState(InstrumentTrendState s)
        {
            s.ActiveDirection = string.Empty;
            s.ActiveEntrySignal = string.Empty;
            s.ActiveEntryPrice = 0.0;
            s.ActiveQuantity = 0;
            s.ActiveEntryBar = int.MinValue;
            s.ActiveExitPrice = 0.0;
            s.ActiveExitQuantity = 0;
            s.ActiveExitSignal = string.Empty;
            s.ActiveExitTime = DateTime.MinValue;
        }

        private void AppendDashboardTradeOutcome(InstrumentTrendState s, double exitPrice, int quantity, string exitSignal, DateTime fillTime)
        {
            if (s.ActiveEntryPrice <= 0)
                return;

            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            if (instrument == null || instrument.MasterInstrument == null)
                return;

            try
            {
                bool isLong = s.ActiveDirection == "long";
                double points = isLong ? exitPrice - s.ActiveEntryPrice : s.ActiveEntryPrice - exitPrice;
                int tradeQuantity = Math.Max(1, quantity);
                double dollars = points * instrument.MasterInstrument.PointValue * tradeQuantity;
                string outcome = dollars > 0 ? "WIN" : dollars < 0 ? "LOSS" : "FLAT";
                string direction = isLong ? "LONG" : "SHORT";
                string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "TrendTcn_completed_trades.tsv");
                string header = "time\tticker\tdirection\tentryPrice\texitPrice\tquantity\tpnl\toutcome\texitSignal\tentrySignal\tinstrument\tentryTime\taccount";

                string line = string.Join("\t", new[]
                {
                    fillTime.ToString("o", CultureInfo.InvariantCulture),
                    instrument.MasterInstrument.Name,
                    direction,
                    s.ActiveEntryPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    exitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                    tradeQuantity.ToString(CultureInfo.InvariantCulture),
                    dollars.ToString("0.########", CultureInfo.InvariantCulture),
                    outcome,
                    exitSignal ?? string.Empty,
                    s.ActiveEntrySignal ?? string.Empty,
                    s.ShortName,
                    s.ActiveEntryFillTime > DateTime.MinValue ? s.ActiveEntryFillTime.ToString("o", CultureInfo.InvariantCulture) : string.Empty,
                    Account != null ? Account.Name : string.Empty
                });

                lock (dashboardTradeLogLock)
                {
                    if (!File.Exists(path) || new FileInfo(path).Length == 0)
                        File.WriteAllText(path, header + Environment.NewLine);

                    File.AppendAllText(path, line + Environment.NewLine);
                }
            }
            catch (Exception error)
            {
                Print("TrendTcn dashboard trade TSV log failed: " + error.Message);
            }
        }

        private string OpenTradeStatusFileName(InstrumentTrendState s)
        {
            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            string ticker = instrument != null && instrument.MasterInstrument != null ? instrument.MasterInstrument.Name : s.ShortName;
            string accountLabel = Account != null ? Account.Name : "Unknown";
            return OpenTradeStatusExporter.FileName("TrendTcn", ticker + "_" + accountLabel);
        }

        private void WriteOpenTradeStatus(InstrumentTrendState s)
        {
            if (s == null || Positions[s.Bip].MarketPosition == MarketPosition.Flat || s.ActiveEntryPrice <= 0)
            {
                ClearOpenTradeStatus(s);
                return;
            }

            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            if (instrument == null || instrument.MasterInstrument == null)
                return;

            // Positions[s.Bip] only reflects fills routed through this strategy instance.
            // A manual/ATM flatten done outside the strategy never updates it, so cross-check
            // the real account position to avoid leaving a zombie row in the dashboard feed.
            Position realPosition = FindAccountPositionForInstrument(instrument.FullName);
            if (realPosition == null || realPosition.MarketPosition == MarketPosition.Flat || realPosition.Quantity <= 0)
            {
                ClearOpenTradeStatus(s);
                return;
            }

            double currentPrice = Closes[s.Bip][0];
            string direction = Positions[s.Bip].MarketPosition == MarketPosition.Long ? "LONG" : "SHORT";
            int barsHeld = s.ActiveEntryBar == int.MinValue ? 0 : CurrentBars[s.Bip] - s.ActiveEntryBar;
            double unrealized = Positions[s.Bip].GetUnrealizedProfitLoss(PerformanceUnit.Currency, currentPrice);
            string row = OpenTradeStatusExporter.Row(Times[s.Bip][0], Name, instrument.MasterInstrument.Name, direction,
                Math.Max(1, Positions[s.Bip].Quantity), s.ActiveEntryPrice, currentPrice, unrealized, barsHeld, s.ActiveEntrySignal, Account != null ? Account.Name : string.Empty,
                barsPeriodType: "Delta", barsPeriodValue: DeltaValueForBip(s.Bip).ToString(CultureInfo.InvariantCulture));
            OpenTradeStatusExporter.Write(OpenTradeStatusFileName(s), row);
        }

        // Mirrors the BarsInProgress order set up by AddDataSeries in State.Configure
        // (0=CL primary, 1=6E, 2=NQ, 3=ES, 4=FDAX, 5=NKD, 6=BTC, 7=GC, 8=HG) so the
        // dashboard's open-trades feed can show the actual Order Flow Delta size per instrument.
        private int DeltaValueForBip(int bip)
        {
            switch (bip)
            {
                case 0: return ClTrendDeltaValue;
                case 1: return E6TrendDeltaValue;
                case 2: return NqTrendDeltaValue;
                case 3: return EsTrendDeltaValue;
                case 4: return FdaxTrendDeltaValue;
                case 5: return NkdTrendDeltaValue;
                case 6: return BtcTrendDeltaValue;
                case 7: return GcTrendDeltaValue;
                case 8: return HgTrendDeltaValue;
                default: return 0;
            }
        }

        private void ClearOpenTradeStatus(InstrumentTrendState s)
        {
            if (s != null)
                OpenTradeStatusExporter.Clear(OpenTradeStatusFileName(s));
        }

        // Cross-instrument safety net: catches a position this instance still thinks is open
        // (ActiveEntryPrice > 0) but the account has actually gone flat on -- e.g. a manual
        // Close from the Positions grid/SuperDOM, which never fires this instance's
        // OnPositionUpdate. Runs from every instrument's OnBarUpdate so it doesn't depend on
        // the affected instrument's own (possibly slow, volume-based) bar type ticking over.
        private void ReconcileStaleOpenTradeStatuses()
        {
            for (int i = 0; i < inst.Length; i++)
            {
                InstrumentTrendState other = inst[i];
                if (other == null || other.ActiveEntryPrice <= 0)
                    continue;

                Instrument otherInstrument = BarsArray[other.Bip] != null ? BarsArray[other.Bip].Instrument : null;
                if (otherInstrument == null || otherInstrument.MasterInstrument == null)
                    continue;

                Position realPosition = FindAccountPositionForInstrument(otherInstrument.FullName);
                if (realPosition == null || realPosition.MarketPosition == MarketPosition.Flat || realPosition.Quantity <= 0)
                {
                    double exitPrice = other.ActiveExitPrice;
                    int exitQuantity = other.ActiveExitQuantity;
                    string exitSignal = other.ActiveExitSignal;
                    DateTime exitTime = other.ActiveExitTime;

                    // OnExecutionUpdate never saw this fill (that's the whole reason this is
                    // stale), so pull the real exit fill straight from the account's execution
                    // history instead of leaving the completed-trades log blank for it.
                    if (exitPrice <= 0)
                        FindManualExitFill(otherInstrument, other.ActiveEntryFillTime, out exitPrice, out exitQuantity, out exitSignal, out exitTime);

                    if (exitPrice > 0)
                    {
                        AppendDashboardTradeOutcome(
                            other,
                            exitPrice,
                            Math.Max(1, exitQuantity > 0 ? exitQuantity : other.ActiveQuantity),
                            string.IsNullOrEmpty(exitSignal) ? "ManualClose" : exitSignal,
                            exitTime == DateTime.MinValue ? Times[other.Bip][0] : exitTime);
                    }

                    ResetInstrumentTradeState(other);
                    ClearOpenTradeStatus(other);
                }
            }
        }

        // Manual/ATM closes never reach this instance's OnExecutionUpdate, so the only way to
        // recover the real exit fill is to read it back from the account's own execution
        // history (which every fill on the account lands in, regardless of who submitted it).
        private void FindManualExitFill(Instrument instrument, DateTime afterTime, out double exitPrice, out int exitQuantity, out string exitSignal, out DateTime exitTime)
        {
            exitPrice = 0.0;
            exitQuantity = 0;
            exitSignal = string.Empty;
            exitTime = DateTime.MinValue;

            if (Account == null || Account.Executions == null || instrument == null)
                return;

            foreach (Execution execution in Account.Executions.ToList())
            {
                if (execution == null || execution.Instrument != instrument)
                    continue;
                if (afterTime != DateTime.MinValue && execution.Time <= afterTime)
                    continue;
                if (execution.Time < exitTime)
                    continue;

                exitTime = execution.Time;
                exitPrice = execution.Price;
                exitQuantity = execution.Quantity;
                exitSignal = execution.Order != null && !string.IsNullOrEmpty(execution.Order.Name) ? execution.Order.Name : "ManualClose";
            }
        }

        private Position FindAccountPositionForInstrument(string instrumentFullName)
        {
            if (Account == null || Account.Positions == null || string.IsNullOrEmpty(instrumentFullName))
                return null;

            foreach (Position accountPosition in Account.Positions)
            {
                if (accountPosition == null || accountPosition.Instrument == null)
                    continue;

                if (accountPosition.Instrument.FullName == instrumentFullName)
                    return accountPosition;
            }

            return null;
        }

        // ---------------------------------------------------- Candidate labeling

        private void RegisterPendingCandidate(InstrumentTrendState s, string direction, bool isNearMiss = false, int matchCount = 6)
        {
            if (!EnableSampleLogging)
                return;

            double[][] windowCopy = new double[s.FeatureWindow.Count][];
            for (int i = 0; i < s.FeatureWindow.Count; i++)
                windowCopy[i] = s.FeatureWindow[i];

            s.PendingCandidates.Add(new PendingCandidate
            {
                SignalBar = CurrentBars[s.Bip],
                Direction = direction,
                PriceAtSignal = Closes[s.Bip][0],
                AtrAtSignal = Math.Max(s.Atr[0], TickSizeFor(s)),
                Window = windowCopy,
                IsNearMiss = isNearMiss,
                MatchCount = matchCount,
            });
        }

        // Labels every candidate +2ATR/-1ATR in its direction, regardless of
        // whether the ML model actually took the trade -- this way rejected
        // candidates still produce training signal instead of being thrown away.
        private void EvaluatePendingCandidates(InstrumentTrendState s)
        {
            for (int i = s.PendingCandidates.Count - 1; i >= 0; i--)
            {
                PendingCandidate candidate = s.PendingCandidates[i];
                int barsElapsed = CurrentBars[s.Bip] - candidate.SignalBar;
                if (barsElapsed < LabelLookaheadBars)
                    continue;

                string label = ScoreCandidateOutcome(s, candidate);
                LogTrendSample(s, candidate, label);
                s.PendingCandidates.RemoveAt(i);
            }
        }

        private string ScoreCandidateOutcome(InstrumentTrendState s, PendingCandidate candidate)
        {
            double target = candidate.AtrAtSignal * 2.0;
            double stop = candidate.AtrAtSignal * 1.0;
            int barsBack = CurrentBars[s.Bip] - candidate.SignalBar;

            for (int i = barsBack; i >= 1; i--)
            {
                double barHigh = Highs[s.Bip][i];
                double barLow = Lows[s.Bip][i];

                if (candidate.Direction == "long")
                {
                    bool hitTarget = barHigh - candidate.PriceAtSignal >= target;
                    bool hitStop = candidate.PriceAtSignal - barLow >= stop;
                    if (hitTarget && !hitStop)
                        return "long";
                    if (hitStop && !hitTarget)
                        return "no_trade";
                    if (hitTarget && hitStop)
                        return "no_trade"; // ambiguous same-bar hit -- treat conservatively
                }
                else
                {
                    bool hitTarget = candidate.PriceAtSignal - barLow >= target;
                    bool hitStop = barHigh - candidate.PriceAtSignal >= stop;
                    if (hitTarget && !hitStop)
                        return "short";
                    if (hitStop && !hitTarget)
                        return "no_trade";
                    if (hitTarget && hitStop)
                        return "no_trade";
                }
            }
            return "no_trade";
        }

        private void LogTrendSample(InstrumentTrendState s, PendingCandidate candidate, string label)
        {
            if (!EnableSampleLogging)
                return;

            Instrument instrument = BarsArray[s.Bip] != null ? BarsArray[s.Bip].Instrument : null;
            string symbol = instrument != null && instrument.MasterInstrument != null ? instrument.MasterInstrument.Name : string.Empty;
            StringBuilder windowSb = new StringBuilder("[");
            for (int i = 0; i < candidate.Window.Length; i++)
            {
                if (i > 0) windowSb.Append(",");
                windowSb.Append("[");
                double[] row = candidate.Window[i];
                for (int j = 0; j < row.Length; j++)
                {
                    if (j > 0) windowSb.Append(",");
                    windowSb.Append(FormatJsonDouble(row[j]));
                }
                windowSb.Append("]");
            }
            windowSb.Append("]");

            string metadataJson = candidate.IsNearMiss
                ? ",\"metadata\":{\"near_miss\":true,\"match_count\":" + candidate.MatchCount + "}"
                : string.Empty;

            string json = "{"
                + "\"symbol\":\"" + JsonEscape(symbol) + "\","
                + "\"bars_period\":\"" + JsonEscape(DataSeriesLabel) + "\","
                + "\"label\":\"" + JsonEscape(label) + "\","
                + "\"window\":" + windowSb
                + metadataJson
                + "}";

            string sampleUrl = TrimTrailingSlash(MlServiceUrl) + "/log-trend-sample";
            FireAndForgetPostJson(sampleUrl, json);
        }

        private string BuildWindowJson(InstrumentTrendState s)
        {
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < s.FeatureWindow.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append("[");
                double[] row = s.FeatureWindow[i];
                for (int j = 0; j < row.Length; j++)
                {
                    if (j > 0) sb.Append(",");
                    sb.Append(FormatJsonDouble(row[j]));
                }
                sb.Append("]");
            }
            sb.Append("]");
            return sb.ToString();
        }

        // ------------------------------------------------------------- HTTP/JSON


        private bool ShouldPrintMlHttpError()
        {
            DateTime now = DateTime.UtcNow;
            lock (mlHttpErrorPrintLock)
            {
                if ((now - lastMlHttpErrorPrintUtc).TotalSeconds < MlHttpErrorPrintThrottleSeconds)
                    return false;

                lastMlHttpErrorPrintUtc = now;
                return true;
            }
        }
        private string PostJson(string url, string json)
        {
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

        private void FireAndForgetPostJson(string url, string json)
        {
            System.Threading.Tasks.Task.Run(() =>
            {
                try { PostJson(url, json); } catch { }
            });
        }

        private string ExtractJsonString(string json, string key)
        {
            string marker = "\"" + key + "\":";
            int start = json.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
            if (start < 0) return string.Empty;
            start = json.IndexOf('"', start + marker.Length);
            if (start < 0) return string.Empty;
            int end = json.IndexOf('"', start + 1);
            if (end < 0) return string.Empty;
            return json.Substring(start + 1, end - start - 1).ToLowerInvariant();
        }

        private double ExtractJsonDouble(string json, string key)
        {
            string marker = "\"" + key + "\":";
            int start = json.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
            if (start < 0) return 0.0;
            start += marker.Length;
            while (start < json.Length && char.IsWhiteSpace(json[start]))
                start++;
            int end = start;
            while (end < json.Length && "0123456789+-.eE".IndexOf(json[end]) >= 0)
                end++;
            double value;
            return double.TryParse(json.Substring(start, end - start), NumberStyles.Float, CultureInfo.InvariantCulture, out value) ? value : 0.0;
        }

        private bool ExtractJsonBool(string json, string key)
        {
            string marker = "\"" + key + "\":";
            int start = json.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
            if (start < 0) return false;
            start += marker.Length;
            while (start < json.Length && char.IsWhiteSpace(json[start]))
                start++;
            return json.IndexOf("true", start, StringComparison.OrdinalIgnoreCase) == start;
        }

        private string JsonEscape(string value)
        {
            return (value ?? string.Empty).Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private string FormatJsonDouble(double value)
        {
            if (double.IsNaN(value) || double.IsInfinity(value))
                value = 0.0;
            return value.ToString("0.########", CultureInfo.InvariantCulture);
        }

        private string TrimTrailingSlash(string value)
        {
            return string.IsNullOrWhiteSpace(value) ? "http://localhost:8767" : value.TrimEnd('/');
        }
    }
}
