/**
 * Whisper model VRAM-fit rules, derived from the transcriber's `/health`
 * response (`whisper_model_fit`). Unlike codec/container pairs, there's no
 * other field to auto-correct into compatibility here — a model that
 * doesn't fit just isn't selectable until the hardware changes.
 */

type Option = { label: string; value: string }

/** Return the set of model values that don't fit in available VRAM. */
export function unfitModels(fit: Record<string, boolean> | null | undefined): Set<string> {
  if (!fit) return new Set()
  return new Set(
    Object.entries(fit)
      .filter(([, fits]) => !fits)
      .map(([name]) => name),
  )
}

/**
 * Pick the best (most accurate) model from `options` that still fits.
 * `options` must be ordered fastest/smallest-to-most-accurate/largest.
 * Returns `current` unchanged if it already fits, or if no fit data is
 * available, or if nothing fits (shouldn't happen in practice).
 */
export function bestFittingModel(
  current: string,
  options: Option[],
  fit: Record<string, boolean> | null | undefined,
): string {
  if (!fit || fit[current] !== false) return current
  const fitting = [...options].reverse().find((o) => fit[o.value] !== false)
  return fitting?.value ?? current
}
