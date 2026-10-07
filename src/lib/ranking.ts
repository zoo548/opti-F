export type RankImportanceLevel = 'low' | 'medium' | 'high'

export type RankImportance = {
  time?: RankImportanceLevel | number
  duration?: RankImportanceLevel | number
  cost?: RankImportanceLevel | number
  transfer?: RankImportanceLevel | number
}

export type RankingBetas = {
  gc: number
  knee: number
  timeShare: number
}

export const DEFAULT_BETAS: RankingBetas = { gc: 0.2, knee: 0.2, timeShare: 0.5 }
export const BETA_MAX = 0.6

export const TIME_COST_CHIPS: { label: string; timeShare: number }[] = [
  { label: '시간 중시 70:30', timeShare: 0.7 },
  { label: '균형 50:50', timeShare: 0.5 },
  { label: '비용 중시 30:70', timeShare: 0.3 },
]

export const IMPORTANCE_VALUE: Record<RankImportanceLevel, number> = {
  low: 1,
  medium: 3,
  high: 5,
}

function clampShare(value: number) {
  if (!Number.isFinite(value)) return 0.5
  if (value > 1) return Math.min(1, Math.max(0, value / 100))
  return Math.min(1, Math.max(0, value))
}

export function parseManualBetas(raw: unknown): RankingBetas | null {
  if (!raw || typeof raw !== 'object') return null
  const input = raw as Record<string, unknown>
  const gc = Number(input.gc)
  const knee = Number(input.knee)
  if (!Number.isFinite(gc) || !Number.isFinite(knee)) return null
  if (gc < 0 || gc > BETA_MAX || knee < 0 || knee > BETA_MAX) return null
  const rawShare = input.timeShare ?? input.time_share
  const timeShare = rawShare == null ? DEFAULT_BETAS.timeShare : clampShare(Number(rawShare))
  return { gc, knee, timeShare }
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

export type PreviewAxis = 'time' | 'cost' | 'transfer'

export function previewWeights(
  betas: RankingBetas,
  axes: PreviewAxis[] = ['time', 'cost'],
) {
  const split = conditionShare(betas.gc, betas.knee)
  const timeShare = clampShare(betas.timeShare)
  if (axes.length === 0) {
    const tot = split.gc + split.knee
    if (tot <= 0) return { gc: 1, knee: 0, time: 0, cost: 0, transfer: 0, rest: 0 }
    return { gc: split.gc / tot, knee: split.knee / tot, time: 0, cost: 0, transfer: 0, rest: 0 }
  }
  const out = { gc: split.gc, knee: split.knee, time: 0, cost: 0, transfer: 0, rest: split.rest }
  const hasTime = axes.includes('time')
  const hasCost = axes.includes('cost')
  const hasTransfer = axes.includes('transfer')
  if (hasTime || hasCost) {
    if (hasTime && hasCost) {
      out.time = split.rest * timeShare
      out.cost = split.rest * (1 - timeShare)
    } else if (hasTime) {
      out.time = split.rest
    } else {
      out.cost = split.rest
    }
  } else if (hasTransfer) {
    out.transfer = split.rest
  }
  return out
}

export function previewLine(weights: ReturnType<typeof previewWeights>) {
  const parts = [
    ['시간', weights.time],
    ['비용', weights.cost],
    ['가성비', weights.gc],
    ['균형점', weights.knee],
    ['환승', weights.transfer],
  ].filter(([, value]) => Number(value) > 1e-9)
  return parts.map(([label, value]) => `${label} ${Math.round(Number(value) * 100)}%`).join(' · ')
}

export function ratioPreviewLine(betas: RankingBetas) {
  return previewLine(previewWeights(betas, ['time', 'cost']))
}

export function importanceFromPrefs(
  limits: {
    arrive_by?: string | null
    max_time_min?: number | null
    max_cost_krw?: number | null
    max_transfers?: number | null
  },
  prefs: RankingBetas,
): RankImportance {
  const timeW = Math.max(1, Math.round(clampShare(prefs.timeShare) * 100))
  const costW = Math.max(1, 100 - timeW)
  const out: RankImportance = {}
  if (limits.arrive_by) out.time = timeW
  if (limits.max_time_min != null) out.duration = timeW
  if (limits.max_cost_krw != null) out.cost = costW
  if (limits.max_transfers != null) out.transfer = 3
  return out
}

export function betasEqual(a: RankingBetas | null | undefined, b: RankingBetas | null | undefined) {
  if (!a || !b) return false
  return (
    Math.abs(a.gc - b.gc) < 1e-9 &&
    Math.abs(a.knee - b.knee) < 1e-9 &&
    Math.abs((a.timeShare ?? 0.5) - (b.timeShare ?? 0.5)) < 1e-9
  )
}
