import { type UIEvent, useRef, useState } from 'react'
import {
  deleteEncoderPreset,
  importEncoderPresets,
  previewEncoderPresets,
  saveEncoderPreset,
} from '@/lib/api'
import EncoderSelect from '@/components/encoder/EncoderSelect'
import IconButton from '@/components/ui/IconButton'
import { PencilIcon, SaveIcon, TrashIcon } from '@/components/ui/icons'
import type {
  EncoderHealth,
  EncoderPreset,
  EncoderPresetImportResult,
  EncoderPresetPreview,
} from '@/types'

type GuidedPresetFields = {
  name: string
  encoder: string
  videoPreset: string
  fileFormat: string
  qualityType: string
  quality: string
}

type Draft = {
  originalName: string | null
  body: Record<string, unknown>
  rawText: string
  rawError: string | null
}

type ImportSummary = EncoderPresetImportResult & {
  unsupported: EncoderPresetPreview[]
}

export type PresetEditorProps = {
  presets: EncoderPreset[]
  health?: EncoderHealth | null
  onSaved: (preset?: EncoderPreset) => void
  onDeleted: (name?: string) => void
  onError: (message: string) => void
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function cloneBody(body: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(body)) as Record<string, unknown>
}

function asString(value: unknown): string {
  return value == null ? '' : String(value)
}

function patchGuidedLeaf(
  body: Record<string, unknown>,
  form: GuidedPresetFields,
): Record<string, unknown> {
  return {
    ...body,
    PresetName: form.name,
    VideoEncoder: form.encoder,
    VideoPreset: form.videoPreset,
    FileFormat: form.fileFormat,
    VideoQualityType: preserveNumericType(form.qualityType, body.VideoQualityType),
    VideoQualitySlider: preserveNumericType(form.quality, body.VideoQualitySlider),
  }
}

function preserveNumericType(value: string, previous: unknown): string | number {
  if (typeof previous !== 'number') return value
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : value
}

function guidedFields(body: Record<string, unknown>): GuidedPresetFields {
  return {
    name: asString(body.PresetName),
    encoder: asString(body.VideoEncoder),
    videoPreset: asString(body.VideoPreset),
    fileFormat: asString(body.FileFormat),
    qualityType: asString(body.VideoQualityType),
    quality: asString(body.VideoQualitySlider),
  }
}

function formatBody(body: Record<string, unknown>): string {
  return JSON.stringify(body, null, 2)
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;',
    }
    return entities[character]!
  })
}

function highlightJson(value: string): string {
  return escapeHtml(value).replace(
    /(&quot;.*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?/g,
    (token, quoted: string, colon: string | undefined) => {
      if (quoted) {
        return `<span class="text-cyan-300">${quoted}</span>${colon ?? ''}`
      }
      if (token === 'true' || token === 'false' || token === 'null') {
        return `<span class="text-amber-300">${token}</span>`
      }
      return `<span class="text-teal-300">${token}</span>`
    },
  )
}

function completeLeafError(body: Record<string, unknown>): string | null {
  for (const field of ['PresetName', 'VideoEncoder', 'VideoPreset', 'FileFormat']) {
    if (typeof body[field] !== 'string' || !String(body[field]).trim()) {
      return `Preset JSON must include a non-empty ${field}.`
    }
  }
  return null
}

const COMMON_SPEEDS = [
  'ultrafast',
  'superfast',
  'veryfast',
  'faster',
  'fast',
  'medium',
  'slow',
  'slower',
  'veryslow',
  'placebo',
  'speed',
  'balanced',
  'quality',
]

const COMMON_QUALITY_VALUES = ['16', '18', '20', '22', '24', '26', '28', '30', '32']

function PresetEditorTabs({
  view,
  onSelectView,
}: {
  view: 'guided' | 'raw'
  onSelectView: (view: 'guided' | 'raw') => void
}) {
  return (
    <div className="flex gap-2" aria-label="Preset editor mode">
      <button
        type="button"
        aria-pressed={view === 'guided'}
        onClick={() => onSelectView('guided')}
        className={`rounded-lg border px-3 py-1.5 text-[0.75rem] font-medium transition ${
          view === 'guided'
            ? 'border-teal-400/30 bg-teal-400/15 text-teal-300'
            : 'border-white/8 bg-white/4 text-[var(--text-secondary)] hover:text-white'
        }`}
      >
        Guided fields
      </button>
      <button
        type="button"
        aria-pressed={view === 'raw'}
        onClick={() => onSelectView('raw')}
        className={`rounded-lg border px-3 py-1.5 text-[0.75rem] font-medium transition ${
          view === 'raw'
            ? 'border-teal-400/30 bg-teal-400/15 text-teal-300'
            : 'border-white/8 bg-white/4 text-[var(--text-secondary)] hover:text-white'
        }`}
      >
        Raw JSON
      </button>
    </div>
  )
}

export default function PresetEditor({
  presets,
  health,
  onSaved,
  onDeleted,
  onError,
}: PresetEditorProps) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [view, setView] = useState<'guided' | 'raw'>('guided')
  const [saving, setSaving] = useState(false)
  const [importDocument, setImportDocument] = useState<Record<string, unknown> | null>(null)
  const [importCandidates, setImportCandidates] = useState<EncoderPresetPreview[] | null>(null)
  const [selectedImportNames, setSelectedImportNames] = useState<string[]>([])
  const [importing, setImporting] = useState(false)
  const [importSummary, setImportSummary] = useState<ImportSummary | null>(null)
  const importInput = useRef<HTMLInputElement>(null)
  const rawHighlightRef = useRef<HTMLPreElement>(null)
  const rawLineNumbersRef = useRef<HTMLPreElement>(null)
  const seed = presets.find((preset) => preset.body)
  const availableEncoders = Array.from(
    new Set([...(health?.encoders ?? []), ...presets.map((preset) => preset.encoder)]),
  )

  const openDraft = (
    body: Record<string, unknown>,
    originalName: string | null,
    initialView: 'guided' | 'raw' = 'guided',
  ) => {
    const nextBody = cloneBody(body)
    setDraft({ originalName, body: nextBody, rawText: formatBody(nextBody), rawError: null })
    setView(initialView)
    if (rawHighlightRef.current) rawHighlightRef.current.style.transform = 'translate(0, 0)'
    if (rawLineNumbersRef.current) rawLineNumbersRef.current.style.transform = 'translateY(0)'
  }

  const updateGuided = (field: keyof GuidedPresetFields, value: string) => {
    setDraft((current) => {
      if (!current || (field === 'name' && current.originalName)) return current
      const form = { ...guidedFields(current.body), [field]: value }
      const body = patchGuidedLeaf(current.body, form)
      return { ...current, body, rawText: formatBody(body), rawError: null }
    })
  }

  const updateRaw = (rawText: string) => {
    if (!draft) return
    try {
      const parsed: unknown = JSON.parse(rawText)
      if (!isJsonObject(parsed)) throw new Error('Preset JSON must be an object.')
      const body = draft.originalName ? { ...parsed, PresetName: draft.originalName } : parsed
      setDraft({
        ...draft,
        body,
        rawText,
        rawError: null,
      })
    } catch (error) {
      setDraft({ ...draft, rawText, rawError: errorMessage(error) })
    }
  }

  const syncRawScroll = (event: UIEvent<HTMLTextAreaElement>) => {
    const { scrollLeft, scrollTop } = event.currentTarget
    if (rawHighlightRef.current) {
      rawHighlightRef.current.style.transform = `translate(${-scrollLeft}px, ${-scrollTop}px)`
    }
    if (rawLineNumbersRef.current) {
      rawLineNumbersRef.current.style.transform = `translateY(${-scrollTop}px)`
    }
  }

  const save = async () => {
    if (!draft || draft.rawError) return
    const name = draft.originalName ?? asString(draft.body.PresetName).trim()
    if (!name) {
      onError('Preset JSON must include a PresetName.')
      return
    }
    const leafError = completeLeafError({ ...draft.body, PresetName: name })
    if (leafError) {
      onError(leafError)
      return
    }

    setSaving(true)
    try {
      const saved = await saveEncoderPreset(name, draft.body)
      onSaved({
        name,
        encoder: asString(saved.body.VideoEncoder),
        video_preset: asString(saved.body.VideoPreset),
        file_format: asString(saved.body.FileFormat),
        body: saved.body,
      })
      setDraft({ ...draft, originalName: name, body: saved.body, rawText: formatBody(saved.body) })
    } catch (error) {
      onError(errorMessage(error))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (name: string) => {
    try {
      await deleteEncoderPreset(name)
      onDeleted(name)
      if (draft?.originalName === name) setDraft(null)
    } catch (error) {
      onError(errorMessage(error))
    }
  }

  const previewImportDocument = async (file: File) => {
    try {
      const parsed: unknown = JSON.parse(await file.text())
      if (!isJsonObject(parsed)) throw new Error('Imported preset document must be a JSON object.')
      const preview = await previewEncoderPresets(parsed)
      setImportDocument(parsed)
      setImportCandidates(preview.presets)
      setSelectedImportNames(
        preview.presets.filter((preset) => preset.supported).map((preset) => preset.name),
      )
      setImportSummary(null)
    } catch (error) {
      onError(errorMessage(error))
    }
  }

  const importSelectedPresets = async () => {
    if (!importDocument || selectedImportNames.length === 0) return
    setImporting(true)
    try {
      const summary = await importEncoderPresets(importDocument, selectedImportNames)
      setImportSummary({
        ...summary,
        unsupported: importCandidates?.filter((candidate) => !candidate.supported) ?? [],
      })
      setImportDocument(null)
      setImportCandidates(null)
      setSelectedImportNames([])
      onSaved()
    } catch (error) {
      onError(errorMessage(error))
    } finally {
      setImporting(false)
    }
  }

  const form = draft ? guidedFields(draft.body) : null
  const speedOptions = form
    ? Array.from(
        new Set([
          ...((health?.encoder_presets?.[form.encoder]?.length ?? 0) > 0
            ? (health?.encoder_presets?.[form.encoder] ?? [])
            : COMMON_SPEEDS),
          ...(form.videoPreset ? [form.videoPreset] : []),
        ]),
      )
    : []
  const encoderOptions = Array.from(
    new Set([...(availableEncoders ?? []), form?.encoder ?? '']),
  ).filter(Boolean)
  const qualityOptions = form
    ? Array.from(new Set([...COMMON_QUALITY_VALUES, ...(form.quality ? [form.quality] : [])]))
    : []

  return (
    <section aria-label="Preset editor" className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => importInput.current?.click()}
          className="rounded-lg border border-teal-400/25 bg-teal-400/10 px-3 py-2 text-[0.75rem] font-medium text-teal-300"
        >
          Import presets
        </button>
        <input
          ref={importInput}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void previewImportDocument(file)
            event.target.value = ''
          }}
        />
        <button
          type="button"
          disabled={!seed?.body && availableEncoders.length === 0}
          onClick={() => {
            const body = seed?.body
              ? cloneBody(seed.body)
              : {
                  PresetName: 'New preset',
                  VideoEncoder: availableEncoders[0] || '',
                  VideoPreset: '',
                  FileFormat: 'av_mkv',
                }
            const encoder = asString(body.VideoEncoder) || availableEncoders[0] || ''
            body.PresetName = 'New preset'
            body.VideoEncoder = encoder
            body.VideoPreset =
              asString(health?.encoder_presets?.[encoder]?.[0]) ||
              asString(body.VideoPreset) ||
              COMMON_SPEEDS[5]
            body.FileFormat = asString(body.FileFormat) || 'av_mkv'
            openDraft(body, null)
          }}
          className="rounded-lg border border-teal-400/25 bg-teal-400/10 px-3 py-2 text-[0.75rem] font-medium text-teal-300 disabled:opacity-50"
        >
          New guided preset
        </button>
        <button
          type="button"
          onClick={() => {
            const encoder = availableEncoders[0] || ''
            openDraft(
              {
                PresetName: 'New preset',
                VideoEncoder: encoder,
                VideoPreset: health?.encoder_presets?.[encoder]?.[0] || '',
                FileFormat: 'av_mkv',
              },
              null,
              'raw',
            )
          }}
          className="rounded-lg border border-white/10 bg-white/4 px-3 py-2 text-[0.75rem] text-[var(--text-secondary)] hover:text-white"
        >
          New raw preset
        </button>
      </div>

      {importCandidates && (
        <div className="space-y-3 rounded-xl border border-[var(--border)] bg-[rgba(0,0,0,0.2)] p-4">
          <p className="text-[0.78rem] font-medium text-[var(--text-secondary)]">
            Choose the presets to import.
          </p>
          <ul
            className="max-h-[360px] space-y-2 overflow-y-auto pr-1"
            aria-label="Preset import candidates"
          >
            {importCandidates.map((candidate) => {
              const isSelected = selectedImportNames.includes(candidate.name)
              return (
                <li
                  key={`${candidate.name}-${candidate.encoder}`}
                  className={`rounded-lg border p-2.5 transition-colors ${
                    isSelected
                      ? 'border-teal-500/30 bg-teal-500/[0.06]'
                      : 'border-white/6 bg-[rgba(0,0,0,0.15)]'
                  }`}
                >
                  <label
                    className={`flex cursor-pointer items-start gap-3 text-[0.78rem] ${
                      candidate.supported
                        ? 'text-[var(--text-primary)]'
                        : 'text-[var(--text-secondary)] opacity-60'
                    }`}
                  >
                    <input
                      type="checkbox"
                      aria-label={`Keep preset ${candidate.name}`}
                      checked={isSelected}
                      disabled={!candidate.supported || importing}
                      onChange={(event) => {
                        setSelectedImportNames((current) =>
                          event.target.checked
                            ? [...current, candidate.name]
                            : current.filter((name) => name !== candidate.name),
                        )
                      }}
                      className="mt-0.5 h-4 w-4 shrink-0 rounded border-white/20 bg-white/5 text-teal-400 focus:ring-teal-400"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-1.5">
                        <span className="font-medium text-[var(--text-primary)]">
                          {candidate.name}
                        </span>
                        <span className="text-[0.72rem] text-[var(--text-secondary)]">
                          ({candidate.encoder})
                        </span>
                      </div>
                      {!candidate.supported && candidate.reason && (
                        <span className="mt-1 block text-[0.72rem] text-amber-200">
                          {candidate.reason}
                        </span>
                      )}
                    </div>
                  </label>
                </li>
              )
            })}
          </ul>
          <div className="flex gap-2 pt-1">
            <button
              type="button"
              disabled={importing || selectedImportNames.length === 0}
              onClick={() => void importSelectedPresets()}
              className="rounded-lg border border-teal-400/25 bg-teal-400/10 px-3 py-2 text-[0.75rem] font-medium text-teal-300 disabled:opacity-50"
            >
              {importing ? 'Importing…' : 'Import selected presets'}
            </button>
            <button
              type="button"
              disabled={importing}
              onClick={() => {
                setImportDocument(null)
                setImportCandidates(null)
                setSelectedImportNames([])
              }}
              className="rounded-lg border border-white/8 px-3 py-2 text-[0.75rem] text-[var(--text-secondary)] disabled:opacity-50 hover:text-white"
            >
              Cancel import
            </button>
          </div>
          {selectedImportNames.length === 0 && (
            <p className="text-[0.72rem] text-amber-200">
              Select at least one supported preset to import.
            </p>
          )}
        </div>
      )}

      {importSummary && (
        <div
          role="status"
          className={`rounded-lg border px-3 py-2 text-[0.75rem] ${importSummary.skipped.length || importSummary.unsupported.length ? 'border-amber-400/30 bg-amber-400/10 text-amber-200' : 'border-teal-400/25 bg-teal-400/10 text-teal-200'}`}
        >
          Imported {importSummary.imported.length} preset
          {importSummary.imported.length === 1 ? '' : 's'}.
          {importSummary.unselected.length > 0 && (
            <p className="mt-1">Not imported: {importSummary.unselected.join(', ')}.</p>
          )}
          {importSummary.unsupported.length > 0 && (
            <ul className="mt-1 list-disc pl-4">
              {importSummary.unsupported.map((item) => (
                <li key={`${item.name}-${item.encoder}`}>
                  {item.name}: {item.reason}
                </li>
              ))}
            </ul>
          )}
          {importSummary.skipped.length > 0 && (
            <ul className="mt-1 list-disc pl-4">
              {importSummary.skipped.map((item) => (
                <li key={`${item.name}-${item.encoder}`}>
                  {item.name}: {item.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!seed?.body && availableEncoders.length === 0 && (
        <p role="status">Import a preset or create one in raw JSON before using guided creation.</p>
      )}

      <ul className="space-y-2" aria-label="Stored presets">
        {presets.map((preset) => (
          <li key={preset.name} className="flex items-center gap-2">
            <span className="text-[0.75rem] text-[var(--text-secondary)]">{preset.name}</span>
            <IconButton
              label={`Edit ${preset.name}`}
              disabled={!preset.body || saving}
              onClick={() => preset.body && openDraft(preset.body, preset.name)}
              tone="accent"
            >
              <PencilIcon />
            </IconButton>
            <IconButton
              label={`Delete ${preset.name}`}
              disabled={saving}
              onClick={() => void remove(preset.name)}
              tone="danger"
            >
              <TrashIcon />
            </IconButton>
          </li>
        ))}
      </ul>

      {draft && form && (
        <div className="space-y-3">
          <PresetEditorTabs view={view} onSelectView={setView} />

          {view === 'guided' ? (
            <div className="grid gap-2">
              <label>
                Name
                <input
                  value={form.name}
                  disabled={draft.originalName !== null}
                  onChange={(event) => updateGuided('name', event.target.value)}
                />
              </label>
              <div>
                <span className="block text-[0.78rem] text-[var(--text-secondary)] mb-1">
                  Video encoder
                </span>
                <EncoderSelect
                  aria-label="Video encoder"
                  value={form.encoder}
                  onChange={(event) => {
                    const encoder = event.target.value
                    updateGuided('encoder', encoder)
                    const firstSpeed = health?.encoder_presets?.[encoder]?.[0]
                    if (firstSpeed) updateGuided('videoPreset', firstSpeed)
                  }}
                  options={encoderOptions}
                />
              </div>
              <div>
                <span className="block text-[0.78rem] text-[var(--text-secondary)] mb-1">
                  Speed preset
                </span>
                <EncoderSelect
                  aria-label="Speed preset"
                  value={form.videoPreset}
                  onChange={(event) => updateGuided('videoPreset', event.target.value)}
                  options={speedOptions}
                />
              </div>
              <div>
                <span className="block text-[0.78rem] text-[var(--text-secondary)] mb-1">
                  File format
                </span>
                <EncoderSelect
                  aria-label="File format"
                  value={form.fileFormat}
                  onChange={(event) => updateGuided('fileFormat', event.target.value)}
                  options={['av_mkv', 'av_mp4', 'av_webm']}
                />
              </div>
              <div>
                <span className="block text-[0.78rem] text-[var(--text-secondary)] mb-1">
                  Quality type
                </span>
                <EncoderSelect
                  aria-label="Quality type"
                  value={form.qualityType}
                  onChange={(event) => updateGuided('qualityType', event.target.value)}
                  options={['0', '1', '2']}
                />
              </div>
              <div>
                <span className="block text-[0.78rem] text-[var(--text-secondary)] mb-1">
                  Quality
                </span>
                <EncoderSelect
                  aria-label="Quality"
                  value={form.quality}
                  onChange={(event) => updateGuided('quality', event.target.value)}
                  options={[{ value: '', label: 'Not set' }, ...qualityOptions]}
                />
              </div>
            </div>
          ) : (
            <label className="grid min-h-0 gap-1 text-[0.78rem] text-[var(--text-secondary)]">
              Preset JSON
              <div className="relative flex h-[28rem] max-h-[70vh] min-h-0 overflow-hidden rounded-lg border border-[var(--border)] bg-[#101018] font-mono text-[0.75rem] leading-5">
                <pre
                  ref={rawLineNumbersRef}
                  aria-hidden="true"
                  className="pointer-events-none h-full min-h-0 min-w-10 select-none border-r border-white/8 px-2 py-2 text-right text-white/25"
                >
                  {draft.rawText.split('\n').map((_, index) => `${index + 1}\n`)}
                </pre>
                <div className="relative min-h-0 flex-1">
                  <pre
                    ref={rawHighlightRef}
                    aria-hidden="true"
                    className="pointer-events-none absolute top-0 left-0 m-0 min-h-0 min-w-full w-max whitespace-pre px-3 py-2 font-mono text-[0.75rem] leading-5"
                    dangerouslySetInnerHTML={{ __html: highlightJson(draft.rawText) }}
                  />
                  <textarea
                    aria-label="Preset JSON"
                    value={draft.rawText}
                    onChange={(event) => updateRaw(event.target.value)}
                    onScroll={syncRawScroll}
                    wrap="off"
                    spellCheck={false}
                    className="relative z-10 h-full min-h-0 w-full resize-none overflow-auto bg-transparent px-3 py-2 font-mono text-[0.75rem] leading-5 text-transparent caret-white outline-none selection:bg-teal-400/25"
                  />
                </div>
              </div>
            </label>
          )}

          {draft.rawError && (
            <p role="alert" className="text-[0.78rem] text-red-300">
              {draft.rawError}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              disabled={Boolean(draft.rawError) || saving || Boolean(completeLeafError(draft.body))}
              onClick={() => void save()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-teal-400/15 px-3 py-2 text-[0.75rem] font-medium text-teal-300 disabled:opacity-50"
            >
              <SaveIcon size={14} /> {saving ? 'Saving…' : 'Save preset'}
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => setDraft(null)}
              className="rounded-lg border border-white/8 px-3 py-2 text-[0.75rem] text-[var(--text-secondary)] disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
