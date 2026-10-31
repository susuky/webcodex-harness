param([Parameter(Mandatory=$true)][string]$ResultPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FixtureDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr awareness);
  [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hwnd);
}
'@
[void][FixtureDpi]::SetProcessDpiAwarenessContext([IntPtr](-4))
$form=[Windows.Forms.Form]::new()
$form.Text='WebCodex Semantic Fixture ' + $PID
$form.Size=[Drawing.Size]::new(430,340)
$form.AutoScaleMode=[Windows.Forms.AutoScaleMode]::Dpi
$form.StartPosition=[Windows.Forms.FormStartPosition]::Manual
$form.Location=[Drawing.Point]::new(40,60)
$entry=[Windows.Forms.TextBox]::new()
$entry.Name='message'
$entry.AccessibleName='Message'
$entry.SetBounds(16,16,350,30)
$form.Controls.Add($entry)
$password=[Windows.Forms.TextBox]::new()
$password.Name='password'
$password.AccessibleName='Password'
$password.UseSystemPasswordChar=$true
$password.Text='fixture-secret-never-return'
$password.SetBounds(16,52,350,30)
$form.Controls.Add($password)
$script:count=0
$script:dpiBefore=0
$script:screenBefore=''

function SaveResult {
    $script:count++
    $row=@{count=$script:count;text=$entry.Text;dpi_before=$script:dpiBefore;
        dpi_after=[FixtureDpi]::GetDpiForWindow($form.Handle);screen_before=$script:screenBefore;
        screen_after=[Windows.Forms.Screen]::FromControl($form).DeviceName;
        screen_count=[Windows.Forms.Screen]::AllScreens.Count}
    [IO.File]::WriteAllText($ResultPath, ($row | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}

$apply=[Windows.Forms.Button]::new()
$apply.Text='Apply';$apply.Name='apply';$apply.SetBounds(16,90,160,32)
$apply.Add_Click({SaveResult})
$form.Controls.Add($apply)
$move=[Windows.Forms.Button]::new()
$move.Text='Move and resize';$move.Name='move';$move.SetBounds(16,130,200,32)
$move.Add_Click({
    $script:dpiBefore=[FixtureDpi]::GetDpiForWindow($form.Handle)
    $script:screenBefore=[Windows.Forms.Screen]::FromControl($form).DeviceName
    $screens=[Windows.Forms.Screen]::AllScreens
    $target=$screens[0]
    foreach($screen in $screens){if($screen.DeviceName -ne $script:screenBefore){$target=$screen;break}}
    $area=$target.WorkingArea
    $form.SetBounds($area.X+120,$area.Y+130,650,460)
    $form.Scale([Drawing.SizeF]::new(1.25,1.25))
})
$form.Controls.Add($move)
foreach($index in 0..1){
    $button=[Windows.Forms.Button]::new()
    $button.Text='Duplicate';$button.Name='duplicate'+$index
    $button.SetBounds(16+180*$index,190,160,32)
    $button.Add_Click({SaveResult})
    $form.Controls.Add($button)
}
$form.Add_Shown({
    [Console]::Out.WriteLine((@{pid=$PID;name=$form.Text} | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
})
[Windows.Forms.Application]::Run($form)
