import { KIM_PRESET, parseStoredPreset, type RankImportance, type RankingBetas } from '@/lib/ranking'

export { KIM_PRESET }

export const RANKING_PRESETS = {
  KIM: KIM_PRESET,
} as const

export function hydrateRankingPreset(raw: unknown): { betas: RankingBetas; importance: RankImportance } | null {
  const parsed = parseStoredPreset(raw)
  if (!parsed) return null
  return { betas: { gc: parsed.gc, knee: parsed.knee }, importance: parsed.importance }
}
