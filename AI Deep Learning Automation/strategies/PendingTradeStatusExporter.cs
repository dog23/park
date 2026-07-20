using System;
using System.Globalization;
using System.IO;

namespace NinjaTrader.NinjaScript.Strategies
{
    internal static class PendingTradeStatusExporter
    {
        private static readonly object fileLock = new object();
        // "time" refreshes every heartbeat like OpenTradeStatusExporter's -- "submittedTime" is
        // the fixed moment the limit order went in, which is what the dashboard's Length column
        // counts from.
        private const string Header = "time\tstrategy\tticker\tdirection\tquantity\tlimitPrice\tcurrentPrice\taccount\ttemplateNumber\tsubmittedTime\tbarsPeriodType\tbarsPeriodValue";

        public static string FileName(string strategyName, string ticker)
        {
            return SafeFileNamePart(strategyName) + "_" + SafeFileNamePart(ticker) + "_pending_trades.tsv";
        }

        public static string Row(DateTime time, string strategy, string ticker, string direction, int quantity, double limitPrice, double currentPrice, string account, int templateNumber, DateTime submittedTime, string barsPeriodType = "", string barsPeriodValue = "")
        {
            return string.Join("\t", new[]
            {
                time.ToString("o", CultureInfo.InvariantCulture),
                strategy ?? string.Empty,
                ticker ?? string.Empty,
                direction ?? string.Empty,
                Math.Max(1, quantity).ToString(CultureInfo.InvariantCulture),
                limitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                currentPrice.ToString("0.########", CultureInfo.InvariantCulture),
                account ?? string.Empty,
                templateNumber.ToString(CultureInfo.InvariantCulture),
                (submittedTime == default(DateTime) ? time : submittedTime).ToString("o", CultureInfo.InvariantCulture),
                barsPeriodType ?? string.Empty,
                barsPeriodValue ?? string.Empty
            });
        }

        public static void Write(string fileName, string row)
        {
            WriteText(fileName, Header + Environment.NewLine + row + Environment.NewLine);
        }

        public static void Clear(string fileName)
        {
            WriteText(fileName, Header + Environment.NewLine);
        }

        private static void WriteText(string fileName, string text)
        {
            string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, fileName);

            lock (fileLock)
            {
                const int maxAttempts = 5;
                const int retryDelayMs = 20;

                for (int attempt = 1; attempt <= maxAttempts; attempt++)
                {
                    try
                    {
                        File.WriteAllText(path, text);
                        return;
                    }
                    catch (IOException ex)
                    {
                        if (attempt == maxAttempts)
                        {
                            NinjaTrader.Code.Output.Process(
                                "PendingTradeStatusExporter: Unable to write '" + fileName +
                                "' after " + maxAttempts + " attempts. " + ex.Message,
                                PrintTo.OutputTab1);
                            return;
                        }
                        System.Threading.Thread.Sleep(retryDelayMs);
                    }
                    catch (UnauthorizedAccessException ex)
                    {
                        NinjaTrader.Code.Output.Process(
                            "PendingTradeStatusExporter: Access denied for '" + fileName +
                            "'. " + ex.Message,
                            PrintTo.OutputTab1);
                        return;
                    }
                    catch (Exception ex)
                    {
                        // Same reasoning as OpenTradeStatusExporter: this is a side-channel export and
                        // must never bubble into strategy code, or a dashboard file hiccup could trip
                        // RealtimeErrorHandling and force-flatten a live position.
                        NinjaTrader.Code.Output.Process(
                            "PendingTradeStatusExporter: Unexpected error writing '" + fileName +
                            "'. " + ex.Message,
                            PrintTo.OutputTab1);
                        return;
                    }
                }
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
