export type PlaceInput = {
  name: string
  lat: number
  lng: number
}

export type RouteTypeCode = 'PP' | 'PT' | 'TP' | 'TT' | string

export type RouteLeg = {
  mode: string
  name: string
  minutes: number
  color: string
  coords: number[][]
}

export type RouteCandidate = {
  id: string
  type: RouteTypeCode
  total_time: number
  weighted_time: number
  cost: number
  transfers: number
  gc: number | null
  knee_score: number | null
  is_pareto: boolean
  transfer_station: string | null
  legs: RouteLeg[]
}

export type AnalyzeResponse = {
  candidates: RouteCandidate[]
  top_gc: string[]
  top_knee: string[]
  anchors: {
    fastest: string | null
    cheapest: string | null
  }
  warnings: string[]
}

export type RankLimits = {
  max_time_min?: number | null
  max_cost_krw?: number | null
  max_transfers?: number | null
  arrive_by?: string | null
}

export type RankOver = {
  time_min?: number
  cost_krw?: number
  transfers?: number
}

export type RankItem = {
  id: string
  rank: number
  score: number
  meets_all: boolean
  over: RankOver
}

export type RankResponse = {
  ranking: RankItem[]
  robust: boolean
  applied_limits: RankLimits
}

export const ROUTE_TYPE_LABEL: Record<string, string> = {
  PP: '대중교통만',
  PT: '대중교통→택시',
  TP: '택시→대중교통',
  TT: '택시만',
}

const TRANSIT_PILL = '#3B82F6'
const TAXI_PILL = '#FB6B3C'
const WALK_PILL = '#9CA3AF'

function apiGet<T>(path: string, timeoutMs = 20_000): Promise<T> {
  const baseURL = import.meta.env.VITE_BACKEND_URL
  if (!baseURL) {
    return Promise.reject(new Error('VITE_BACKEND_URL이 설정되지 않았습니다'))
  }

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  return fetch(`${baseURL.replace(/\/$/, '')}${path}`, {
    method: 'GET',
    signal: controller.signal,
  })
    .then(async response => {
      if (!response.ok) {
        let detail = `서버 오류 (${response.status})`
        try {
          const parsed = await response.json()
          if (typeof parsed?.detail === 'string') detail = parsed.detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return response.json() as Promise<T>
    })
    .catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new Error('요청 시간이 초과되었습니다. 다시 시도해 주세요.')
      }
      if (error instanceof TypeError) {
        throw new Error('서버에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.')
      }
      throw error
    })
    .finally(() => window.clearTimeout(timer))
}

function sleep(ms: number) {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

function apiPost<T>(path: string, body: unknown, timeoutMs = 90_000): Promise<T> {
  const baseURL = import.meta.env.VITE_BACKEND_URL
  if (!baseURL) {
    return Promise.reject(new Error('VITE_BACKEND_URL이 설정되지 않았습니다'))
  }

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  return fetch(`${baseURL.replace(/\/$/, '')}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async response => {
      if (!response.ok) {
        let detail = `서버 오류 (${response.status})`
        try {
          const parsed = await response.json()
          if (typeof parsed?.detail === 'string') detail = parsed.detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return response.json() as Promise<T>
    })
    .catch(error => {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new Error('요청 시간이 초과되었습니다. 다시 시도해 주세요.')
      }
      if (error instanceof TypeError) {
        throw new Error('서버에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.')
      }
      throw error
    })
    .finally(() => window.clearTimeout(timer))
}

export type RouteParams = {
  vot: number
  alpha_bus: number
  beta_sub: number
  beta_sub_c: number
  gamma_walk: number
  delta_taxi: number
  transfer_penalty: number
}

export type SpLeg = {
  mode: string
  minutes: number
  crowded_minutes: number
  access_walk: number
}

export type SpAlternative = {
  index: number
  name: string
  cost: number
  total_min: number
  transfers: number
  in_vehicle_min: number
  walk_min: number
  legs: SpLeg[]
}

export type SpCard = {
  number: number
  alternatives: SpAlternative[]
}

export type SurveyResponse = {
  survey_token: string
  scenario_text: string
  cards: SpCard[]
}

export type SpProfile = {
  level?: number
  age_group: string
  purpose: string
  use_case?: string | null
  vot_direct_krw_per_min: number
  vot_posterior_krw_per_min: number
  weights_relative_to_uncrowded_subway: Record<string, number>
  transfer_cost_krw?: number | null
  posterior_sd?: Record<string, number>
  inactive_parameters?: string[]
  created_at?: string
  display_age?: string
  display_purpose?: string
  length?: number
  soft_mix?: boolean
}

export type EstimateResponse = {
  profile: SpProfile
  quality_warnings: string[]
  trap_failed?: boolean
  route_params: RouteParams
}

export type AnalyzeJobStatus = {
  status: 'running' | 'done' | 'error'
  progress: number
  stage: string
  result: AnalyzeResponse | null
  error?: string | null
}

export function analyzeRoutes(
  origin: PlaceInput,
  dest: PlaceInput,
  departTime?: string,
  params?: RouteParams | null,
  onProgress?: (info: { progress: number; stage: string }) => void,
): Promise<AnalyzeResponse> {
  const body: Record<string, unknown> = { origin, dest, departTime }
  if (params) body.params = params

  return (async () => {
    const job = await apiPost<{ job_id: string }>('/routes/analyze/jobs', body, 20_000)
    const deadline = Date.now() + 180_000
    while (true) {
      const status = await apiGet<AnalyzeJobStatus>(`/routes/analyze/jobs/${job.job_id}`)
      onProgress?.({
        progress: Number(status.progress) || 0,
        stage: status.stage || '서버를 깨우는 중이에요',
      })
      if (status.status === 'done') {
        if (!status.result) throw new Error('분석 결과가 없습니다.')
        return status.result
      }
      if (status.status === 'error') {
        throw new Error(status.error || '경로 분석에 실패했습니다.')
      }
      if (Date.now() > deadline) {
        throw new Error('요청 시간이 초과되었습니다. 다시 시도해 주세요.')
      }
      await sleep(2000)
    }
  })()
}

export function createSurvey(input: {
  age_group: string
  purpose: string
  vot_direct: number
  length: number
}): Promise<SurveyResponse> {
  return apiPost<SurveyResponse>('/sp/tasks', input)
}

export function estimateProfile(
  survey_token: string,
  responses: number[],
): Promise<EstimateResponse> {
  return apiPost<EstimateResponse>('/sp/estimate', { survey_token, responses })
}

export function rankRoutes(
  candidates: RouteCandidate[],
  limits: RankLimits,
): Promise<RankResponse> {
  return apiPost<RankResponse>('/routes/rank', { candidates, limits })
}

export function legToSegment(leg: RouteLeg) {
  const isWalk = leg.mode === '도보' || leg.mode === 'walk'
  const isTaxi = leg.mode === '택시' || leg.mode === 'taxi'
  return {
    label: leg.name,
    duration: Number(leg.minutes) || 0,
    color: isWalk ? WALK_PILL : isTaxi ? TAXI_PILL : TRANSIT_PILL,
    mode: (isWalk ? 'walk' : isTaxi ? 'taxi' : 'transit') as 'walk' | 'taxi' | 'transit',
    line: isWalk ? undefined : leg.name,
  }
}

export function orderByRanking(candidates: RouteCandidate[], ranking: RankItem[]): RouteCandidate[] {
  const byId = new Map(candidates.map(item => [item.id, item]))
  const ordered: RouteCandidate[] = []
  const seen = new Set<string>()
  for (const item of ranking) {
    const route = byId.get(item.id)
    if (route) {
      ordered.push(route)
      seen.add(item.id)
    }
  }
  for (const item of candidates) {
    if (!seen.has(item.id)) ordered.push(item)
  }
  return ordered
}

export function orderByTopKnee(data: AnalyzeResponse): RouteCandidate[] {
  const byId = new Map(data.candidates.map(item => [item.id, item]))
  const ordered: RouteCandidate[] = []
  const seen = new Set<string>()
  for (const id of data.top_knee) {
    const item = byId.get(id)
    if (item) {
      ordered.push(item)
      seen.add(id)
    }
  }
  for (const item of data.candidates) {
    if (!seen.has(item.id)) ordered.push(item)
  }
  return ordered
}
