using System;
using System.Globalization;
using System.IO;

namespace NinjaTrader.NinjaScript.AddOns
{
    // Drop-in trade logger for the live dashboard's auto-discovery feature.
    // Call LogCompletedTrade(...) once per closed trade from any strategy's
    // exit-fill handling code (e.g. OnExecutionUpdate / OnPositionUpdate
    // when the position returns to Flat). No dashboard.py or dashboard.html
    // changes are needed - the server picks up any "<Name>_completed_trades.tsv"
    // file in the NinjaTrader 8 root automatically.
    public static class DashboardTradeLogger
    {
        private static readonly object writeLock = new object();

        public static void LogCompletedTrade(
            string strategyName,
            string ticker,
            string direction,
            double entryPrice,
            double exitPrice,
            int quantity,
            double pnl,
            string exitSignal,
            DateTime fillTime)
        {
            if (string.IsNullOrWhiteSpace(strategyName) || string.IsNullOrWhiteSpace(ticker))
                return;

            string safeName = strategyName.Trim();
            string path = Path.Combine(
                NinjaTrader.Core.Globals.UserDataDir,
                safeName + "_completed_trades.tsv");
            string header = "time\tticker\tdirection\tentryPrice\texitPrice\tquantity\tpnl\toutcome\texitSignal";
            string outcome = pnl > 0 ? "WIN" : pnl < 0 ? "LOSS" : "FLAT";

            string line = string.Join("\t", new[]
            {
                fillTime.ToString("o", CultureInfo.InvariantCulture),
                ticker.Trim().ToUpperInvariant(),
                direction.Trim().ToUpperInvariant(),
                entryPrice.ToString("0.########", CultureInfo.InvariantCulture),
                exitPrice.ToString("0.########", CultureInfo.InvariantCulture),
                Math.Max(1, quantity).ToString(CultureInfo.InvariantCulture),
                pnl.ToString("0.########", CultureInfo.InvariantCulture),
                outcome,
                exitSignal ?? string.Empty,
            });

            lock (writeLock)
            {
                if (!File.Exists(path) || new FileInfo(path).Length == 0)
                    File.WriteAllText(path, header + Environment.NewLine);

                File.AppendAllText(path, line + Environment.NewLine);
            }
        }
    }
}
