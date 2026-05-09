!macro customInstall
  DetailPrint "Adding Windows Defender Exclusion..."
  
  ; Execute PowerShell command to add the installation directory ($INSTDIR) to exclusions
  ; nsExec::ExecToLog runs the command and logs output, hiding the console window
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend

!macro customUnInstall
  DetailPrint "Removing Windows Defender Exclusion..."
  
  ; Clean up the exclusion on uninstall
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend
