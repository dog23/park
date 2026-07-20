using System;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;

namespace NinjaTrader.NinjaScript.AddOns
{
    // Background bridge for the live dashboard's trade-chart feature (LiveDashboardServer,
    // port 8766). Fully decoupled from every live strategy -- it never touches strategy
    // code, order flow, or OnBarUpdate, so a bug here cannot affect live trading.
    //
    // Protocol (file-based, same style as ManualExitCommand.cs):
    //   Dashboard writes  <UserDataDir>\ChartRequests\<id>.request.json  { ticker, fromTime, toTime }
    //   This AddOn writes <UserDataDir>\ChartRequests\<id>.json          { ok, instrument, bars:[...] }
    //   and deletes the .request.json once handled.
    //
    // JSON is hand-rolled (no Newtonsoft/System.Text.Json reference available in the
    // default NinjaScript compile context) -- both sides of this protocol are simple,
    // flat, machine-generated documents, so minimal string-based parsing is sufficient.
    //
    // Runs on a plain background timer for the lifetime of the NinjaTrader process --
    // no chart window or strategy instance required.
    public class ChartDataExporter : AddOnBase
    {
        private Timer pollTimer;
        private readonly object pollLock = new object();
        private bool polling;

        private static readonly Regex ContractFolderRe = new Regex(@"^(?<root>[A-Z0-9]+) (?<mm>\d{2})-(?<yy>\d{2})$", RegexOptions.Compiled);

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "ChartDataExporter";
            }
            else if (State == State.Configure)
            {
                pollTimer = new Timer(OnTimerTick, null, TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(2));
            }
            else if (State == State.Terminated)
            {
                if (pollTimer != null)
                {
                    pollTimer.Dispose();
                    pollTimer = null;
                }
            }
        }

        private void OnTimerTick(object _)
        {
            // Timer callbacks can overlap if a BarsRequest is slow to resolve; skip
            // re-entrant ticks instead of piling up parallel scans of the same directory.
            lock (pollLock)
            {
                if (polling)
                    return;
                polling = true;
            }

            try
            {
                ProcessRequests();
                CheckStuckFetches();
                CleanupOldFiles();
            }
            catch (Exception ex)
            {
                LogError("tick failed: " + ex.Message);
            }
            finally
            {
                lock (pollLock)
                {
                    polling = false;
                }
            }
        }

        private static string RequestDir()
        {
            string dir = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "ChartRequests");
            Directory.CreateDirectory(dir);
            return dir;
        }

        // BarsRequest.Request() is fire-and-forget -- if its callback never fires (e.g.
        // it was issued during one of Tradovate's frequent sub-second WebSocket
        // reconnect blips -- see trace logs for "ReceiveWebSocketMarketDataMessage" /
        // "ConnectionLost" pairs that resolve a second later), there's otherwise no
        // trace anywhere that a fetch was even attempted, and the request just hangs
        // forever. Track in-flight ones here so CheckStuckFetches can retry a stalled
        // request (the feed is almost always back within a second or two) instead of
        // making the dashboard wait out a full 20s timeout for what's usually a
        // transient blip, and can still surface a real failure if retries exhaust.
        private class PendingFetch
        {
            public string Ticker;
            public DateTime FromTime;
            public DateTime ToTime;
            public string ResponsePath;
            public string ProcessingPath;
            public DateTime AttemptStartedAt;
            public int Generation;
            public int RetryCount;
            // Strong roots for every BarsRequest attempt issued for this fetch. An
            // in-flight BarsRequest issued from an AddOn has no other provable strong
            // reference (its callback delegate is only referenced by the request
            // itself), so without this a GC pass could collect one mid-flight and the
            // callback would silently never fire. Freed when the fetch finalizes and
            // this PendingFetch leaves pendingFetches.
            public readonly System.Collections.Generic.List<BarsRequest> LiveRequests =
                new System.Collections.Generic.List<BarsRequest>();
        }

        private readonly System.Collections.Generic.Dictionary<string, PendingFetch> pendingFetches =
            new System.Collections.Generic.Dictionary<string, PendingFetch>();
        private static readonly TimeSpan RetryAfter = TimeSpan.FromSeconds(6);
        private const int MaxRetries = 3;

        private void ProcessRequests()
        {
            string dir = RequestDir();
            foreach (string requestPath in Directory.GetFiles(dir, "*.request.json"))
            {
                string id = Path.GetFileName(requestPath).Replace(".request.json", "");
                string responsePath = Path.Combine(dir, id + ".json");

                // Claim the request immediately so a slow BarsRequest doesn't cause the
                // next tick to start a duplicate fetch for the same trade.
                string processingPath = requestPath + ".processing";
                try
                {
                    File.Move(requestPath, processingPath);
                }
                catch (IOException)
                {
                    continue; // another tick (or the file write) is mid-flight
                }

                try
                {
                    string json = File.ReadAllText(processingPath);
                    string ticker = JsonStringField(json, "ticker") ?? "";
                    DateTime fromTime = DateTime.Parse(JsonStringField(json, "fromTime"), CultureInfo.InvariantCulture, DateTimeStyles.None);
                    DateTime toTime = DateTime.Parse(JsonStringField(json, "toTime"), CultureInfo.InvariantCulture, DateTimeStyles.None);
                    LogInfo("fetch started: id=" + id + " ticker=" + ticker + " from=" + fromTime.ToString("o", CultureInfo.InvariantCulture) + " to=" + toTime.ToString("o", CultureInfo.InvariantCulture));
                    var pending = new PendingFetch
                    {
                        Ticker = ticker, FromTime = fromTime, ToTime = toTime,
                        ResponsePath = responsePath, ProcessingPath = processingPath,
                        AttemptStartedAt = DateTime.Now, Generation = 0, RetryCount = 0,
                    };
                    // processingPath is deliberately NOT deleted here -- it stays on disk as
                    // the "still in flight" marker (the dashboard's own staleness check looks
                    // for it) until FetchBars's callback actually resolves, in CheckStuckFetches
                    // on final timeout, or in the catch below for a synchronous parse failure.
                    // Deleting it immediately (the old behavior) made the request look "not in
                    // flight" to the dashboard the instant it was claimed, so a stuck BarsRequest
                    // whose callback never fires got silently re-issued every ~1-2s forever
                    // instead of ever reaching the timeout/retry logic.
                    lock (pollLock) { pendingFetches[id] = pending; }
                    FetchBars(pending, id);
                }
                catch (Exception ex)
                {
                    WriteError(responsePath, "request failed: " + ex.Message);
                    lock (pollLock) { pendingFetches.Remove(id); }
                    try { File.Delete(processingPath); } catch { /* best effort */ }
                }
            }
        }

        private void FetchBars(PendingFetch pending, string id)
        {
            string responsePath = pending.ResponsePath;
            string processingPath = pending.ProcessingPath;
            int generation = pending.Generation;

            Instrument instrument = ResolveInstrument(pending.Ticker);
            if (instrument == null)
            {
                WriteError(responsePath, "could not resolve instrument for ticker '" + pending.Ticker + "'");
                lock (pollLock) { pendingFetches.Remove(id); }
                try { File.Delete(processingPath); } catch { /* best effort */ }
                return;
            }

            BarsRequest request = new BarsRequest(instrument, pending.FromTime, pending.ToTime)
            {
                BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Minute, Value = 1 },
                TradingHours = instrument.MasterInstrument.TradingHours,
            };
            lock (pollLock) { pending.LiveRequests.Add(request); } // GC root -- see PendingFetch.LiveRequests

            Action issueRequest = () => request.Request((barsRequest, errorCode, errorMessage) =>
            {
                // Attempts run concurrently: a retry from CheckStuckFetches bumps
                // pending.Generation and issues a fresh BarsRequest for the same id
                // while older attempts are still in flight. Any attempt that comes back
                // with usable bars may finalize the fetch (first success wins) --
                // discarding a late-but-good callback just because a retry had already
                // been issued starved every fetch slower than the 6s retry window into
                // a bogus "no callback" timeout, even though its data arrived on every
                // attempt (2026-07-17 bug). Failures may only finalize from the newest
                // attempt, so a stale error can't kill a retry that might still
                // succeed. Whichever callback removes the id from pendingFetches owns
                // cleanup; every other callback discards (with a log, so a discarded
                // callback is never again invisible).
                bool gotBars = errorCode == ErrorCode.NoError && barsRequest.Bars != null && barsRequest.Bars.Count > 0;
                bool superseded;
                lock (pollLock)
                {
                    PendingFetch current;
                    if (!pendingFetches.TryGetValue(id, out current))
                    {
                        LogInfo("late callback after fetch already finalized, discarding: id=" + id + " attempt=" + generation);
                        return;
                    }
                    superseded = current.Generation != generation;
                    if (superseded && !gotBars)
                    {
                        LogInfo("failed callback from superseded attempt " + generation + " while attempt " + current.Generation + " is still in flight, discarding: id=" + id);
                        return;
                    }
                    pendingFetches.Remove(id);
                }

                try
                {
                    if (errorCode != ErrorCode.NoError)
                    {
                        LogError("BarsRequest error for id=" + id + ": " + errorMessage);
                        WriteError(responsePath, "BarsRequest error: " + errorMessage);
                        return;
                    }

                    if (barsRequest.Bars == null || barsRequest.Bars.Count == 0)
                    {
                        LogError("no bars returned for id=" + id + " ticker=" + pending.Ticker);
                        WriteError(responsePath, "no bars returned");
                        return;
                    }

                    Bars bars = barsRequest.Bars;
                    var sb = new StringBuilder();
                    sb.Append("{\"ok\":true,\"instrument\":");
                    AppendJsonString(sb, instrument.FullName);
                    sb.Append(",\"bars\":[");
                    for (int i = 0; i < bars.Count; i++)
                    {
                        if (i > 0)
                            sb.Append(',');
                        sb.Append("{\"t\":");
                        AppendJsonString(sb, BarTimeToUtc(bars.GetTime(i)).ToString("o", CultureInfo.InvariantCulture));
                        sb.Append(",\"o\":").Append(bars.GetOpen(i).ToString(CultureInfo.InvariantCulture));
                        sb.Append(",\"h\":").Append(bars.GetHigh(i).ToString(CultureInfo.InvariantCulture));
                        sb.Append(",\"l\":").Append(bars.GetLow(i).ToString(CultureInfo.InvariantCulture));
                        sb.Append(",\"c\":").Append(bars.GetClose(i).ToString(CultureInfo.InvariantCulture));
                        sb.Append(",\"v\":").Append(bars.GetVolume(i).ToString(CultureInfo.InvariantCulture));
                        sb.Append('}');
                    }
                    sb.Append("]}");
                    AtomicWrite(responsePath, sb.ToString());
                    LogInfo("fetch completed: id=" + id + " bars=" + bars.Count
                        + (generation > 0 ? " (retry " + generation + ")" : "")
                        + (superseded ? " (late success from a superseded attempt)" : ""));
                }
                catch (Exception ex)
                {
                    LogError("bars callback failed for id=" + id + ": " + ex.Message);
                    WriteError(responsePath, "bars callback failed: " + ex.Message);
                }
                finally
                {
                    // id was already removed from pendingFetches inside the claim lock
                    // above; only the on-disk in-flight marker is left to clear.
                    try { File.Delete(processingPath); } catch { /* best effort */ }
                }
            });

            // This AddOn's poll loop runs on a System.Threading.Timer callback -- a raw
            // ThreadPool thread, not NinjaTrader's own UI/Dispatcher thread. A chart window
            // always issues its own historical BarsRequest from the UI thread. Confirmed via
            // 2026-07-16 trace/connection investigation that the connection itself is fine
            // (native charts on the same connection load bars instantly) while this AddOn's
            // BarsRequest.Request() callback never fires at all -- marshaling the request
            // itself onto the Dispatcher thread tests whether that thread mismatch is why.
            var dispatcher = System.Windows.Application.Current != null ? System.Windows.Application.Current.Dispatcher : null;
            if (dispatcher != null && !dispatcher.CheckAccess())
            {
                LogInfo("dispatching BarsRequest to UI thread: id=" + id);
                dispatcher.BeginInvoke(issueRequest);
            }
            else
            {
                issueRequest();
            }
        }

        // Bars.GetTime() returns an Unspecified-Kind DateTime in NinjaTrader's
        // configured display timezone (Globals.GeneralOptions.TimeZoneInfo) -- NOT
        // Windows system-local time. The dashboard's entry/exit fill times, by
        // contrast, come from strategy code's DateTime.Now (system-local, Kind=Local),
        // whose "o" string includes a UTC offset. An offset-less "o" string here got
        // mis-parsed by the browser as system-local time, so whenever NT's display
        // timezone differs from Windows' (e.g. charts kept in Exchange/Central time
        // while the OS clock is Eastern), every candle landed shifted from the
        // correctly-placed entry/exit markers by that offset. Converting to a real
        // UTC instant here makes both sides parse to the same absolute time.
        private static DateTime BarTimeToUtc(DateTime barTime)
        {
            try
            {
                return TimeZoneInfo.ConvertTimeToUtc(
                    DateTime.SpecifyKind(barTime, DateTimeKind.Unspecified),
                    NinjaTrader.Core.Globals.GeneralOptions.TimeZoneInfo);
            }
            catch
            {
                return DateTime.SpecifyKind(barTime, DateTimeKind.Utc);
            }
        }

        // Trade logs only store the root symbol ("NQ"), not the specific contract
        // ("NQ 09-26"). Resolve it by scanning the same db\minute folder the strategies'
        // own historical data lives in, preferring the nearest contract whose expiry
        // hasn't passed. Falls back to trying the root ticker directly (works for
        // continuous/generic instrument setups).
        private Instrument ResolveInstrument(string rootTicker)
        {
            rootTicker = (rootTicker ?? "").Trim().ToUpperInvariant();
            if (rootTicker.Length == 0)
                return null;

            string minuteDir = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "db", "minute");
            string bestName = null;
            DateTime bestExpiry = DateTime.MaxValue;

            if (Directory.Exists(minuteDir))
            {
                foreach (string sub in Directory.GetDirectories(minuteDir))
                {
                    string folderName = Path.GetFileName(sub);
                    Match m = ContractFolderRe.Match(folderName);
                    if (!m.Success || !string.Equals(m.Groups["root"].Value, rootTicker, StringComparison.OrdinalIgnoreCase))
                        continue;

                    int month = int.Parse(m.Groups["mm"].Value, CultureInfo.InvariantCulture);
                    int year = 2000 + int.Parse(m.Groups["yy"].Value, CultureInfo.InvariantCulture);
                    DateTime expiry = new DateTime(year, month, 1);

                    // Prefer the closest contract to "now" (front month), not just the
                    // first match -- multiple expiries can exist on disk at once.
                    if (Math.Abs((expiry - DateTime.Now).Ticks) < Math.Abs((bestExpiry - DateTime.Now).Ticks))
                    {
                        bestExpiry = expiry;
                        bestName = folderName;
                    }
                }
            }

            if (bestName != null)
            {
                Instrument resolved = TryGetInstrument(bestName);
                if (resolved != null)
                    return resolved;
            }

            return TryGetInstrument(rootTicker);
        }

        private static Instrument TryGetInstrument(string name)
        {
            try
            {
                return Instrument.GetInstrument(name);
            }
            catch
            {
                return null;
            }
        }

        private static void WriteError(string responsePath, string message)
        {
            var sb = new StringBuilder();
            sb.Append("{\"ok\":false,\"error\":");
            AppendJsonString(sb, message);
            sb.Append('}');
            AtomicWrite(responsePath, sb.ToString());
        }

        private static void AtomicWrite(string path, string content)
        {
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, content, Encoding.UTF8);
            File.Copy(tmp, path, true);
            File.Delete(tmp);
        }

        private static void AppendJsonString(StringBuilder sb, string value)
        {
            sb.Append('"');
            foreach (char c in value ?? "")
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        // Extracts the string value of a top-level "key":"value" pair from a flat JSON
        // object. Not a general-purpose parser -- only handles the simple one-level
        // request documents this protocol actually sends.
        private static string JsonStringField(string json, string key)
        {
            Match m = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
            if (!m.Success)
                return null;
            return Regex.Unescape(m.Groups[1].Value);
        }

        // A BarsRequest whose callback never fires -- most often because it was issued
        // during one of the feed's brief WebSocket reconnect blips (Tradovate typically
        // recovers in well under a second, see trace logs for
        // "ReceiveWebSocketMarketDataMessage" / "ConnectionLost" pairs) -- otherwise
        // leaves the request in pendingFetches forever with no response file and no
        // error, and the dashboard just spins on "Loading price data..." with nothing
        // anywhere to explain why. Since the feed almost always recovers within a
        // couple seconds, retry the same request a few times before giving up, instead
        // of making every stall look like a dead connection that needs the full 20s
        // timeout.
        private void CheckStuckFetches()
        {
            System.Collections.Generic.List<System.Tuple<string, PendingFetch>> toRetry = null;
            System.Collections.Generic.List<string> toFail = null;
            lock (pollLock)
            {
                foreach (var kv in pendingFetches)
                {
                    string id = kv.Key;
                    PendingFetch pending = kv.Value;
                    TimeSpan waited = DateTime.Now - pending.AttemptStartedAt;
                    if (waited <= RetryAfter)
                        continue;

                    if (pending.RetryCount < MaxRetries)
                    {
                        pending.RetryCount++;
                        pending.Generation++;
                        pending.AttemptStartedAt = DateTime.Now;
                        if (toRetry == null) toRetry = new System.Collections.Generic.List<System.Tuple<string, PendingFetch>>();
                        toRetry.Add(System.Tuple.Create(id, pending));
                    }
                    else
                    {
                        if (toFail == null) toFail = new System.Collections.Generic.List<string>();
                        toFail.Add(id);
                    }
                }
                if (toFail != null)
                {
                    foreach (string id in toFail)
                        pendingFetches.Remove(id);
                }
            }

            if (toRetry != null)
            {
                foreach (var pair in toRetry)
                {
                    LogError("fetch stalled after " + RetryAfter.TotalSeconds + "s with no callback, retrying (" + pair.Item2.RetryCount + "/" + MaxRetries + "): id=" + pair.Item1);
                    FetchBars(pair.Item2, pair.Item1);
                }
            }

            if (toFail == null)
                return;

            string dir = RequestDir();
            foreach (string id in toFail)
            {
                string responsePath = Path.Combine(dir, id + ".json");
                LogError("fetch timed out after " + MaxRetries + " retries with genuinely no callback from any attempt: id=" + id + " -- before suspecting Connections, verify a native chart fails to load history for the same instrument (2026-07-16: it didn't, the connection was fine)");
                WriteError(responsePath, "timed out waiting for historical data");
                try { File.Delete(Path.Combine(dir, id + ".request.json.processing")); } catch { /* best effort */ }
            }
        }

        private void CleanupOldFiles()
        {
            string dir = RequestDir();
            DateTime cutoff = DateTime.Now.AddHours(-6);
            foreach (string file in Directory.GetFiles(dir))
            {
                try
                {
                    if (File.GetLastWriteTime(file) < cutoff)
                        File.Delete(file);
                }
                catch
                {
                    // best effort cleanup, ignore locked/missing files
                }
            }
        }

        private static void LogError(string message)
        {
            try
            {
                NinjaTrader.Code.Output.Process("ChartDataExporter: " + message, PrintTo.OutputTab1);
            }
            catch
            {
                // Output tab not available (e.g. very early startup) -- swallow, this is
                // diagnostic only and must never affect the host process.
            }
        }

        // Routine progress chatter (fetch started/dispatched/completed, discarded duplicate
        // callbacks) is intentionally NOT printed -- it was pure noise in OutputTab1. Flip
        // LogInfoEnabled to true to get it back when debugging a fetch. Errors still print.
        private const bool LogInfoEnabled = false;

        private static void LogInfo(string message)
        {
            if (!LogInfoEnabled)
                return;

            try
            {
                NinjaTrader.Code.Output.Process("ChartDataExporter: " + message, PrintTo.OutputTab1);
            }
            catch
            {
                // Output tab not available (e.g. very early startup) -- swallow, this is
                // diagnostic only and must never affect the host process.
            }
        }
    }
}
