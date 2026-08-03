# Remove the memory-gateway Windows service. Run as Administrator.
$nssm = "C:\Users\spari\Tools\nssm.exe"
& $nssm stop memory-gateway
& $nssm remove memory-gateway confirm
