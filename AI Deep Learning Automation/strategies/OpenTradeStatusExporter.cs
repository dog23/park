using System;
using System.Globalization;
using System.IO;

namespace NinjaTrader.NinjaScript.Strategies
{
    internal static class OpenTradeStatusExporter
    {
        private static readonly object fileLock = new object();
        // Named (OS-level) mutex, not just the in-process fileLock above -- every NinjaScript
        // recompile/auto-apply gives this static class a brand new instance with its own fileLock,
        // so an old not-yet-unloaded strategy instance and the newly compiled one share no
        // in-process lock at all and can write the same file genuinely concurrently. Same root
        // cause fixed in the sibling PullbackStateExporter.cs on 2026-07-17 (see ML_SYSTEM_GUIDE.txt
        // CHANGE LOG) -- applied here preventively since the pattern is identical, even though this
        // exporter hadn't been observed throwing the sharing-violation errors yet. One mutex for all
        // filenames (not keyed per file) because the original fileLock already serialized every
        // write across every strategy/ticker through a single lock, so this preserves that same
        // scope.
        private static readonly System.Threading.Mutex fileMutex =
            new System.Threading.Mutex(false, "NT8_OpenTradeStatusExporter_write");
        // "time" is refreshed on every write (heartbeat/OnBarUpdate) to the current
        // bar time, so it reflects "as of" rather than when the trade was opened.
        // "entryTime" is the fill time captured once at entry and held fixed for
        // the life of the trade -- that's what the dashboard's Entry Time/Length
        // columns key off of.
        private const string Header = "time\tstrategy\tticker\tdirection\tquantity\tentryPrice\tcurrentPrice\tunrealizedPnL\tbarsHeld\tentrySignal\taccount\treversal\ttemplateNumber\tentryTime\tbarsPeriodType\tbarsPeriodValue";

        public static string FileName(string strategyName, string ticker)
        {
            return SafeFileNamePart(strategyName) + "_" + SafeFileNamePart(ticker) + "_open_trades.tsv";
        }

        // barsPeriodType/barsPeriodValue are optional -- older callers that haven't been
        // updated to pass the strategy's bar series still write a valid row, just without
        // that trailing pair of columns filled in.
        public static string Row(DateTime time, string strategy, string ticker, string direction, int quantity, double entryPrice, double currentPrice, double unrealizedPnl, int barsHeld, string entrySignal, string account, bool reversal = false, int templateNumber = 0, DateTime entryTime = default(DateTime), string barsPeriodType = "", string barsPeriodValue = "")
        {
            return string.Join("\t", new[]
            {
                time.ToString("o", CultureInfo.InvariantCulture),
                strategy ?? string.Empty,
                ticker ?? string.Empty,
                direction ?? string.Empty,
                Math.Max(1, quantity).ToString(CultureInfo.InvariantCulture),
                entryPrice.ToString("0.########", CultureInfo.InvariantCulture),
                currentPrice.ToString("0.########", CultureInfo.InvariantCulture),
                unrealizedPnl.ToString("0.########", CultureInfo.InvariantCulture),
                Math.Max(0, barsHeld).ToString(CultureInfo.InvariantCulture),
                entrySignal ?? string.Empty,
                account ?? string.Empty,
                reversal ? "true" : "false",
                templateNumber.ToString(CultureInfo.InvariantCulture),
                (entryTime == default(DateTime) ? time : entryTime).ToString("o", CultureInfo.InvariantCulture),
                barsPeriodType ?? string.Empty,
                barsPeriodValue ?? string.Empty
            });
        }

        public static void Write(string fileName, string row)
        {
            WriteText(fileName, Header + Environment.NewLine + row + Environment.NewLine);
        }

        public static void WriteRows(string fileName, string[] rows)
        {
            string text = Header + Environment.NewLine;
            if (rows != null && rows.Length > 0)
                text += string.Join(Environment.NewLine, rows) + Environment.NewLine;

            WriteText(fileName, text);
        }

        public static void Clear(string fileName)
        {
            WriteText(fileName, Header + Environment.NewLine);
        }

        private static void WriteText(string fileName, string text)
        {
            string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, fileName);
            // Unique per call rather than a fixed "<file>.tmp" -- a shared temp name can collide
            // with a leftover/in-flight temp file from another writer (e.g. a not-yet-disabled old
            // strategy instance from before a recompile), which surfaces as a sharing violation.
            string tempPath = path + "." + Guid.NewGuid().ToString("N") + ".tmp";

            const int maxAttempts = 5;
            const int retryDelayMs = 20;

            bool mutexAcquired = false;
            try
            {
                // 2s is generous for a handful of small file ops; if it's still held that long,
                // something is genuinely stuck and we'd rather skip this write than block the
                // strategy thread indefinitely.
                mutexAcquired = fileMutex.WaitOne(TimeSpan.FromSeconds(2));
            }
            catch (System.Threading.AbandonedMutexException)
            {
                // Previous owner (e.g. an old strategy instance/process) exited without releasing --
                // we still got ownership, the shared state it was protecting (the file on disk) is
                // fine to proceed with.
                mutexAcquired = true;
            }

            if (!mutexAcquired)
            {
                NinjaTrader.Code.Output.Process(
                    "OpenTradeStatusExporter: Timed out waiting for write lock on '" + fileName + "'.",
                    PrintTo.OutputTab1);
                return;
            }

            lock (fileLock)
            {
                try
                {
                    for (int attempt = 1; attempt <= maxAttempts; attempt++)
                    {
                        try
                        {
                            // Write the full rewrite to a temp file and swap it in with File.Replace
                            // (atomic rename) rather than File.WriteAllText directly on the live path
                            // -- this file gets polled by the dashboard, and a direct in-place write
                            // could be caught mid-truncate as a blank/partial read. Try Replace first
                            // and fall back to Move on FileNotFoundException (destination doesn't
                            // exist yet) rather than pre-checking File.Exists, since that check-then-
                            // act ordering itself races against another writer.
                            File.WriteAllText(tempPath, text);
                            try
                            {
                                File.Replace(tempPath, path, null);
                            }
                            catch (FileNotFoundException)
                            {
                                File.Move(tempPath, path);
                            }
                            return;
                        }
                        catch (IOException ex)
                        {
                            if (attempt == maxAttempts)
                            {
                                NinjaTrader.Code.Output.Process(
                                    "OpenTradeStatusExporter: Unable to write '" + fileName +
                                    "' after " + maxAttempts + " attempts. " + ex.Message,
                                    PrintTo.OutputTab1);
                                DeleteTempFileQuietly(tempPath);
                                return;
                            }
                            System.Threading.Thread.Sleep(retryDelayMs);
                        }
                        catch (UnauthorizedAccessException ex)
                        {
                            NinjaTrader.Code.Output.Process(
                                "OpenTradeStatusExporter: Access denied for '" + fileName +
                                "'. " + ex.Message,
                                PrintTo.OutputTab1);
                            DeleteTempFileQuietly(tempPath);
                            return;
                        }
                        catch (Exception ex)
                        {
                            // Dashboard export is a side-channel and must never escape into strategy
                            // code -- an unhandled exception here would trip RealtimeErrorHandling
                            // .StopCancelClose on strategies and force-flatten a live position just
                            // because the dashboard file write failed (e.g. dashboard server/watchdog
                            // restarting and momentarily locking/touching the file).
                            NinjaTrader.Code.Output.Process(
                                "OpenTradeStatusExporter: Unexpected error writing '" + fileName +
                                "'. " + ex.Message,
                                PrintTo.OutputTab1);
                            DeleteTempFileQuietly(tempPath);
                            return;
                        }
                    }
                }
                finally
                {
                    fileMutex.ReleaseMutex();
                }
            }
        }

        private static void DeleteTempFileQuietly(string tempPath)
        {
            try
            {
                if (File.Exists(tempPath))
                    File.Delete(tempPath);
            }
            catch
            {
            }
        }

        private static string SafeFileNamePart(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "Unknown";

            char[] chars = value.ToCharArray();
            for (int i = 0; i < chars.Length; i++)
            {
                char c = chars[i];
                if (char.IsLetterOrDigit(c) || c == '-' || c == '_')
                    continue;

                chars[i] = '_';
            }

            return new string(chars).Trim('_');
        }
    }
}
