<#
.SYNOPSIS
    Pulls GEX-style levels from the Unusual Whales API for the futures proxy
    ETFs (SPY/QQQ/IWM/DIA) and writes them into the CSV files the GEX
    NinjaTrader strategy reads (GEX_Levels_{Proxy}.csv in the NinjaTrader 8
    user data folder).

.IMPORTANT - READ BEFORE TRUSTING THE OUTPUT
    Unusual Whales does not expose your Vol Desk system's exact fields.
    This script maps what IS available to your CSV schema as follows:

        PTrans           = gamma_flip            (zero-gamma crossing)
        NTrans           = put_wall               (largest put gamma / floor)
        PlusGex          = call_wall               (largest call gamma / resistance)
        PlusGexNext      = 0 (not available -> strategy falls back to Cotmc)
        Cotmc            = call_wall               (no distinct 2nd upside level available)
        ZeroGex          = gamma_flip
        CotmpCushionPct  = (spot - put_wall) / spot * 100
        RR               = (call_wall - spot) / (spot - put_wall)
        DbChange         = day-over-day change in a call/put delta-balance ratio
                            derived from /greek-exposure (NOT the same scale as
                            your equity system's db_change -- treat the 0.50/0.30
                            thresholds in GEX.cs as a starting point to calibrate,
                            not a validated cutoff, until you've watched this
                            proxy metric for a couple of weeks)
        DbSustained      = 1 if that ratio has been >= 0.98 for two sessions running
        Grade            = FIXED PLACEHOLDER (-GradeOverride, default 9). There is
                            no equivalent to your 11-rule structural grade in UW's
                            data. This effectively disables the grade filter until
                            you build real structural checks.
        SpikeCrash       = FIXED PLACEHOLDER (always 0/false). This script cannot
                            detect a spike-crash pattern. MANUALLY confirm the
                            +GEX (call_wall) target isn't a prior spike-crash high
                            before trusting any signal -- this was a 0% win-rate,
                            hard-block rule in your validated system.
        BasketGatePass   = real: SPY's own session %% change > 0.5%%
        BullBearRatioOk  = MANUAL OVERRIDE (-BullBearOverride, default $true).
                            Impossible to compute from 4 tickers; read the real
                            3:1 bull:bear ratio off your own 700-name gamma
                            screen each morning and pass it in.
        VixGatePass      = real, best-effort: net dealer delta on VIX < 0
                            (falls back to $true with a warning if VIX isn't
                            supported on this endpoint for your plan)
        ProxySpot        = the ETF's own close price at fetch time. NinjaTrader
                            has no equity/ETF data feed, so the strategy can't
                            compute a live futures/ETF ratio -- instead it
                            divides the futures' current price by this frozen
                            morning value to convert ETF-quoted levels into
                            futures points for the whole session. This is an
                            approximation (real basis drifts intraday), not a
                            live-tracking ratio.

    SHORT-SIDE MIRROR (see GEX.cs header for the full long/short mapping):
        CotmcCushionPct       = (call_wall - spot) / spot * 100
        RRShort               = (spot - put_wall) / (call_wall - spot)
        DbSustainedShort      = 1 if the delta-balance ratio has been <= 0.02
                                  for two sessions running
        SpikeRally            = FIXED PLACEHOLDER (always 0/false) -- manually
                                  confirm the put_wall target isn't a prior
                                  spike-crash LOW before trusting a short signal
        BasketGatePassShort   = real: SPY's own session %% change < -0.5%%
        VixGatePassShort      = real, best-effort: net dealer delta on VIX > 0
        BullBearRatioOkShort  = MANUAL OVERRIDE (-BearBullOverride, default
                                  $true). Read your own screen's bear:bull
                                  ratio each morning.

.PARAMETER ApiKey
    Unusual Whales API bearer token. Defaults to $env:UW_API_KEY.

.PARAMETER Tickers
    Proxy ETFs to fetch. Defaults to SPY, QQQ, IWM, DIA (ES/NQ/RTY/YM).

.PARAMETER GradeOverride
    Placeholder Grade written to every row until real structural grading exists.

.PARAMETER BullBearOverride
    Manually read off your own gamma screen each morning ($true/$false).

.EXAMPLE
    $env:UW_API_KEY = "..."
    .\Fetch-GexLevels.ps1

.EXAMPLE
    .\Fetch-GexLevels.ps1 -ApiKey "..." -BullBearOverride $false
#>

param(
    [string]$ApiKey = $env:UW_API_KEY,
    [string[]]$Tickers = @("SPY", "QQQ", "IWM", "DIA"),
    [int]$GradeOverride = 9,
    [bool]$BullBearOverride = $true,
    [bool]$BearBullOverride = $true,
    [string]$OutputDir = (Join-Path $env:USERPROFILE "Documents\NinjaTrader 8")
)

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "No API key. Pass -ApiKey or set `$env:UW_API_KEY first."
}

$BaseUrl = "https://api.unusualwhales.com/api"
$Headers = @{ Authorization = "Bearer $ApiKey" }
$TodayStr = (Get-Date).ToString("yyyy-MM-dd")

function Invoke-UW {
    param([string]$Path, [hashtable]$Query = @{})

    $qs = ($Query.GetEnumerator() | ForEach-Object { "$($_.Key)=$([uri]::EscapeDataString([string]$_.Value))" }) -join "&"
    $url = "$BaseUrl$Path"
    if ($qs) { $url = "$url`?$qs" }

    try {
        return Invoke-RestMethod -Uri $url -Headers $Headers -Method Get -ErrorAction Stop
    }
    catch {
        Write-Warning "UW request failed: $url -> $($_.Exception.Message)"
        return $null
    }
}

function ToDouble {
    param($val, [double]$default = 0.0)
    if ($null -eq $val -or $val -eq "") { return $default }
    $d = 0.0
    if ([double]::TryParse([string]$val, [ref]$d)) { return $d }
    return $default
}

# -- Basket gate: SPY's own session % change --------------------------------

$basketGatePass = $false
$spyState = Invoke-UW -Path "/stock/SPY/stock-state"
if ($spyState -and $spyState.data) {
    $close = ToDouble $spyState.data.close
    $prevClose = ToDouble $spyState.data.prev_close
    if ($prevClose -gt 0) {
        $pctChange = (($close - $prevClose) / $prevClose) * 100.0
        $basketGatePass = $pctChange -gt 0.5
        Write-Host "Basket gate: SPY $($pctChange.ToString('0.00'))% -> $basketGatePass"
    }
}
else {
    Write-Warning "Could not fetch SPY stock-state for basket gate; defaulting to false."
}

$basketGatePassShort = $false
if ($spyState -and $spyState.data) {
    $close = ToDouble $spyState.data.close
    $prevClose = ToDouble $spyState.data.prev_close
    if ($prevClose -gt 0) {
        $pctChange = (($close - $prevClose) / $prevClose) * 100.0
        $basketGatePassShort = $pctChange -lt -0.5
    }
}

# -- VIX gate: dealer delta on VIX should be negative (long) / positive (short) --

$vixGatePass = $true
$vixGatePassShort = $true
$vixGex = Invoke-UW -Path "/stock/VIX/greek-exposure"
if ($vixGex -and $vixGex.data -and $vixGex.data.Count -gt 0) {
    $vixLatest = $vixGex.data | Sort-Object date -Descending | Select-Object -First 1
    $vixNetDelta = (ToDouble $vixLatest.call_delta) + (ToDouble $vixLatest.put_delta)
    $vixGatePass = $vixNetDelta -lt 0
    $vixGatePassShort = $vixNetDelta -gt 0
    Write-Host "VIX gate: net dealer delta $vixNetDelta -> long=$vixGatePass short=$vixGatePassShort"
}
else {
    Write-Warning "Could not fetch VIX greek-exposure; defaulting both VIX gates to true."
}

# -- Per-proxy fetch & CSV write ---------------------------------------------

$csvColumns = @(
    "Date", "Grade", "DbChange", "DbSustained", "DbSustainedShort",
    "CotmpCushionPct", "CotmcCushionPct", "ZeroGex", "PlusGex", "PlusGexNext",
    "Cotmc", "NTrans", "PTrans", "RR", "RRShort", "SpikeCrash", "SpikeRally",
    "BasketGatePass", "BasketGatePassShort", "BullBearRatioOk", "BullBearRatioOkShort",
    "VixGatePass", "VixGatePassShort", "ProxySpot"
)

foreach ($ticker in $Tickers) {

    Write-Host "`n== $ticker =="

    $state = Invoke-UW -Path "/stock/$ticker/stock-state"
    if (-not $state -or -not $state.data) {
        Write-Warning "$ticker : no stock-state, skipping."
        continue
    }
    $spot = ToDouble $state.data.close

    # No date param: gex-levels is an end-of-day calc, so this returns the
    # most recently finalized session's levels (i.e. last night's close,
    # which is what feeds today's trade). Passing today's date before the
    # close returns nulls.
    $levels = Invoke-UW -Path "/stock/$ticker/gex-levels"
    if (-not $levels -or -not $levels.data) {
        Write-Warning "$ticker : no gex-levels returned, skipping."
        continue
    }
    $callWall = ToDouble $levels.data.call_wall
    $putWall = ToDouble $levels.data.put_wall
    $gammaFlip = ToDouble $levels.data.gamma_flip

    if ($callWall -le 0 -or $putWall -le 0 -or $spot -le 0 -or $callWall -le $putWall) {
        Write-Warning "$ticker : missing/invalid call_wall or put_wall, skipping."
        continue
    }

    if ($gammaFlip -le 0) {
        # No zero-gamma crossing in range (dealer gamma is one-sided all the
        # way out). Do NOT fall back to gamma_magnet here -- it can equal
        # put_wall/call_wall and collapse PTrans onto NTrans, which would
        # stop the trade out on the first tick below entry. Use the midpoint
        # of put_wall/call_wall instead: guaranteed distinct from both walls.
        $gammaFlip = ($putWall + $callWall) / 2.0
        Write-Warning "$ticker : gamma_flip null (no zero-crossing), using put/call-wall midpoint ($gammaFlip) as PTrans/ZeroGex fallback."
    }

    $gex = Invoke-UW -Path "/stock/$ticker/greek-exposure"
    $dbChange = 0.0
    $dbSustained = $false
    $dbSustainedShort = $false
    if ($gex -and $gex.data -and $gex.data.Count -ge 2) {
        $sorted = $gex.data | Sort-Object date -Descending
        $today = $sorted[0]
        $prev = $sorted[1]

        $todayCallDelta = ToDouble $today.call_delta
        $todayPutDelta = ToDouble $today.put_delta
        $prevCallDelta = ToDouble $prev.call_delta
        $prevPutDelta = ToDouble $prev.put_delta

        $todayDenom = $todayCallDelta - $todayPutDelta
        $prevDenom = $prevCallDelta - $prevPutDelta

        $todayRatio = 0.5
        if ($todayDenom -ne 0) { $todayRatio = $todayCallDelta / $todayDenom }
        $prevRatio = 0.5
        if ($prevDenom -ne 0) { $prevRatio = $prevCallDelta / $prevDenom }

        $dbChange = $todayRatio - $prevRatio
        $dbSustained = ($todayRatio -ge 0.98) -and ($prevRatio -ge 0.98)
        $dbSustainedShort = ($todayRatio -le 0.02) -and ($prevRatio -le 0.02)
    }
    else {
        Write-Warning "$ticker : not enough greek-exposure history for db_change, defaulting to 0."
    }

    $cotmpCushionPct = (($spot - $putWall) / $spot) * 100.0
    $cotmcCushionPct = (($callWall - $spot) / $spot) * 100.0

    $rr = 0.0
    if (($spot - $putWall) -gt 0) {
        $rr = ($callWall - $spot) / ($spot - $putWall)
    }
    $rrShort = 0.0
    if (($callWall - $spot) -gt 0) {
        $rrShort = ($spot - $putWall) / ($callWall - $spot)
    }

    $row = [ordered]@{
        Date                 = $TodayStr
        Grade                = $GradeOverride
        DbChange             = [math]::Round($dbChange, 4)
        DbSustained          = [int]$dbSustained
        DbSustainedShort     = [int]$dbSustainedShort
        CotmpCushionPct      = [math]::Round($cotmpCushionPct, 4)
        CotmcCushionPct      = [math]::Round($cotmcCushionPct, 4)
        ZeroGex              = $gammaFlip
        PlusGex              = $callWall
        PlusGexNext          = 0
        Cotmc                = $callWall
        NTrans               = $putWall
        PTrans               = $gammaFlip
        RR                   = [math]::Round($rr, 4)
        RRShort              = [math]::Round($rrShort, 4)
        SpikeCrash           = 0
        SpikeRally           = 0
        BasketGatePass       = [int]$basketGatePass
        BasketGatePassShort  = [int]$basketGatePassShort
        BullBearRatioOk      = [int]$BullBearOverride
        BullBearRatioOkShort = [int]$BearBullOverride
        VixGatePass          = [int]$vixGatePass
        VixGatePassShort     = [int]$vixGatePassShort
        ProxySpot            = $spot
    }

    $filePath = Join-Path $OutputDir "GEX_Levels_$ticker.csv"

    $existingRows = @()
    if (Test-Path $filePath) {
        $existingRows = Import-Csv -Path $filePath | Where-Object { $_.Date -ne $TodayStr }
    }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add(($csvColumns -join ","))
    foreach ($r in $existingRows) {
        $lines.Add((($csvColumns | ForEach-Object { $r.$_ }) -join ","))
    }
    $lines.Add((($csvColumns | ForEach-Object { $row[$_] }) -join ","))

    Set-Content -Path $filePath -Value $lines -Encoding ascii

    Write-Host "$ticker : spot=$spot call_wall=$callWall put_wall=$putWall gamma_flip=$gammaFlip db_change=$($row.DbChange) rr=$($row.RR)"
    Write-Host "  -> wrote $filePath"
}

Write-Host "`nDone. Grade is a fixed placeholder ($GradeOverride) and SpikeCrash is always 0 -- verify both manually before trusting any signal."
