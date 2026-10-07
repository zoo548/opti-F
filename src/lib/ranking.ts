export type RankImportanceLevel = 'high' | 'mid' | 'low'

export type RankImportance = {
  time?: RankImportanceLevel
  cost?: RankImportanceLevel
}

export type RankingBetas = {
  gc: number
  knee: number
}

export const DEFAULT_BETAS: RankingBetas = { gc: 0.2, knee: 0.2 }
export const DEFAULT_IMPORTANCE: RankImportance = { time: 'mid', cost: 'mid' }
export const BETA_MAX = 0.6

export const IMPORTANCE_VALUE: Record<RankImportanceLevel, number> = {
  high: 4,
  mid: 3,
  low: 2,
}

export const IMPORTANCE_OPTIONS: { value: RankImportanceLevel; label: string }[] = [
  { value: 'low', label: '하' },
  { value: 'mid', label: '중' },
  { value: 'high', label: '상' },
]

const LEVEL_ALIASES: Record<string, RankImportanceLevel> = {
  high: 'high',
  상: 'high',
  mid: 'mid',
  medium: 'mid',
  중: 'mid',
  low: 'low',
  하: 'low',
}

export function parseImportanceLevel(raw: unknown): RankImportanceLevel | null {
  if (typeof raw !== 'string') return null
  return LEVEL_ALIASES[raw.trim()] ?? null
}

export function parseConditionImportance(raw: unknown): RankImportance {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_IMPORTANCE }
  const input = raw as Record<string, unknown>
  return {
    time: parseImportanceLevel(input.time) ?? DEFAULT_IMPORTANCE.time,
    cost: parseImportanceLevel(input.cost) ?? DEFAULT_IMPORTANCE.cost,
  }
}

export function parseManualBetas(raw: unknown): RankingBetas | null {
  if (!raw || typeof raw !== 'object') return null
  const input = raw as Record<string, unknown>
  const gc = Number(input.gc)
  const knee = Number(input.knee)
  if (!Number.isFinite(gc) || !Number.isFinite(knee)) return null
  if (gc < 0 || gc > BETA_MAX || knee < 0 || knee > BETA_MAX) return null
  return { gc, knee }
}

export function conditionShare(gc: number, knee: number) {
  let bg = gc
  let bk = knee
  if (bg + bk > 0.9 + 1e-9) {
    const scale = 0.9 / Math.max(bg + bk, 1e-9)
    bg *= scale
    bk *= scale
  }
  return { gc: bg, knee: bk, rest: 1 - bg - bk }
}

export function previewWeights(
  betas: RankingBetas,
  importance: RankImportance = DEFAULT_IMPORTANCE,
  axes: { time?: boolean; cost?: boolean } = { time: true, cost: true },
) {
  const split = conditionShare(betas.gc, betas.knee)
  const hasTime = Boolean(axes.time)
  const hasCost = Boolean(axes.cost)
  const out = { gc: split.gc, knee: split.knee, time: 0, cost: 0, rest: split.rest }
  if (!hasTime && !hasCost) {
    const tot = split.gc + split.knee
    if (tot <= 0) return { ...out, gc: 1, rest: 0 }
    return { ...out, gc: split.gc / tot, knee: split.knee / tot, rest: 0 }
  }
  if (hasTime && !hasCost) {
    out.time = split.rest
    return out
  }
  if (hasCost && !hasTime) {
    out.cost = split.rest
    return out
  }
  const timeV = IMPORTANCE_VALUE[importance.time || 'mid']
  const costV = IMPORTANCE_VALUE[importance.cost || 'mid']
  const sum = timeV + costV || 1
  out.time = split.rest * (timeV / sum)
  out.cost = split.rest * (costV / sum)
  return out
}

export function previewLine(weights: ReturnType<typeof previewWeights>) {
  const parts = [
    ['시간', weights.time],
    ['비용', weights.cost],
    ['가성비', weights.gc],
    ['균형점', weights.knee],
  ].filter(([, value]) => Number(value) > 1e-9)
  return parts.map(([label, value]) => `${label} ${Math.round(Number(value) * 100)}%`).join(' · ')
}

export function importanceForLimits(
  limits: {
    arrive_by?: string | null
    max_time_min?: number | null
    max_cost_krw?: number | null
  },
  prefs: RankImportance,
): RankImportance {
  const out: RankImportance = {}
  if (limits.arrive_by || limits.max_time_min != null) out.time = prefs.time || 'mid'
  if (limits.max_cost_krw != null) out.cost = prefs.cost || 'mid'
  return out
}

export function betasEqual(a: RankingBetas | null | undefined, b: RankingBetas | null | undefined) {
  if (!a || !b) return false
  return Math.abs(a.gc - b.gc) < 1e-9 && Math.abs(a.knee - b.knee) < 1e-9
}

export function importanceEqual(a: RankImportance | null | undefined, b: RankImportance | null | undefined) {
  return (a?.time || 'mid') === (b?.time || 'mid') && (a?.cost || 'mid') === (b?.cost || 'mid')
}

export const KIM_PRESET = {
  id: 'KIM',
  gc: 0.2,
  knee: 0.2,
  importance: { time: 'high', cost: 'low' } as RankImportance,
}

export function parseStoredPreset(raw: unknown) {
  if (!raw || typeof raw !== 'object') return null
  const input = raw as Record<string, unknown>
  const betas = parseManualBetas(input)
  if (!betas) return null
  const nested = (input.importance && typeof input.importance === 'object')
    ? (input.importance as Record<string, unknown>)
    : input
  return {
    ...betas,
    importance: parseConditionImportance({
      time: nested.time,
      cost: nested.cost,
    }),
  }
}

export function hydrateRankingPreset(raw: unknown): { betas: RankingBetas; importance: RankImportance } | null {
  const parsed = parseStoredPreset(raw)
  if (!parsed) return null
  return { betas: { gc: parsed.gc, knee: parsed.knee }, importance: parsed.importance }
}
