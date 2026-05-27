# Auto-run: QMT backtest + simulation
# Scheduled at 9:25 AM on trading days

$ErrorActionPreference = "Continue"
$logFile = Join-Path $PSScriptRoot "outputs\auto_run_$(Get-Date -Format 'yyyy-MM-dd_HHmm').log"
$py = "C:\Users\小七\Desktop\API+本地大模型\a_stock_strategy\.venv\Scripts\python.exe"
$projectDir = "C:\Users\小七\Desktop\API+本地大模型\qmt_strategy"

"" > $logFile
"=== Auto Run: $(Get-Date 'yyyy-MM-dd HH:mm:ss') ===" >> $logFile

# Check QMT connection first
"Checking QMT connection..." >> $logFile
$qmtOk = $false
for ($i = 0; $i -lt 24; $i++) {
    $result = & $py -c "from xtquant import xtdata; xtdata.connect(port=58610); print('OK')" 2>&1
    if ($result -match "OK") {
        "QMT connected after $($i*5) seconds" >> $logFile
        $qmtOk = $true
        break
    }
    Start-Sleep -Seconds 5
}

if (-not $qmtOk) {
    "QMT NOT AVAILABLE - aborting" >> $logFile
    exit 1
}

# Step 1: QMT minute backtest
"[1/2] QMT minute backtest..." >> $logFile
try {
    cd $projectDir
    & $py -c "
import sys; sys.path.insert(0, 'src')
from backtest import Backtest
from pathlib import Path
from datetime import datetime
bt = Backtest(Path('data'))
today = datetime.now().strftime('%Y-%m-%d')
# Run backtest on available data
result = bt.run('2025-06-01', today)
bt.save_result(result, Path('outputs/backtest_qmt.json'))
print(f'Backtest done: return={result.total_return}%, monthly={result.monthly_return}%, win_rate={result.win_rate}%, trades={result.total_trades}')
" 2>&1 >> $logFile
    "Backtest done" >> $logFile
} catch {
    "Backtest ERROR: $_" >> $logFile
}

# Step 2: Live simulation
"[2/2] Live simulation (no real orders)..." >> $logFile
try {
    cd $projectDir
    & $py -m src.main run 2>&1 >> $logFile
    "Simulation done" >> $logFile
} catch {
    "Simulation ERROR: $_" >> $logFile
}

"=== Auto Run Complete: $(Get-Date 'yyyy-MM-dd HH:mm:ss') ===" >> $logFile
