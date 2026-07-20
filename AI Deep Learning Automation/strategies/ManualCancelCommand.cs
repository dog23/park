using System;
using System.IO;

namespace NinjaTrader.NinjaScript.Strategies
{
    // Lets the live dashboard request cancellation of a strategy's working limit entry
    // order. The dashboard writes a small command file via POST /api/cancel; the
    // strategy polls for it once per bar and, if found, consumes (deletes) it and
    // cancels its own entry order. Mirrors ManualExitCommand.cs.
    internal static class ManualCancelCommand
    {
        private static readonly object fileLock = new object();

        public static string FileName(string strategyName, string tickerAccountKey)
        {
            return SafeFileNamePart(strategyName) + "_" + SafeFileNamePart(tickerAccountKey) + "_cancel_command.txt";
        }

        public static bool ConsumeIfRequested(string strategyName, string tickerAccountKey)
        {
            string path = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, FileName(strategyName, tickerAccountKey));

            lock (fileLock)
            {
                if (!File.Exists(path))
                    return false;

                try
                {
                    File.Delete(path);
                    return true;
                }
                catch (IOException)
                {
                    return false;
                }
                catch (UnauthorizedAccessException)
                {
                    return false;
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
