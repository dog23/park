using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;

namespace NinjaTrader.NinjaScript.Strategies
{
    internal static class PullbackStateExporter
    {
        private static readonly object fileLock = new object();
        private static readonly Dictionary<string, string> rowsByKey = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        private static bool seededFromDisk = false;
        private const string FileName = "TemaLimit_pullback_state.tsv";
        // Named (OS-level) mutex, not just the in-process fileLock above -- every NinjaScript
        // recompile/auto-apply gives this static class a brand new instance with its own fileLock,
        // so an old not-yet-unloaded strategy instance and the newly compiled one share no in-process
        // lock at all and were writing the file genuinely concurrently (sharing violations, "already
        // exists", "unable to remove the file to be replaced"). A named Mutex is looked up by name in
        // the OS, so it's shared across those separate instances/AppDomains regardless.
        private static readonly System.Threading.Mutex fileMutex =
            new System.Threading.Mutex(false, "NT8_PullbackStateExporter_TemaLimit_pullback_state");
        private const string Header = "time\tticker\tbarsPeriodType\tbarsPeriodValue\tatr\tatrAvg\tatrRatio\tbasePullbackTicks\tlivePullbackTicks";

        public static void Update(DateTime time, string ticker, string barsPeriodType, string barsPeriodValue, double atr, double atrAvg, double atrRatio, int basePullbackTicks, int livePullbackTicks)
        {
            if (string.IsNullOrEmpty(ticker))
                return;

            // Each hot-reload/auto-apply gets a fresh static rowsByKey (see WriteText comment below),
            // so a newly loaded instance used to know only about the instrument that just closed a
            // bar and would overwrite the file with just that one row, wiping every other
            // instrument's row until it happened to get its own next bar close. Seed once from
            // whatever the previous instance last wrote so a fresh process starts with the full
            // picture instead of an empty one.
            EnsureSeededFromDisk();

            string row = string.Join("\t", new[]
            {
                time.ToString("o", CultureInfo.InvariantCulture),
                ticker,
                barsPeriodType ?? string.Empty,
                barsPeriodValue ?? string.Empty,
                atr.ToString("0.########", CultureInfo.InvariantCulture),
                atrAvg.ToString("0.########", CultureInfo.InvariantCulture),
                atrRatio.ToString("0.###", CultureInfo.InvariantCulture),
                basePullbackTicks.ToString(CultureInfo.InvariantCulture),
                livePullbackTicks.ToString(CultureInfo.InvariantCulture)
            });

            // Keyed by ticker+data series (not ticker alone) -- multiple strategy instances can
            // trade the same ticker concurrently on different bar types, and each needs its own row.
            string key = ticker + "|" + (barsPeriodType ?? string.Empty) + "|" + (barsPeriodValue ?? string.Empty);

            lock (fileLock)
            {
                rowsByKey[key] = row;
                string text = Header + Environment.NewLine
                    + string.Join(Environment.NewLine, rowsByKey.OrderBy(kv => kv.Key).Select(kv => kv.Value))
                    + Environment.NewLine;
                WriteText(text);
            }
        }

        private static void EnsureSeededFromDisk()
        {
            if (seededFromDisk)
                return;

            lock (fileLock)
            {
                if (seededFromDisk)
                    return;
                seededFromDisk = true;

                try
                {
                    string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, FileName);
                    if (!File.Exists(path))
                        return;

                    string[] lines = File.ReadAllLines(path);
                    for (int i = 1; i < lines.Length; i++) // skip header
                    {
                        string line = lines[i];
                        if (string.IsNullOrWhiteSpace(line))
                            continue;

                        string[] cols = line.Split('\t');
                        if (cols.Length < 9)
                            continue;

                        string key = cols[1] + "|" + cols[2] + "|" + cols[3];
                        rowsByKey[key] = line;
                    }
                }
                catch
                {
                    // Best-effort seed only -- a bad/partial read just means we start empty like
                    // before, not worse.
                }
            }
        }

        private static void WriteText(string text)
        {
            string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, FileName);
            // Unique per call rather than a fixed "<file>.tmp" -- a shared temp name can collide
            // with a leftover/in-flight temp file from another writer (e.g. a not-yet-disabled old
            // strategy instance from before a recompile), which surfaces as a sharing violation.
            string tempPath = path + "." + Guid.NewGuid().ToString("N") + ".tmp";

            // Escalating backoff (20,40,80,...ms, ~1.3s total) rather than a flat 5x20ms -- the
            // Replace swap makes each version a brand-new file to Windows, so Defender/Search
            // re-scan it after every bar close and can hold a restrictive lock for a few hundred
            // ms, longer than the old ~100ms total budget.
            const int maxAttempts = 7;
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
                    "PullbackStateExporter: Timed out waiting for write lock on '" + FileName + "'.",
                    PrintTo.OutputTab1);
                return;
            }

            try
            {
                for (int attempt = 1; attempt <= maxAttempts; attempt++)
                {
                    try
                    {
                        // Write the full rewrite to a temp file and swap it in with File.Replace
                        // (atomic rename) rather than File.WriteAllText directly on the live path --
                        // this file gets rewritten from scratch on every bar close across every
                        // ticker/data series, and a reader polling every second was catching it
                        // mid-truncate and seeing a blank file.
                        //
                        // Try Replace first and fall back to Move on FileNotFoundException
                        // (destination doesn't exist yet) instead of pre-checking File.Exists -- a
                        // stale exists-then-act check races against other instances of this same
                        // strategy, which surfaced as "Cannot create a file when that file already
                        // exists" (Move lost the race) and "Unable to remove the file to be replaced"
                        // (Replace hit a destination another instance was mid-swap on).
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
                                "PullbackStateExporter: Unable to write '" + FileName +
                                "' after " + maxAttempts + " attempts. " + ex.Message,
                                PrintTo.OutputTab1);
                            DeleteTempFileQuietly(tempPath);
                            return;
                        }
                        System.Threading.Thread.Sleep(retryDelayMs << (attempt - 1));
                    }
                    catch (UnauthorizedAccessException ex)
                    {
                        NinjaTrader.Code.Output.Process(
                            "PullbackStateExporter: Access denied for '" + FileName +
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
                            "PullbackStateExporter: Unexpected error writing '" + FileName +
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
    }
}
