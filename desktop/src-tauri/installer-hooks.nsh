; Wavr — NSIS installer hooks.
;
; Tauri's NSIS template creates one Start-menu shortcut: the app. Nothing else.
; That is defensible until somebody wants to remove Wavr and reaches for the
; Start menu, which is where people look: typing "uninstall" finds nothing,
; because there is nothing named that to find. Right-clicking the app tile and
; choosing Uninstall does work — the registry entry is correct — but it is not
; where the first user looked, and "it works if you already know where" is the
; shape of most of the defects this product has had.
;
; So: one more shortcut, named for the word somebody types.
;
; Removed again on uninstall. An orphan shortcut pointing at a deleted
; uninstaller is worse than no shortcut: it is a dead entry that survives the
; thing it was for.

!macro NSIS_HOOK_POSTINSTALL
  CreateShortcut "$SMPROGRAMS\Uninstall Wavr.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  Delete "$SMPROGRAMS\Uninstall Wavr.lnk"
!macroend
