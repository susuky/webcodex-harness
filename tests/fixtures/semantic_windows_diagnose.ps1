$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$results=[Collections.Generic.List[object]]::new()
foreach($name in @('UIAutomationClient','UIAutomationTypes','UIAutomationClientsideProviders')) {
    try {
        Add-Type -AssemblyName $name
        $assembly=[AppDomain]::CurrentDomain.GetAssemblies() | Where-Object {$_.GetName().Name -eq $name} | Select-Object -First 1
        $results.Add(@{step='load';name=$name;ok=$true;assembly=$assembly.FullName;location=$assembly.Location})
    } catch {
        $results.Add(@{step='load';name=$name;ok=$false;type=$_.Exception.GetType().FullName;message=$_.Exception.Message})
    }
}
try {
    $type=[UIAutomationClientsideProviders.UIAutomationClientSideProviders]
    $name=$type.Assembly.GetName()
    $providers=[UIAutomationClientsideProviders.UIAutomationClientSideProviders]::ClientSideProviderDescriptionTable
    $results.Add(@{step='provider_table';ok=($null -ne $providers);count=$providers.Length;type=$providers.GetType().FullName})
    $references=@([System.Windows.Automation.AutomationElement].Assembly.Location,
                  [System.Windows.Automation.ControlType].Assembly.Location,
                  [UIAutomationClientsideProviders.UIAutomationClientSideProviders].Assembly.Location)
    Add-Type -ReferencedAssemblies $references -TypeDefinition @'
public static class WebCodexSemanticInitProbe {
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    public static void Initialize() {
        System.Windows.Automation.ClientSettings.RegisterClientSideProviders(
            UIAutomationClientsideProviders.UIAutomationClientSideProviders.ClientSideProviderDescriptionTable);
    }
}
'@
    [WebCodexSemanticInitProbe]::Initialize()
    $results.Add(@{step='register_table_csharp';ok=$true;assembly=$name.FullName})
} catch {
    $results.Add(@{step='register';ok=$false;type=$_.Exception.GetType().FullName;message=$_.Exception.Message;
        inner=$(if($_.Exception.InnerException){$_.Exception.InnerException.GetType().FullName + ': ' + $_.Exception.InnerException.Message}else{''});
        stack=$(if($_.Exception.InnerException){$_.Exception.InnerException.StackTrace}else{$_.Exception.StackTrace})})
}
try {
    $results.Add(@{step='environment';powershell=$PSVersionTable.PSVersion.ToString();clr=[Environment]::Version.ToString();
        bitness=[IntPtr]::Size*8;apartment=[Threading.Thread]::CurrentThread.GetApartmentState().ToString()})
} catch {}
[Console]::Out.Write(($results | ConvertTo-Json -Depth 6 -Compress))
