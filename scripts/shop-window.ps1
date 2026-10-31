param(
  [Parameter(Mandatory=$true)][ValidateRange(1,65535)][int]$Port,
  [Parameter(Mandatory=$true)][string]$ProfilePath,
  [ValidateSet('show','hide','query')][string]$Mode = 'query'
)
$ErrorActionPreference = 'Stop'
$profileFullPath = [System.IO.Path]::GetFullPath($ProfilePath)
$portPattern = '(?:^|\s)--remote-debugging-port=' + $Port + '(?=\s|$)'
$browserProcesses = @(Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" | Where-Object {
  $_.CommandLine -match $portPattern -and $_.CommandLine -notmatch '(?:^|\s)--type=' -and $_.CommandLine.IndexOf($profileFullPath,[System.StringComparison]::OrdinalIgnoreCase) -ge 0
})
if ($browserProcesses.Count -ne 1) { throw '无法唯一定位该店铺的 Chrome 进程，未操作其他窗口。' }
$windowSource = @'
using System;
using System.Collections.Generic;
using System.Text;
using System.Runtime.InteropServices;
public class ShopWindowInfo {
  public long Handle; public bool Visible; public bool Minimized;
}
public static class ShopWindows {
  public delegate bool EnumProc(IntPtr handle, IntPtr parameter);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr parameter);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr handle, StringBuilder text, int length);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr handle);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr handle);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextLength(IntPtr handle);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr handle, int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
  public static List<ShopWindowInfo> Find(uint wanted) {
    var result = new List<ShopWindowInfo>();
    EnumWindows(delegate(IntPtr handle, IntPtr parameter) {
      uint actual; GetWindowThreadProcessId(handle, out actual);
      if(actual == wanted) {
        var name = new StringBuilder(256); GetClassName(handle,name,name.Capacity);
        if(name.ToString() == "Chrome_WidgetWin_1" && GetWindowTextLength(handle)>0) result.Add(new ShopWindowInfo { Handle=handle.ToInt64(), Visible=IsWindowVisible(handle), Minimized=IsIconic(handle) });
      }
      return true;
    },IntPtr.Zero);
    return result;
  }
}
'@
Add-Type -TypeDefinition $windowSource
$chromeProcessId = [uint32]$browserProcesses[0].ProcessId
$windows = [ShopWindows]::Find($chromeProcessId)
if ($windows.Count -eq 0) { throw '该店铺没有可控制的 Chrome 窗口。' }
if ($Mode -ne 'query') {
  foreach ($window in $windows) {
    $handle = [IntPtr]$window.Handle
    $command = if ($Mode -eq 'hide') { 0 } else { 9 }
    [void][ShopWindows]::ShowWindowAsync($handle,$command)
    if ($Mode -eq 'show') { [void][ShopWindows]::SetForegroundWindow($handle) }
  }
  Start-Sleep -Milliseconds 180
  $windows = [ShopWindows]::Find($chromeProcessId)
}
$visible = @($windows | Where-Object { $_.Visible }).Count -gt 0
if ($Mode -eq 'hide' -and $visible) { throw '窗口未能隐藏，请重试。' }
if ($Mode -eq 'show' -and -not $visible) { throw '窗口未能显示，请重试。' }
@{ visible=$visible; windowCount=$windows.Count } | ConvertTo-Json -Compress
