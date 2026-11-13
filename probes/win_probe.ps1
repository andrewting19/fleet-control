$ErrorActionPreference = 'SilentlyContinue'
$t0 = Get-Date
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$cores = [int]$cs.NumberOfLogicalProcessors
# Formatted perf counters are rates over the last WMI sample, so read once, wait, read again.
$null = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process
$null = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor
$null = Get-CimInstance Win32_PerfFormattedData_PerfDisk_LogicalDisk
Start-Sleep -Milliseconds 1000
$cpu = [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
$queue = [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_System).ProcessorQueueLength
$mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$pf = @(Get-CimInstance Win32_PageFileUsage)
$swapTotal = [double](($pf | Measure-Object AllocatedBaseSize -Sum).Sum) * 1MB
$swapUsed = [double](($pf | Measure-Object CurrentUsage -Sum).Sum) * 1MB
# "Pages Input/sec" also counts reads of mapped files (databases, programs), so it is not a swap rate.
# Real paging must also show as disk traffic on the drive that holds the page file: take the smaller of the two.
$pfDrives = @($pf | ForEach-Object { $_.Name.Substring(0, 2).ToUpper() } | Select-Object -Unique)
$ld = @(Get-CimInstance Win32_PerfFormattedData_PerfDisk_LogicalDisk | Where-Object { $pfDrives -contains $_.Name.ToUpper() })
$pfRead = [double](($ld | Measure-Object DiskReadBytesPersec -Sum).Sum)
$pfWrite = [double](($ld | Measure-Object DiskWriteBytesPersec -Sum).Sum)
$swapIn = [Math]::Min([double]$mem.PagesInputPersec * 4096, $pfRead)
$swapOut = [Math]::Min([double]$mem.PagesOutputPersec * 4096, $pfWrite)
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
# Jobs started with 'job run' (jobs/job.py): running, queued, and ended in the last 6 hours.
$jobs = @()
$jd = Join-Path $env:USERPROFILE '.agentjobs'
if (Test-Path $jd) {
  $cut = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 21600
  foreach ($dir in (Get-ChildItem $jd -Directory)) {
    try {
      $m = Get-Content (Join-Path $dir.FullName 'meta.json') -Raw | ConvertFrom-Json
      if (-not (@('done', 'failed', 'killed', 'stopped', 'lost') -contains $m.state) -or $m.ended -ge $cut) {
        $jobs += @{ id = $m.id; name = $m.name; project = $m.project; owner = $m.owner; state = $m.state; created = $m.created; started = $m.started; ended = $m.ended; exit = $m.exit; why = $m.why; peak_gb = $m.peak_gb; mem_gb = $m.mem_gb; peak_bytes = $m.peak_bytes; mem_now = $m.mem_now; cmd = ($m.cmd -join ' ') }
      }
    } catch {}
  }
}
$out = @{
  ok = $true; hostname = $env:COMPUTERNAME; os = 'Windows'; cores = $cores; load = @($load, $load, $load); cpu_util = $cpu
  mem_total = [double]$cs.TotalPhysicalMemory; mem_avail = [double]$mem.AvailableBytes
  swap_total = $swapTotal; swap_used = $swapUsed
  page_size = 1; swap_rate_1s = @{ in = $swapIn; out = $swapOut }
  disks = $disks; gpus = $gpus; jobs = $jobs; procs = $procs; mem_top = $mtop
  uptime = [int]($now - $os.LastBootUpTime).TotalSeconds; probe_ms = [int]($now - $t0).TotalMilliseconds
}
$out | ConvertTo-Json -Compress -Depth 5
