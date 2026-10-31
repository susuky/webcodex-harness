$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:dispatched = $false

function BoundText($value, [int]$limit = 256) {
    $value = [string]$value
    if ($value.Length -gt $limit) { return $value.Substring(0, $limit) }
    return $value
}

function Fail([string]$code, [string]$message, $candidates = @()) {
    $script:failure = @{ok=$false; outcome='not_started'; error_code=$code; error=$message; candidates=@($candidates)}
    throw $message
}

function Matches($row, $selector) {
    foreach ($property in $selector.PSObject.Properties) {
        if ($property.Name -eq 'pid') {
            if ($row[$property.Name] -ne [int]$property.Value) { return $false }
        } elseif (-not [string]::Equals([string]$row[$property.Name], [string]$property.Value, [StringComparison]::Ordinal)) {
            return $false
        }
    }
    return $true
}

function Pattern($element, $patternId) {
    $value = $null
    if ($element.TryGetCurrentPattern($patternId, [ref]$value)) { return $value }
    return $null
}

function Row($element, [bool]$isWindow = $false) {
    $current = $element.Current
    $protected = [bool]$current.IsPassword -or [WebCodexSemanticNative]::IsPasswordWindow([IntPtr]$current.NativeWindowHandle)
    $role = $current.ControlType.ProgrammaticName.Replace('ControlType.', '').ToLowerInvariant()
    $invoke = Pattern $element ([System.Windows.Automation.InvokePattern]::Pattern)
    $value = $null
    if (-not $protected) { $value = Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern) }
    $row = @{
        name = $(if ($protected) { '[protected]' } else { BoundText $current.Name })
        role = $role
        automation_id = BoundText $current.AutomationId
        element_id = 'uia:' + (($element.GetRuntimeId()) -join '.')
        pid = $current.ProcessId
        enabled = [bool]$current.IsEnabled
        focused = [bool]$current.HasKeyboardFocus
        protected = $protected
        can_invoke = [bool]($current.IsEnabled -and (-not $protected) -and ($null -ne $invoke))
        can_focus = [bool]($current.IsEnabled -and $current.IsKeyboardFocusable)
        can_set_text = [bool]($current.IsEnabled -and (-not $protected) -and ($null -ne $value) -and (-not $value.Current.IsReadOnly))
    }
    if ($isWindow) {
        $ticks = (Get-Process -Id $current.ProcessId -ErrorAction Stop).StartTime.ToUniversalTime().Ticks
        $row.window_id = 'uia:' + $current.NativeWindowHandle + ':' + $current.ProcessId + ':' + $ticks
        $row.Remove('element_id')
    }
    return $row
}

function ReadWindows($walker, $root, [int]$limit) {
    $windows = [System.Collections.Generic.List[object]]::new()
    $truncated = $false
    $window = $walker.GetFirstChild($root)
    while ($null -ne $window) {
        if ($windows.Count -ge $limit) { $truncated=$true; break }
        try {
            $row = Row $window $true
            $windows.Add(@{native=$window; row=$row})
        } catch {
            # A failed provider read can hide another window with the same name/PID.
            $truncated = $true
        }
        $window = $walker.GetNextSibling($window)
    }
    return @{items=$windows; truncated=$truncated}
}

function ResolveWindow($windows, $selector, [bool]$truncated) {
    $selected = @($windows | Where-Object { Matches $_.row $selector })
    if ($selected.Count -eq 0) { Fail 'window_not_found' 'No window matches the selector' }
    if ($selected.Count -ne 1 -or ($truncated -and -not $selector.window_id)) {
        Fail 'ambiguous_window' 'Select one window by window_id' @($selected | Select-Object -First 20 | ForEach-Object {$_.row})
    }
    return $selected[0]
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName UIAutomationClientsideProviders
    $references = @([System.Windows.Automation.AutomationElement].Assembly.Location,
                    [System.Windows.Automation.ControlType].Assembly.Location,
                    [UIAutomationClientsideProviders.UIAutomationClientSideProviders].Assembly.Location)
    Add-Type -ReferencedAssemblies $references -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WebCodexSemanticNative {
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    public static void InitializeUIA() {
        var providers = UIAutomationClientsideProviders.UIAutomationClientSideProviders.ClientSideProviderDescriptionTable;
        if (providers == null || providers.Length == 0)
            throw new InvalidOperationException("The Windows standard-control provider table is unavailable");
        // Keep a typed, non-inlined frame: direct PowerShell 5.1 registration failed with
        // NullReferenceException during native acceptance; this bootstrap passed on two DPI scales.
        System.Windows.Automation.ClientSettings.RegisterClientSideProviders(providers);
    }
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool GetUserObjectInformation(IntPtr handle, int index, System.Text.StringBuilder output, int length, out int needed);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, System.Text.StringBuilder output, int length);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] private static extern int GetWindowLong(IntPtr hwnd, int index);
    public static bool IsPasswordWindow(IntPtr hwnd) {
        if (hwnd == IntPtr.Zero) return false;
        var name = new System.Text.StringBuilder(256);
        if (GetClassName(hwnd, name, 256) == 0) return false;
        string value = name.ToString();
        bool edit = value.Equals("Edit", StringComparison.OrdinalIgnoreCase)
            || value.StartsWith("WindowsForms10.EDIT.", StringComparison.OrdinalIgnoreCase)
            || value.IndexOf("richedit", StringComparison.OrdinalIgnoreCase) >= 0;
        return edit && (GetWindowLong(hwnd, -16) & 0x20) != 0;
    }
}
'@
    [WebCodexSemanticNative]::InitializeUIA()
    # Semantic actions use UIA patterns; awareness keeps future geometry metadata physical.
    [void][WebCodexSemanticNative]::SetProcessDpiAwarenessContext([IntPtr](-4))
    if ((Get-Process -Id $PID).SessionId -eq 0) {
        Fail 'desktop_unavailable' 'Start the Windows Runner in the signed-in desktop session'
    }
    $desktop = [WebCodexSemanticNative]::OpenInputDesktop(0, $false, 1)
    if ($desktop -eq [IntPtr]::Zero) { Fail 'desktop_unavailable' 'The Windows input desktop is locked or unavailable' }
    try {
        $desktopName = [System.Text.StringBuilder]::new(256)
        $needed = 0
        $queried = [WebCodexSemanticNative]::GetUserObjectInformation($desktop, 2, $desktopName, 512, [ref]$needed)
        if (-not $queried -or $desktopName.ToString() -ne 'Default') {
            Fail 'desktop_unavailable' 'The Windows input desktop is locked or protected'
        }
    } finally { [void][WebCodexSemanticNative]::CloseDesktop($desktop) }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windowLimit = $(if ($request.action -eq 'windows') { [Math]::Min([int]$request.max_nodes, 200) } else { 200 })
    $observed = ReadWindows $walker $root $windowLimit
    $windows = $observed.items
    $windowsTruncated = $observed.truncated
    if ($request.action -eq 'windows') {
        $result = @{ok=$true; backend='uia'; windows=@($windows | ForEach-Object {$_.row}); truncated=$windowsTruncated}
    } else {
        $selectedWindow = ResolveWindow $windows $request.window $windowsTruncated
        $windowRow = $selectedWindow.row
        $queue = [System.Collections.Generic.Queue[object]]::new()
        $queue.Enqueue(@{native=$selectedWindow.native; depth=0; parent=$null})
        $nodes = [System.Collections.Generic.List[object]]::new()
        $truncated = $false
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($queue.Count -gt 0) {
            if ($nodes.Count -ge [int]$request.max_nodes -or $timer.Elapsed.TotalSeconds -gt 10) {
                $truncated=$true; break
            }
            $entry = $queue.Dequeue()
            try {
                $row = Row $entry.native
                $row.depth = $entry.depth
                $row.parent_id = $entry.parent
                $nodes.Add(@{native=$entry.native; row=$row})
                if ($row.protected) { continue }
                $child = $walker.GetFirstChild($entry.native)
                if ($entry.depth -ge [int]$request.max_depth) {
                    if ($null -ne $child) { $truncated=$true }
                    continue
                }
                while ($null -ne $child) {
                    if (($queue.Count + $nodes.Count) -ge [int]$request.max_nodes) { $truncated=$true; break }
                    $queue.Enqueue(@{native=$child; depth=$entry.depth+1; parent=$row.element_id})
                    $child = $walker.GetNextSibling($child)
                }
            } catch { $truncated=$true }
        }
        if ($request.action -eq 'tree') {
            $result = @{ok=$true; backend='uia'; window=$windowRow; elements=@($nodes | ForEach-Object {$_.row}); truncated=$truncated}
        } else {
            $selected = @($nodes | Where-Object { Matches $_.row $request.selector })
            if ($request.action -eq 'find') {
                $result = @{ok=$true; backend='uia'; window=$windowRow; elements=@($selected | ForEach-Object {$_.row}); truncated=$truncated}
            } else {
                $candidates = @($selected | Select-Object -First 20 | ForEach-Object {$_.row})
                if ($truncated) { Fail 'search_incomplete' 'Increase tree limits to resolve the control uniquely' $candidates }
                if ($selected.Count -eq 0) { Fail 'element_not_found' 'No control matches the selector' }
                if ($selected.Count -ne 1) { Fail 'ambiguous_element' 'Add role or automation_id to select one control' $candidates }
                $element = $selected[0].native
                $current = Row $element
                if (-not (Matches $current $request.selector) -or $current.element_id -ne $selected[0].row.element_id) {
                    Fail 'stale_element' 'Control changed; observe it again'
                }
                if (-not $current['can_' + $request.action]) { Fail 'unsupported_action' ('This control does not support ' + $request.action) @($current) }
                switch ($request.action) {
                    'invoke' {
                        $pattern = Pattern $element ([System.Windows.Automation.InvokePattern]::Pattern)
                        $script:dispatched = $true
                        $pattern.Invoke()
                    }
                    'focus' {
                        $script:dispatched = $true
                        $element.SetFocus()
                    }
                    'set_text' {
                        $pattern = Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern)
                        $script:dispatched = $true
                        $pattern.SetValue([string]$request.text)
                    }
                    default { Fail 'invalid_action' 'Unknown semantic desktop action' }
                }
                $result = @{ok=$true; backend='uia'; action=$request.action; input_sent=$true; window=$windowRow; element=$current}
            }
        }
    }
} catch {
    if ($script:dispatched) {
        $result = @{ok=$false; outcome='unknown'; error='Native action response was lost; inspect the control before retrying'}
    } elseif ($null -ne $script:failure) { $result = $script:failure }
    else {
        $result = @{ok=$false; outcome='not_started'; error_code='provider_unavailable'; error='Windows UI Automation is unavailable; check the signed-in desktop and PowerShell dependencies'}
    }
}
[Console]::Out.Write(($result | ConvertTo-Json -Depth 12 -Compress))
