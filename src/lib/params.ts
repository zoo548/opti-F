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
  transfer_penalty: 11.24,
}

export type ParamSource = 'manual' | 'survey' | 'default'
export type VotSource = ParamSource

export type ManualCoeffKey = 'alpha_bus' | 'beta_sub_c' | 'gamma_walk' | 'delta_taxi' | 'transfer_penalty'

export type ManualParams = Partial<Record<ManualCoeffKey, number>>

export const MANUAL_COEFFS: {
  key: ManualCoeffKey
  label: string
  unit: string
  min: number
  max: number
  step: number
  hint: (value: number) => string
}[] = [
  {
    key: 'alpha_bus',
    label: '버스 체감',
    unit: '',
    min: 0.5,
    max: 3.0,
    step: 0.05,
    hint: value => `${value.toFixed(1)}면 버스 1분이 지하철 ${value.toFixed(1)}분처럼 느껴져요`,
  },
  {
    key: 'beta_sub_c',
    label: '혼잡 지하철 체감',
    unit: '',
    min: 1.0,
    max: 3.0,
    step: 0.05,
    hint: value => `${value.toFixed(1)}면 혼잡한 지하철 1분이 여유 지하철 ${value.toFixed(1)}분처럼 느껴져요`,
  },
  {
    key: 'gamma_walk',
    label: '도보 체감',
    unit: '',
    min: 0.5,
    max: 4.0,
    step: 0.05,
    hint: value => `${value.toFixed(1)}면 걷기 1분이 지하철 ${value.toFixed(1)}분처럼 느껴져요`,
  },
  {
    key: 'delta_taxi',
    label: '택시 체감',
    unit: '',
    min: 0.3,
    max: 2.0,
    step: 0.05,
    hint: value => `${value.toFixed(1)}면 택시 1분이 지하철 ${value.toFixed(1)}분처럼 느껴져요`,
  },
  {
    key: 'transfer_penalty',
    label: '환승 1회 저항',
    unit: '분',
    min: 0,
    max: 30,
    step: 0.01,
    hint: value => `환승 한 번을 ${value.toFixed(2)}분 더 걸린 것처럼 반영해요`,
  },
]

export function parseManualVot(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed || !/^\d+(\.\d+)?$/.test(trimmed)) return null
  const value = votToKrwPerMin(Number(trimmed))
  if (!Number.isFinite(value) || value < VOT_MIN || value > VOT_MAX) return null
  return value
}

export function parseManualCoeff(key: ManualCoeffKey, raw: string | number): number | null {
  const spec = MANUAL_COEFFS.find(item => item.key === key)
  if (!spec) return null
  const value = typeof raw === 'number' ? raw : Number(String(raw).trim())
  if (!Number.isFinite(value) || value < spec.min || value > spec.max) return null
  return value
}

export function sanitizeManualParams(raw: unknown): ManualParams {
  if (!raw || typeof raw !== 'object') return {}
  const input = raw as Record<string, unknown>
  const out: ManualParams = {}
  for (const spec of MANUAL_COEFFS) {
    const parsed = parseManualCoeff(spec.key, Number(input[spec.key]))
    if (parsed != null) out[spec.key] = parsed
  }
  return out
}

function pickValue(
  manual: number | undefined,
  survey: number | undefined,
  fallback: number,
): { value: number; source: ParamSource } {
  if (manual != null) return { value: manual, source: 'manual' }
  if (survey != null) return { value: survey, source: 'survey' }
  return { value: fallback, source: 'default' }
}

export function getEffectiveParams(input: {
  manualVot: number | null
  manualParams?: ManualParams | null
  routeParams: RouteParams | null
  usePersonal: boolean
}): { params: RouteParams; source: ParamSource; sources: Record<keyof RouteParams, ParamSource>; vot: number } {
  const survey = input.usePersonal && input.routeParams ? input.routeParams : null
  const manual = input.manualParams || {}

  const votPick = pickValue(
    input.manualVot != null ? votToKrwPerMin(input.manualVot) : undefined,
    survey ? votToKrwPerMin(survey.vot) : undefined,
    DEFAULT_ROUTE_PARAMS.vot,
  )
  const bus = pickValue(manual.alpha_bus, survey?.alpha_bus, DEFAULT_ROUTE_PARAMS.alpha_bus)
  const crowded = pickValue(manual.beta_sub_c, survey?.beta_sub_c, DEFAULT_ROUTE_PARAMS.beta_sub_c)
  const walk = pickValue(manual.gamma_walk, survey?.gamma_walk, DEFAULT_ROUTE_PARAMS.gamma_walk)
  const taxi = pickValue(manual.delta_taxi, survey?.delta_taxi, DEFAULT_ROUTE_PARAMS.delta_taxi)
  const transfer = pickValue(manual.transfer_penalty, survey?.transfer_penalty, DEFAULT_ROUTE_PARAMS.transfer_penalty)

  const params: RouteParams = {
    vot: votPick.value,
    alpha_bus: bus.value,
    beta_sub: 1.0,
    beta_sub_c: crowded.value,
    gamma_walk: walk.value,
    delta_taxi: taxi.value,
    transfer_penalty: transfer.value,
  }
  const sources: Record<keyof RouteParams, ParamSource> = {
    vot: votPick.source,
    alpha_bus: bus.source,
    beta_sub: 'default',
    beta_sub_c: crowded.source,
    gamma_walk: walk.source,
    delta_taxi: taxi.source,
    transfer_penalty: transfer.source,
  }
  return { params, source: votPick.source, sources, vot: params.vot }
}
