; Custom NSIS hooks injected into electron-builder's generated installer/
; uninstaller. The macros below are appended to the default templates by
; electron-builder when this file is at ui/build/installer.nsh.

!macro customInstall
  DetailPrint "Adding Windows Defender Exclusion..."

  ; Execute PowerShell command to add the installation directory ($INSTDIR) to exclusions
  ; nsExec::ExecToLog runs the command and logs output, hiding the console window
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend

!macro customUnInit
  ; ── OpenPA data cleanup dialog (runs BEFORE file deletion) ─────────────
  ;
  ; This macro fires early in the Uninstall section, while $INSTDIR\resources\
  ; is still on disk. customUnInstall (below) runs AFTER electron-builder
  ; removes the app files — too late to invoke our bundled uninstall.ps1.
  ;
  ; The default NSIS uninstaller only removes the Electron app files. We
  ; also need to handle the OpenPA data dirs (~/.openpa System Dir and
  ; %LOCALAPPDATA%\OpenPA Install Dir, including Docker named volumes).
  ; Ask the user explicitly, then run uninstall.ps1 — same script the
  ; in-app Settings → Updates flow runs, so behavior is identical
  ; regardless of how the user triggered uninstall.
  ;
  ; /SD IDNO = "if silent uninstall (msiexec /qn, Uninstall.exe /S),
  ;             default to Keep" — never destroy data without an
  ;             explicit click.
  MessageBox MB_YESNOCANCEL|MB_ICONQUESTION \
    "Remove all OpenPA data?$\r$\n$\r$\nYes  = remove everything (System Dir at ~/.openpa, Install Dir, Docker volumes)$\r$\nNo   = keep your data (.env, storage, tokens preserved)$\r$\nCancel = abort uninstall" \
    /SD IDNO \
    IDYES OpenpaPurge \
    IDNO OpenpaKeep
  ; IDCANCEL (or window-close) falls through to here — abort the uninstall.
  Abort

  OpenpaPurge:
    DetailPrint "Running OpenPA uninstall (purge: removing all data)..."
    IfFileExists "$INSTDIR\resources\uninstall.ps1" RunPurge ScriptMissing
  RunPurge:
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall.ps1" -Purge'
    Goto OpenpaDone

  OpenpaKeep:
    DetailPrint "Running OpenPA uninstall (keep: preserving .env / storage / tokens)..."
    IfFileExists "$INSTDIR\resources\uninstall.ps1" RunKeep ScriptMissing
  RunKeep:
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\uninstall.ps1" -Keep'
    Goto OpenpaDone

  ScriptMissing:
    ; Should never hit — uninstall.ps1 ships with the installer via
    ; electron-builder.json5's extraResources. If it does, the user has
    ; data on disk we can't clean up automatically.
    MessageBox MB_OK|MB_ICONEXCLAMATION \
      "uninstall.ps1 was not found at $INSTDIR\resources\uninstall.ps1.$\r$\n$\r$\nOpenPA's data directories (~/.openpa and %LOCALAPPDATA%\OpenPA) and any Docker containers/volumes will need to be removed manually.$\r$\n$\r$\nSee the OpenPA docs for the manual cleanup commands."
    Goto OpenpaDone

  OpenpaDone:
!macroend

!macro customUnInstall
  ; Defender exclusion cleanup runs AFTER file deletion — fine, this
  ; reaches into an external (Windows Defender) state, not $INSTDIR.
  DetailPrint "Removing Windows Defender Exclusion..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend
