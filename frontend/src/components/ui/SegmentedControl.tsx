interface SegmentedControlProps {
  options: { label: string; value: string }[]
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  /** Values that are incompatible with the current state of another field.
   *  They remain clickable (auto-correction fixes the other field) but are
   *  visually dimmed so the user knows the pairing will trigger a change. */
  incompatible?: Set<string>
  /** Values that can't be selected at all right now (e.g. don't fit in
   *  available VRAM), mapped to a tooltip explaining why. Unlike
   *  `incompatible`, there's no other field whose change would fix it, so
   *  these are hard-disabled rather than dimmed. */
  unavailable?: Map<string, string>
  color?: 'blue' | 'indigo' | 'rose' | 'emerald' | 'cyan'
}

const activeClasses = {
  blue: 'bg-[var(--accent)] text-white shadow-[0_2px_10px_var(--accent-glow)]',
  indigo: 'bg-[var(--accent-2)] text-white shadow-[0_2px_10px_var(--accent-2-glow)]',
  rose: 'bg-[var(--accent-3)] text-white shadow-[0_2px_10px_var(--accent-3-glow)]',
  emerald: 'bg-[var(--accent-4)] text-white shadow-[0_2px_10px_var(--accent-4-glow)]',
  cyan: 'bg-[var(--accent-6)] text-white shadow-[0_2px_10px_var(--accent-6-glow)]',
}

export default function SegmentedControl({
  options,
  value,
  onChange,
  disabled,
  incompatible,
  unavailable,
  color = 'blue',
}: SegmentedControlProps) {
  return (
    <div
      className="inline-flex gap-[2px] rounded-[10px] border border-[var(--border)] bg-[var(--bg-input)] p-[3px]"
      role="radiogroup"
    >
      {options.map((opt) => {
        const isActive = value === opt.value
        const isUnavailable = unavailable?.has(opt.value)
        const isIncompat = !isActive && !isUnavailable && incompatible?.has(opt.value)
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={isActive}
            disabled={disabled || isUnavailable}
            title={isUnavailable ? unavailable?.get(opt.value) : undefined}
            onClick={() => onChange(opt.value)}
            className={`cursor-pointer rounded-lg border-none px-4 py-[0.45rem] font-[Geist,sans-serif] text-[0.78rem] font-medium transition-all duration-250 ${
              isUnavailable
                ? `${isActive ? activeClasses[color] : 'bg-transparent text-[var(--text-tertiary)]'} cursor-not-allowed opacity-40`
                : isActive
                  ? activeClasses[color]
                  : isIncompat
                    ? 'bg-transparent text-[var(--text-tertiary)] opacity-50 line-through hover:opacity-70'
                    : 'bg-transparent text-[var(--text-tertiary)] hover:bg-[rgba(255,255,255,0.025)] hover:text-[var(--text-secondary)]'
            } ${disabled ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
