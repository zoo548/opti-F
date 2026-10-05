import type { RouteParams } from '@/lib/api'

/** SP UI · sp_engine · route_engine 모두 원/분. GC = vot(원/분) × 가중시간(분) + 요금(원). */
export function votToKrwPerMin(value: number) {
  return value
}

export const VOT_MIN = 50
export const VOT_MAX = 3000

export const DEFAULT_ROUTE_PARAMS: RouteParams = {
  vot: 400,
  alpha_bus: 1.18,
  beta_sub: 1.0,
  beta_sub_c: 1.55,
  gamma_walk: 2.35,
  delta_taxi: 0.88,
  transfer_penalty: 5.0,
}

export type VotSource = 'manual' | 'survey' | 'default'

export function parseManualVot(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed || !/^\d+(\.\d+)?$/.test(trimmed)) return null
  const value = votToKrwPerMin(Number(trimmed))
  if (!Number.isFinite(value) || value < VOT_MIN || value > VOT_MAX) return null
  return value
}

export function getEffectiveParams(input: {
  manualVot: number | null
  routeParams: RouteParams | null
  usePersonal: boolean
}): { params: RouteParams; source: VotSource; vot: number } {
  const weights = input.usePersonal && input.routeParams ? input.routeParams : DEFAULT_ROUTE_PARAMS
  let vot = DEFAULT_ROUTE_PARAMS.vot
  let source: VotSource = 'default'
  if (input.usePersonal && input.routeParams) {
    vot = votToKrwPerMin(input.routeParams.vot)
    source = 'survey'
  }
  if (input.manualVot != null) {
    vot = votToKrwPerMin(input.manualVot)
    source = 'manual'
  }
  return { params: { ...weights, vot }, source, vot }
}
