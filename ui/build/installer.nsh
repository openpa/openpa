; Custom NSIS hooks injected into electron-builder's generated installer/
; uninstaller. The macros below are appended to the default templates by
; electron-builder when this file is at ui/build/installer.nsh.

!macro customInstall
  DetailPrint "Adding Windows Defender Exclusion..."

  ; Execute PowerShell command to add the installation directory ($INSTDIR) to exclusions
  ; nsExec::ExecToLog runs the command and logs output, hiding the console window
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend

!macro customUnInstall
  ; ── OpenPA data cleanup dialog ─────────────────────────────────────────
  ;
  ; The default NSIS uninstaller only removes the Electron app files. We
  ; also need to handle the OpenPA data dirs (~/.openpa and the Install
  ; Dir at %LOCALAPPDATA%\OpenPA). Ask the user explicitly, then run
  ; uninstall.ps1 — same script the in-app Settings → Updates flow runs,
  ; so behavior is identical regardless of how the user triggered uninstall.
  ;
  ; /SD IDNO means "if silent uninstall (e.g. msiexec /qn or
  ;             ``Uninstall.exe /S``), default to Keep — never destroy data
  ;             without an explicit click.
  MessageBox MB_YESNOCANCEL|MB_ICONQUESTION \
    "Remove all OpenPA data?$\r$\n$\r$\nYes  = remove everything (System Dir, Install Dir, Docker volumes)$\r$\nNo   = keep your data (.env, storage, tokens preserved)$\r$\nCancel = abort uninstall" \
    /SD IDNO IDYES OpenpaPurge IDNO OpenpaKeep
    ; IDCANCEL falls through to here. NSIS's `Abort` inside customUnInstall
    ; cancels the rest of the uninstall.
    Abort

  OpenpaPurge:
    DetailPrint "Running OpenPA uninstall (purge: removing all data)..."
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall.ps1" -Purge'
    Goto OpenpaDone

  OpenpaKeep:
    DetailPrint "Running OpenPA uninstall (keep: preserving .env / storage / tokens)..."
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall.ps1" -Keep'
    Goto OpenpaDone

  OpenpaDone:
    DetailPrint "Removing Windows Defender Exclusion..."
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend
