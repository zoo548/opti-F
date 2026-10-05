export type RankImportanceLevel = 'low' | 'medium' | 'high'

export type RankImportance = {
  time?: RankImportanceLevel
  duration?: RankImportanceLevel
  cost?: RankImportanceLevel
  transfer?: RankImportanceLevel
}

export type RankingBetas = {
  gc: number
  knee: number
}

export const DEFAULT_BETAS: RankingBetas = { gc: 0.3, knee: 0.2 }
export const BETA_MAX = 0.6

export const IMPORTANCE_VALUE: Record<RankImportanceLevel, number> = {
  low: 1,
  medium: 3,
  high: 5,
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

export type PreviewAxis = 'time' | 'cost' | 'transfer'

export function previewWeights(
  betas: RankingBetas,
  axes: PreviewAxis[],
  importance: RankImportance = {},
) {
  if (axes.length === 0) {
    const tot = betas.gc + betas.knee
    if (tot <= 0) return { gc: 1, knee: 0, time: 0, cost: 0, transfer: 0, rest: 0 }
    return { gc: betas.gc / tot, knee: betas.knee / tot, time: 0, cost: 0, transfer: 0, rest: 0 }
  }
  const split = conditionShare(betas.gc, betas.knee)
  const keyMap: Record<PreviewAxis, keyof RankImportance> = {
    time: 'time',
    cost: 'cost',
    transfer: 'transfer',
  }
  const raw = axes.map(axis => {
    const level = (importance[keyMap[axis]] || 'medium') as RankImportanceLevel
    return { axis, value: IMPORTANCE_VALUE[level] || 3 }
  })
  const sum = raw.reduce((acc, item) => acc + item.value, 0) || 1
  const out = { gc: split.gc, knee: split.knee, time: 0, cost: 0, transfer: 0, rest: split.rest }
  for (const item of raw) {
    out[item.axis] = split.rest * (item.value / sum)
  }
  return out
}

export function previewLine(weights: ReturnType<typeof previewWeights>) {
  const parts = [
    ['가성비', weights.gc],
    ['균형점', weights.knee],
    ['시간', weights.time],
    ['요금', weights.cost],
    ['환승', weights.transfer],
  ].filter(([, value]) => Number(value) > 1e-9)
  return parts.map(([label, value]) => `${label} ${Math.round(Number(value) * 100)}%`).join(' · ')
}

export function betasEqual(a: RankingBetas | null | undefined, b: RankingBetas | null | undefined) {
  if (!a || !b) return false
  return Math.abs(a.gc - b.gc) < 1e-9 && Math.abs(a.knee - b.knee) < 1e-9
}
