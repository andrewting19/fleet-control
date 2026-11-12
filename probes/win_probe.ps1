$ErrorActionPreference = 'SilentlyContinue'
$t0 = Get-Date
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$cores = [int]$cs.NumberOfLogicalProcessors
# Formatted perf counters are rates over the last WMI sample, so read once, wait, read again.
$null = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process
$null = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor
Start-Sleep -Milliseconds 1000
$cpu = [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
$queue = [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_System).ProcessorQueueLength
$mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$pf = @(Get-CimInstance Win32_PageFileUsage)
$swapTotal = [double](($pf | Measure-Object AllocatedBaseSize -Sum).Sum) * 1MB
$swapUsed = [double](($pf | Measure-Object CurrentUsage -Sum).Sum) * 1MB
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Where-Object { $_.Size -gt 0 } | ForEach-Object { @{ mount = $_.DeviceID; total = [double]$_.Size; used = [double]($_.Size - $_.FreeSpace) } })
$gpus = @()
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  foreach ($ln in (nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits)) {
    $f = @($ln.Split(',') | ForEach-Object { $_.Trim() })
    if ($f.Count -eq 4) { $gpus += @{ name = $f[0]; util = [double]$f[1]; mem_used = [double]$f[2] * 1MB; mem_total = [double]$f[3] * 1MB } }
  }
}
$pp = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $_.Name -ne '_Total' -and $_.Name -ne 'Idle' })
$wp = @{}
Get-CimInstance Win32_Process | ForEach-Object { $wp[[int]$_.ProcessId] = $_ }
$now = Get-Date
function Info($x) {
  $p = $wp[[int]$x.IDProcess]
  $cmd = $x.Name
  if ($p -and $p.CommandLine) { $cmd = $p.CommandLine; if ($cmd.Length -gt 300) { $cmd = $cmd.Substring(0, 300) } }
  $age = $null
  if ($p -and $p.CreationDate) { $age = [int]($now - $p.CreationDate).TotalSeconds }
  @{ pid = [int]$x.IDProcess; cpu = [double]$x.PercentProcessorTime; rss = [double]$x.WorkingSetPrivate; swap = 0; comm = ($x.Name -replace '#\d+$', ''); cmd = $cmd; cwd = ''; user = ''; age = $age }
}
$procs = @($pp | Sort-Object PercentProcessorTime -Descending | Select-Object -First 25 | Where-Object { $_.PercentProcessorTime -gt 0 } | ForEach-Object { Info $_ })
$mtop = @($pp | Sort-Object WorkingSetPrivate -Descending | Select-Object -First 8 | ForEach-Object { Info $_ })
$load = [Math]::Round($cpu / 100 * $cores + $queue, 2)
$out = @{
  ok = $true; hostname = $env:COMPUTERNAME; os = 'Windows'; cores = $cores; load = @($load, $load, $load); cpu_util = $cpu
  mem_total = [double]$cs.TotalPhysicalMemory; mem_avail = [double]$mem.AvailableBytes
  swap_total = $swapTotal; swap_used = $swapUsed
  swap_rate_1s = @{ in = [double]$mem.PagesInputPersec; out = [double]$mem.PagesOutputPersec }
  disks = $disks; gpus = $gpus; procs = $procs; mem_top = $mtop
  uptime = [int]($now - $os.LastBootUpTime).TotalSeconds; probe_ms = [int]($now - $t0).TotalMilliseconds
}
$out | ConvertTo-Json -Compress -Depth 5
