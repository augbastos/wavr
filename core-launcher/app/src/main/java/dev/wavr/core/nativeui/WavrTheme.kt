package dev.wavr.core.nativeui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

/**
 * Material 3 components (text fields, buttons, switches) drawn in Wavr's own
 * colours. Without it they fall back to Material's LIGHT scheme -- dark text on
 * Wavr's dark surface, which failed the AA contrast PRODUCT.md requires (a typed
 * Core address was nearly invisible). Built only from the generated tokens.
 */
private val scheme = darkColorScheme(
    primary = WavrTokens.accent, onPrimary = WavrTokens.background,
    secondary = WavrTokens.info, onSecondary = WavrTokens.background,
    background = WavrTokens.background, onBackground = WavrTokens.text,
    surface = WavrTokens.surface, onSurface = WavrTokens.text,
    surfaceVariant = WavrTokens.elevated, onSurfaceVariant = WavrTokens.textDim,
    surfaceContainer = WavrTokens.surface, surfaceContainerHigh = WavrTokens.elevated,
    outline = WavrTokens.textFaint, outlineVariant = WavrTokens.elevated2,
    error = WavrTokens.danger, onError = WavrTokens.background
)

@Composable
internal fun WavrTheme(content: @Composable () -> Unit) = MaterialTheme(colorScheme = scheme, content = content)
