#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public abstract class ActiveStopVisualStrategyBase : Strategy
    {
        [NinjaScriptProperty]
        [Display(Name = "Show Active Stop On Chart", GroupName = "Visual", Order = 0)]
        public bool ShowActiveStopOnChart { get; set; } = false;

        protected void RenderActiveStopVisual(double stopPrice)
        {
            const string lineTag = "CodexActiveStopLine";
            const string labelTag = "CodexActiveStopLabel";

            if (ChartControl == null || !ShowActiveStopOnChart || Position.MarketPosition == MarketPosition.Flat
                || stopPrice <= 0 || double.IsNaN(stopPrice) || double.IsInfinity(stopPrice))
            {
                RemoveDrawObject(lineTag);
                RemoveDrawObject(labelTag);
                return;
            }

            Draw.HorizontalLine(this, lineTag, stopPrice, Brushes.OrangeRed);
            Draw.Text(this, labelTag, "ACTIVE STOP " + stopPrice.ToString("0.00"), 0, stopPrice, Brushes.OrangeRed);
        }
    }
}
