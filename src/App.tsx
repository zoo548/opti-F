import { useState, useEffect, useRef, createContext, useContext } from 'react'
import { usePlaceSearch, type Place } from '@/hooks/usePlaceSearch'
import {
  analyzeRoutes,
  createSurvey,
  estimateProfile,
  rankRoutes,
  orderByTopKnee,
  orderByRanking,
  legToSegment,
  ROUTE_TYPE_LABEL,
  type AnalyzeResponse,
  type EstimateResponse,
  type RankLimits,
  type RankOver,
  type RankResponse,
  type RouteCandidate,
  type RouteParams,
  type SpAlternative,
  type SpProfile,
  type SurveyResponse,
} from '@/lib/api'
import { getEffectiveParams, MANUAL_COEFFS, parseManualCoeff, parseManualVot, sanitizeManualParams, VOT_MAX, VOT_MIN, type ManualCoeffKey, type ManualParams, type ParamSource } from '@/lib/params'
import { BETA_MAX, betasEqual, conditionShare, DEFAULT_BETAS, parseManualBetas, previewLine, previewWeights, type RankingBetas } from '@/lib/ranking'

// ─── Types ───────────────────────────────────────────────────────────────────
type Screen =
  | 'home' | 'sp-setup' | 'sp-question' | 'sp-complete' | 'sp-profile'
  | 'search-input' | 'analyzing' | 'results' | 'reservation' | 'regret' | 'detail'

type TripContextValue = {
  origin: Place | null
  destination: Place | null
  setOrigin: (place: Place | null) => void
  setDestination: (place: Place | null) => void
  analysis: AnalyzeResponse | null
  analyzeError: string | null
  analyzeProgress: number
  analyzeStage: string
  selectedRoute: RouteCandidate | null
  setSelectedRoute: (route: RouteCandidate | null) => void
  departTime: string | null
  ranking: RankResponse | null
  rankError: string | null
  rankingBusy: boolean
  pendingLimits: RankLimits | null
  appliedLimits: RankLimits | null
  setPendingLimits: (limits: RankLimits | null) => void
  departMode: 'now' | 'scheduled'
  scheduledDepart: string | null
  setDepartNow: () => void
  setScheduledDepart: (iso: string) => void
  startAnalyze: (origin: Place, destination: Place) => void
  retryAnalyze: () => void
  waitForAnalyze: () => Promise<AnalyzeResponse>
  applyRank: (limits: RankLimits, candidates?: RouteCandidate[]) => Promise<void>
  clearRanking: () => void
  rankedBetas: RankingBetas | null
}

const TripContext = createContext<TripContextValue | null>(null)

function useTrip() {
  const ctx = useContext(TripContext)
  if (!ctx) throw new Error('TripContext missing')
  return ctx
}

const PROFILE_KEY = 'opti.sp.profile'
const PARAMS_KEY = 'opti.sp.route_params'
const USE_PERSONAL_KEY = 'opti.sp.use_personal'
const MANUAL_VOT_KEY = 'opti.sp.manual_vot'
const MANUAL_PARAMS_KEY = 'opti.sp.manual_params'
const MANUAL_BETAS_KEY = 'opti.sp.manual_betas'
const USER_STORE_KEY = 'opti.user.prefs'

type SpContextValue = {
  survey: SurveyResponse | null
  surveyBusy: boolean
  surveyError: string | null
  answers: Record<number, number>
  setAnswer: (cardIndex: number, altIndex: number) => void
  startSurvey: (age: string, purpose: string, votDirect: number, length: number) => Promise<void>
  estimateBusy: boolean
  estimateError: string | null
  estimateResult: EstimateResponse | null
  runEstimate: () => Promise<void>
  profile: SpProfile | null
  routeParams: RouteParams | null
  usePersonal: boolean
  setUsePersonal: (value: boolean) => void
  displayAge: string
  displayPurpose: string
  questionCount: number
  restartSurvey: () => void
  manualVot: number | null
  setManualVot: (value: number | null) => void
  manualParams: ManualParams
  setManualCoeff: (key: ManualCoeffKey, value: number | null) => boolean
  resetManualParams: () => void
  rankingBetas: RankingBetas
  setRankingBetas: (value: RankingBetas) => void
  resetRankingBetas: () => void
}

const SpContext = createContext<SpContextValue | null>(null)

function useSp() {
  const ctx = useContext(SpContext)
  if (!ctx) throw new Error('SpContext missing')
  return ctx
}

function readStorage<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function writeStorage(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* ignore quota / private mode */
  }
}

function persistUserStore(
  manualVot: number | null,
  profile: SpProfile | null,
  routeParams: RouteParams | null,
  manualParams: ManualParams,
  rankingBetas: RankingBetas,
) {
  writeStorage(USER_STORE_KEY, {
    manual_vot: manualVot,
    manual_params: manualParams,
    manual_betas: rankingBetas,
    sp_profile: profile,
    route_params: routeParams,
  })
}

function altToSegments(alt: SpAlternative): Segment[] {
  const color: Record<string, string> = {
    subway: '#2F7BF6',
    bus: '#16A34A',
    taxi: '#FF6B3D',
  }
  const label: Record<string, string> = {
    subway: '지하철',
    bus: '버스',
    taxi: '택시',
  }
  const segments: Segment[] = []
  let walkAccounted = 0
  for (const leg of alt.legs || []) {
    const access = Number(leg.access_walk) || 0
    if (access > 0) {
      segments.push({ label: '도보', duration: access, color: '#9CA3AF', mode: 'walk' })
      walkAccounted += access
    }
    const mode = leg.mode === 'taxi' ? 'taxi' : 'transit'
    segments.push({
      label: label[leg.mode] || '지하철',
      duration: Number(leg.minutes) || 0,
      color: color[leg.mode] || '#2F7BF6',
      mode,
    })
  }
  const leftover = Math.max(0, (Number(alt.walk_min) || 0) - walkAccounted)
  if (leftover > 0) {
    segments.push({ label: '도보', duration: leftover, color: '#9CA3AF', mode: 'walk' })
  }
  return segments
}

function feelLine(mode: string, weight: number) {
  const w = Number.isFinite(weight) ? weight : 1
  return `${mode} 1분 = 지하철 ${w.toFixed(1)}분처럼 느껴요`
}

function formatProfileDate(iso?: string | null) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`
}

function formatDepartLabel(iso: string | null) {
  const d = iso ? new Date(iso) : new Date()
  const wk = ['일', '월', '화', '수', '목', '금', '토'][d.getDay()]
  return `${d.getFullYear()}. ${d.getMonth() + 1}. ${d.getDate()}.(${wk}) ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')} 출발`
}

function arrivalLabel(iso: string | null, minutes: number) {
  const d = iso ? new Date(iso) : new Date()
  const at = new Date(d.getTime() + minutes * 60_000)
  const h = at.getHours()
  const m = String(at.getMinutes()).padStart(2, '0')
  const ap = h < 12 ? '오전' : '오후'
  const h12 = h % 12 || 12
  return `${ap} ${h12}:${m}`
}

function clockLabel(iso: string | null) {
  const d = iso ? new Date(iso) : new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function seoulIso(date: Date) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const pick = (type: string) => parts.find(part => part.type === type)?.value || '00'
  return `${pick('year')}-${pick('month')}-${pick('day')}T${pick('hour')}:${pick('minute')}:${pick('second')}+09:00`
}

function seoulParts(iso: string | null) {
  const d = iso ? new Date(iso) : new Date()
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d)
  const pick = (type: string) => parts.find(part => part.type === type)?.value || ''
  return {
    year: Number(pick('year')),
    month: Number(pick('month')),
    day: Number(pick('day')),
    hour: Number(pick('hour')),
    minute: Number(pick('minute')),
    weekday: pick('weekday'),
  }
}

function buildSeoulIso(year: number, month: number, day: number, hour: number, minute: number) {
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00+09:00`
}

function departChipLabel(iso: string) {
  const now = seoulParts(null)
  const then = seoulParts(iso)
  const today = Date.UTC(now.year, now.month - 1, now.day)
  const target = Date.UTC(then.year, then.month - 1, then.day)
  const diff = Math.round((target - today) / 86400000)
  const clock = `${String(then.hour).padStart(2, '0')}:${String(then.minute).padStart(2, '0')}`
  if (diff === 0) return `오늘 ${clock} 출발`
  if (diff === 1) return `내일 ${clock} 출발`
  return `${then.month}/${then.day} ${clock} 출발`
}

function sourceBadge(source: ParamSource) {
  const label = source === 'manual' ? '직접' : source === 'survey' ? '설문' : '기본'
  const cls = source === 'manual'
    ? 'text-[#2F7BF6] bg-[#EAF2FF]'
    : source === 'survey'
      ? 'text-[#16A34A] bg-[#DCFCE7]'
      : 'text-[#6B7280] bg-[#F3F4F6]'
  return <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${cls}`}>{label}</span>
}

const fmt = (n: number) => n.toLocaleString('ko-KR')

function clockAfter(iso: string | null, minutes: number) {
  const d = iso ? new Date(iso) : new Date()
  return clockLabel(new Date(d.getTime() + minutes * 60_000).toISOString())
}

function arrivalToIso(departIso: string | null, hhmm: string) {
  const base = departIso ? new Date(departIso) : new Date()
  const [hour, minute] = hhmm.split(':').map(Number)
  const at = new Date(base)
  at.setHours(hour, minute, 0, 0)
  if (at.getTime() < base.getTime()) at.setDate(at.getDate() + 1)
  return at.toISOString()
}

function overHint(over?: RankOver | null) {
  if (!over) return ''
  const parts: string[] = []
  if (over.time_min && over.time_min > 0) parts.push(`시간 +${Math.round(over.time_min)}분`)
  if (over.cost_krw && over.cost_krw > 0) parts.push(`요금 +${fmt(Math.round(over.cost_krw))}원`)
  if (over.transfers && over.transfers > 0) parts.push(`환승 +${Math.round(over.transfers)}회`)
  return parts.length ? `희망 조건보다 ${parts.join(', ')}` : ''
}

function limitsSummary(limits: RankLimits | null) {
  if (!limits) return ''
  const parts: string[] = []
  if (limits.max_time_min != null) parts.push(`시간 ${Math.round(limits.max_time_min)}분 이하`)
  if (limits.max_cost_krw != null) parts.push(`요금 ${fmt(Math.round(limits.max_cost_krw))}원 이하`)
  if (limits.max_transfers != null) parts.push(`환승 ${Math.round(limits.max_transfers)}회 이하`)
  if (limits.arrive_by) parts.push(`도착 ${clockLabel(limits.arrive_by)}까지`)
  return parts.join(' · ')
}

function hasLimits(limits: RankLimits | null | undefined) {
  if (!limits) return false
  return (
    limits.max_time_min != null ||
    limits.max_cost_krw != null ||
    limits.max_transfers != null ||
    Boolean(limits.arrive_by)
  )
}

function rangeHint(low?: { label: string; value: string }, high?: { label: string; value: string }) {
  if (low && high) return `${low.label} ${low.value} ~ ${high.label} ${high.value}`
  if (low) return `${low.label} ${low.value}`
  if (high) return `${high.label} ${high.value}`
  return ''
}

type TimelineItem = {
  time: string
  label: string
  sub: string
  dot: string | null
  line: string | null
  station?: boolean
  taxi?: boolean
  badge?: string
}

function legsToTimeline(
  route: RouteCandidate,
  originName: string,
  destName: string,
  departIso: string | null,
): TimelineItem[] {
  const TRANSIT = '#3B82F6'
  const TAXI = '#FF6B3D'
  const WALK = '#9CA3AF'
  const items: TimelineItem[] = []
  let elapsed = 0
  const legs = route.legs || []
  const vehicleIdx = legs
    .map((leg, index) => ({ leg, index }))
    .filter(({ leg }) => leg.mode !== '도보' && leg.mode !== 'walk')
  const lastVehicle = vehicleIdx[vehicleIdx.length - 1]?.index
  const taxiFare = route.type === 'TT' ? route.cost : null

  items.push({
    time: clockAfter(departIso, 0),
    label: originName || '[출발지]',
    sub: '출발',
    dot: WALK,
    line: '#E5E7EB',
    station: true,
  })

  legs.forEach((leg, index) => {
    const isWalk = leg.mode === '도보' || leg.mode === 'walk'
    const isTaxi = leg.mode === '택시' || leg.mode === 'taxi'
    const color = isWalk ? WALK : isTaxi ? TAXI : TRANSIT
    const minutes = Number(leg.minutes) || 0
    const lineName = leg.name || (isTaxi ? '택시' : isWalk ? '도보' : '대중교통')

    if (isWalk) {
      items.push({
        time: '',
        label: `${lineName} ${Math.round(minutes)}분`,
        sub: '',
        dot: null,
        line: WALK,
      })
      elapsed += minutes
      return
    }

    const isFirstVehicle = index === vehicleIdx[0]?.index
    const isLastVehicle = index === lastVehicle
    const stationName = isFirstVehicle
      ? (originName || '승차')
      : isLastVehicle
        ? (destName || '하차')
        : (route.transfer_station || '환승')
    const role = isFirstVehicle ? '승차' : isLastVehicle ? '하차' : '환승'

    items.push({
      time: clockAfter(departIso, elapsed),
      label: stationName,
      sub: `${lineName} ${role}`,
      dot: color,
      line: isTaxi ? '#E5E7EB' : color,
      station: true,
      taxi: isTaxi,
    })
    items.push({
      time: '',
      label: `${lineName} ${Math.round(minutes)}분`,
      sub: isTaxi && taxiFare != null ? `예상 요금 ${fmt(Math.round(taxiFare))}원` : '',
      dot: color,
      line: color,
      badge: lineName,
      taxi: isTaxi,
    })
    elapsed += minutes
  })

  items.push({
    time: clockAfter(departIso, elapsed),
    label: destName || '[도착지]',
    sub: '도착',
    dot: TAXI,
    line: null,
    station: true,
  })
  return items
}

// ─── Constants ────────────────────────────────────────────────────────────────
type SegMode = 'walk' | 'transit' | 'taxi'
type StepMode = 'transit' | 'taxi' | 'end'
type Segment = { label: string; duration: number; color: string; mode: SegMode; line?: string }
type Step = { station: string; line: string; color: string; mode: StepMode; dir?: string; minutes?: number; crowd?: number; fare?: number; taxiTransfer?: boolean }

const ROUTES = [
  {
    id: 'P05', type: 'PT', rank: 1, S: 0.068,
    label: 'GTX-A → 수도권 3호선 · 고속터미널 → 택시',
    time: 67.3, cost: 13950, transfers: 2, arrival: '11:37',
    tags: ['추천', '요금 최저'],
    segments: [
      { label: '도보', duration: 4, color: '#9CA3AF', mode: 'walk' },
      { label: 'GTX-A', duration: 12, color: '#8B5CF6', mode: 'transit', line: 'GTX-A' },
      { label: '3호선', duration: 21, color: '#F97316', mode: 'transit', line: '3호선' },
      { label: '택시', duration: 13, color: '#FF6B3D', mode: 'taxi' },
    ] as Segment[],
    steps: [
      { station: '구성역', line: 'GTX-A', color: '#8B5CF6', mode: 'transit', dir: '수서 방면', minutes: 12 },
      { station: '수서역', line: '3호선', color: '#F97316', mode: 'transit', dir: '대화 방면', minutes: 21, crowd: 6, taxiTransfer: false },
      { station: '고속터미널역', line: '택시', color: '#FF6B3D', mode: 'taxi', dir: '63빌딩까지 약 13분', fare: 10300, taxiTransfer: true },
      { station: '63빌딩', line: '', color: '#9CA3AF', mode: 'end' },
    ] as Step[],
    compare: { transitMin: 15, taxiWon: 35970 },
    dominated: false,
  },
  {
    id: 'P03', type: 'PT', rank: 2, S: 0.090,
    label: 'GTX-A · 성남 → 택시',
    time: 68.3, cost: 34230, transfers: 1, arrival: '11:38',
    tags: ['빠름'],
    segments: [
      { label: '도보', duration: 6, color: '#9CA3AF', mode: 'walk' },
      { label: 'GTX-A', duration: 18, color: '#8B5CF6', mode: 'transit', line: 'GTX-A' },
      { label: '택시', duration: 40, color: '#FF6B3D', mode: 'taxi' },
    ] as Segment[],
    steps: [
      { station: '구성역', line: 'GTX-A', color: '#8B5CF6', mode: 'transit', dir: '수서 방면', minutes: 18 },
      { station: '성남역', line: '택시', color: '#FF6B3D', mode: 'taxi', dir: '63빌딩까지 약 40분', fare: 28500, taxiTransfer: true },
      { station: '63빌딩', line: '', color: '#9CA3AF', mode: 'end' },
    ] as Step[],
    compare: { transitMin: 14, taxiWon: 15690 },
    dominated: false,
  },
  {
    id: 'P01', type: 'TT', rank: 3, S: 0.148,
    label: '택시 직행',
    time: 68.1, cost: 49920, transfers: 0, arrival: '11:38',
    tags: ['환승 없음'],
    segments: [
      { label: '택시', duration: 68, color: '#FF6B3D', mode: 'taxi' },
    ] as Segment[],
    steps: [
      { station: '구성역', line: '택시', color: '#FF6B3D', mode: 'taxi', dir: '63빌딩까지 직행', fare: 49920, taxiTransfer: false },
      { station: '63빌딩', line: '', color: '#9CA3AF', mode: 'end' },
    ] as Step[],
    compare: { transitMin: 14, taxiWon: 0 },
    dominated: false,
  },
  {
    id: 'P08', type: 'PP', rank: null, S: null,
    label: '대중교통 (1호선+환승)',
    time: 82, cost: 5550, transfers: 4, arrival: '11:52',
    tags: ['요금 최저'],
    segments: [
      { label: '도보', duration: 7, color: '#9CA3AF', mode: 'walk' },
      { label: '1호선', duration: 38, color: '#2F7BF6', mode: 'transit', line: '1호선' },
      { label: '9호선', duration: 18, color: '#EAB308', mode: 'transit', line: '9호선' },
      { label: '도보', duration: 14, color: '#9CA3AF', mode: 'walk' },
    ] as Segment[],
    steps: [
      { station: '구성역', line: '1호선', color: '#2F7BF6', mode: 'transit', dir: '서울역 방면', minutes: 38 },
      { station: '노량진역', line: '9호선', color: '#EAB308', mode: 'transit', dir: '김포공항 방면', minutes: 18 },
      { station: '여의도역', line: '', color: '#9CA3AF', mode: 'end' },
    ] as Step[],
    compare: { transitMin: 0, taxiWon: 44370 },
    dominated: true,
  },
]

const PARAMS = {
  VOT: { label: 'VOT 시간가치', unit: '원/분', value: 387, sd: 52, color: '#2F7BF6' },
  α: { label: 'α 버스 가중치', unit: '', value: 1.28, sd: 0.21, color: '#374151' },
  δ: { label: 'δ 택시 가중치', unit: '', value: 0.71, sd: 0.09, color: '#FF6B3D' },
  γ: { label: 'γ 도보 가중치', unit: '', value: 1.63, sd: 0.24, color: '#374151' },
  θ: { label: 'θ 환승저항', unit: '분/회', value: 10.8, sd: 1.43, color: '#22C55E' },
  βc: { label: 'β_c 혼잡 지하철', unit: '', value: 1.61, sd: 0.18, color: '#EF4444' },
}

// ─── Small Components ─────────────────────────────────────────────────────────

function Chip({ label, active = false, color = 'default', onClick }: {
  label: string; active?: boolean; color?: 'default' | 'blue' | 'green'; onClick?: () => void
}) {
  const base = 'px-4 py-2 rounded-full text-[13px] font-medium border transition-all cursor-pointer select-none'
  const styles = {
    default: active ? 'bg-[#2F7BF6] text-white border-[#2F7BF6]' : 'bg-white text-[#374151] border-[#E5E7EB]',
    blue: 'bg-[#EBF2FF] text-[#2F7BF6] border-[#BFDBFE]',
    green: 'bg-[#DCFCE7] text-[#16A34A] border-[#BBF7D0]',
  }
  return <button className={`${base} ${styles[active && color === 'default' ? 'default' : color]}`} onClick={onClick}>{label}</button>
}

// ─── Transit Icons ───────────────────────────────────────────────────────────
function IconSubway({ color, size = 14 }: { color: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="3" width="12" height="9" rx="2" fill={color}/>
      <rect x="4" y="5" width="3" height="3" rx="0.5" fill="white"/>
      <rect x="9" y="5" width="3" height="3" rx="0.5" fill="white"/>
      <line x1="2" y1="9.5" x2="14" y2="9.5" stroke="white" strokeWidth="0.8"/>
      <line x1="5" y1="12" x2="4" y2="14" stroke={color} strokeWidth="1.2" strokeLinecap="round"/>
      <line x1="11" y1="12" x2="12" y2="14" stroke={color} strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  )
}

function IconTaxi({ color, size = 14 }: { color: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="6" width="14" height="7" rx="2" fill={color}/>
      <path d="M4 6L5.5 3h5L12 6" fill={color}/>
      <circle cx="4" cy="13.5" r="1.5" fill="white"/>
      <circle cx="12" cy="13.5" r="1.5" fill="white"/>
      <rect x="6" y="7.5" width="4" height="2.5" rx="0.5" fill="white"/>
    </svg>
  )
}

function IconWalk({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="8" cy="2.5" r="1.5" fill="#9CA3AF"/>
      <path d="M8 4.5L6 9h4L8 4.5Z" fill="#9CA3AF"/>
      <path d="M6 9L4.5 13M10 9L11.5 13" stroke="#9CA3AF" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  )
}

function ModeIcon({ mode, color, size = 14 }: { mode: SegMode | StepMode; color: string; size?: number }) {
  if (mode === 'taxi') return <IconTaxi color={color} size={size} />
  if (mode === 'walk') return <IconWalk size={size} />
  return <IconSubway color={color} size={size} />
}

// ─── Segment Bar ─────────────────────────────────────────────────────────────
// Walk slots always show "N분". Vehicle slots always show icon + "N분".
// minWidth guarantees content never gets clipped.
function SegmentBar({
  segments,
  scaleMax,
}: {
  segments: Segment[]
  scaleMax?: number
}) {
  // Merge consecutive walk/wait segments
  const merged: Segment[] = []
  for (const s of segments) {
    const last = merged[merged.length - 1]
    if (last && last.mode === 'walk' && s.mode === 'walk') {
      merged[merged.length - 1] = { ...last, duration: last.duration + s.duration }
    } else {
      merged.push({ ...s })
    }
  }

  const totalDuration = merged.reduce((a, s) => a + s.duration, 0)
  const scaleFrac = scaleMax ? Math.min(totalDuration / scaleMax, 1) : 1

  // px constants
  const ICON = 16   // icon badge size
  const GAP  = 2    // icon↔text gap
  const WALK_MIN = 26  // "99분" at 10px + 4px side pads ≈ 26px
  const VEH_MIN  = ICON + GAP + 24 + 8  // icon + gap + "99분" + side pads ≈ 50px

  return (
    <div style={{ width: '100%' }}>
      <div
        style={{
          display: 'flex', height: 22, borderRadius: 11, overflow: 'hidden',
          background: '#EEF1F4', width: `${scaleFrac * 100}%`,
        }}
      >
        {merged.map((s, i) => {
          const ariaLabel = `${s.label} ${s.duration}분`

          if (s.mode === 'walk') {
            return (
              <div
                key={i}
                title={ariaLabel}
                aria-label={ariaLabel}
                style={{
                  flexGrow: Math.max(s.duration, 2),
                  flexShrink: 1,
                  flexBasis: 0,
                  minWidth: WALK_MIN,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}
              >
                <span style={{
                  fontSize: 10, fontWeight: 500, color: '#6B7280',
                  fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
                }}>
                  {s.duration}분
                </span>
              </div>
            )
          }

          // Vehicle segment — always icon + "N분"
          return (
            <div
              key={i}
              title={ariaLabel}
              aria-label={ariaLabel}
              style={{
                flexGrow: Math.max(s.duration, 2),
                flexShrink: 1,
                flexBasis: 0,
                minWidth: VEH_MIN,
                background: s.color,
                borderRadius: 11,
                display: 'flex', alignItems: 'center',
                gap: GAP, paddingLeft: 4, paddingRight: 6,
                overflow: 'hidden',
              }}
            >
              <div style={{
                width: ICON, height: ICON, borderRadius: ICON / 2,
                background: 'white', border: `1.5px solid ${s.color}`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                <ModeIcon mode={s.mode} color={s.color} size={10} />
              </div>
              <span style={{
                fontSize: 11, fontWeight: 700, color: 'white',
                fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
              }}>
                {s.duration}분
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── Segment Summary ──────────────────────────────────────────────────────────
// Single-line vehicle-only summary. Walk slots handled by bar. Scrollable if overflow.
function SegmentSummary({ segments }: { segments: Segment[] }) {
  const vehicles = segments.filter(s => s.mode !== 'walk')

  if (vehicles.length === 0) return null

  return (
    <div style={{
      marginTop: 6,
      overflowX: 'auto',
      msOverflowStyle: 'none',
      scrollbarWidth: 'none',
    } as React.CSSProperties}>
      <div style={{
        display: 'inline-flex', alignItems: 'center',
        whiteSpace: 'nowrap', gap: 0,
      }}>
        {vehicles.map((v, i) => (
          <span key={i} style={{ display: 'inline-flex', alignItems: 'center' }}>
            {i > 0 && (
              <span style={{ color: '#C4CAD2', fontSize: 11, padding: '0 4px', flexShrink: 0 }}>›</span>
            )}
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 12, lineHeight: 1.6 }}>
              <ModeIcon mode={v.mode} color={v.color} size={12} />
              <span style={{ color: v.color, fontWeight: 600 }}>{v.label}</span>
              <span style={{ color: '#4B5563', fontVariantNumeric: 'tabular-nums' }}>{v.duration}분</span>
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}

// ─── Segment Steps ────────────────────────────────────────────────────────────
function SegmentSteps({ steps }: { steps: Step[] }) {
  return (
    <div style={{ marginTop: 14 }}>
      {steps.map((step, i) => {
        const isEnd = step.mode === 'end'
        const isLast = i === steps.length - 1
        return (
          <div key={i} style={{ display: 'flex', gap: 10 }}>
            {/* Icon + spine */}
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0 }}>
              <div style={{
                width: 22, height: 22, borderRadius: 11,
                background: isEnd ? '#E5E7EB' : step.color,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0,
              }}>
                {isEnd
                  ? <div style={{ width: 7, height: 7, borderRadius: 4, background: '#9CA3AF' }} />
                  : <ModeIcon mode={step.mode} color="white" size={12} />
                }
              </div>
              {!isLast && (
                <div style={{ width: 2, flex: 1, minHeight: 18, background: '#E5E7EB', margin: '2px 0' }} />
              )}
            </div>
            {/* Text */}
            <div style={{ flex: 1, paddingBottom: isLast ? 0 : 14 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 14, fontWeight: 600, color: '#111827', wordBreak: 'keep-all' }}>{step.station}</span>
                {step.line && (
                  <span style={{ fontSize: 12, color: '#6B7280' }}>{step.line}</span>
                )}
                {step.taxiTransfer && (
                  <span style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: '#FFF0EB', color: '#FF6B3D', fontWeight: 600, whiteSpace: 'nowrap' }}>여기서 택시 환승</span>
                )}
              </div>
              {step.dir && (
                <div style={{ fontSize: 12, color: '#6B7280', marginTop: 2, wordBreak: 'keep-all' }}>
                  {step.dir}
                  {step.minutes && <span> · {step.minutes}분</span>}
                  {step.crowd && <span style={{ color: '#E5484D' }}> · 혼잡 {step.crowd}분</span>}
                  {step.fare && <span> · {fmt(step.fare)}원</span>}
                </div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!on)}
      className={`w-14 h-7 rounded-full transition-all relative flex-shrink-0 ${on ? 'bg-[#22C55E]' : 'bg-[#D1D5DB]'}`}
    >
      <div className={`absolute top-1 w-5 h-5 rounded-full bg-white shadow transition-all ${on ? 'left-8' : 'left-1'}`} />
    </button>
  )
}

function NavHeader({ title, subtitle, onBack }: { title: string; subtitle?: string; onBack: () => void }) {
  return (
    <div className="flex items-center gap-3 px-5 pt-10 pb-3 bg-white border-b border-[#F3F4F6]">
      <button onClick={onBack} className="flex-shrink-0 w-8 h-8 flex items-center justify-center">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <path d="M11 14L6 9L11 4" stroke="#111827" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>
      <div>
        <div className="text-[17px] font-semibold text-[#111827] leading-tight">{title}</div>
        {subtitle && <div className="text-[12px] text-[#6B7280]">{subtitle}</div>}
      </div>
    </div>
  )
}

function WheelColumn<T extends string | number>({
  options,
  value,
  onChange,
  ariaLabel,
  widthClass = 'w-16',
}: {
  options: T[]
  value: T
  onChange: (value: T) => void
  ariaLabel: string
  widthClass?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const rowHeight = 36

  useEffect(() => {
    const index = options.indexOf(value)
    if (ref.current && index >= 0 && Math.abs(ref.current.scrollTop - index * rowHeight) > 1) {
      ref.current.scrollTo({ top: index * rowHeight, behavior: 'smooth' })
    }
  }, [options, value])

  return (
    <div
      ref={ref}
      role="listbox"
      aria-label={ariaLabel}
      className={`wheel-column relative h-[108px] snap-y snap-mandatory overflow-y-auto ${widthClass}`}
      onScroll={event => {
        const index = Math.max(0, Math.min(options.length - 1, Math.round(event.currentTarget.scrollTop / rowHeight)))
        if (options[index] !== value) onChange(options[index])
      }}
    >
      <div className="h-9" aria-hidden="true" />
      {options.map(option => (
        <button
          type="button"
          role="option"
          aria-selected={option === value}
          key={option}
          onClick={() => onChange(option)}
          className={`flex h-9 w-full snap-center items-center justify-center tabular-nums transition-all ${
            option === value
              ? 'text-[20px] font-bold text-[#182230]'
              : 'text-[15px] font-medium text-[#A7AFBC]'
          }`}
        >
          {typeof option === 'number' ? String(option).padStart(2, '0') : option}
        </button>
      ))}
      <div className="h-9" aria-hidden="true" />
    </div>
  )
}

// ─── Screens ──────────────────────────────────────────────────────────────────

function PlaceSuggestList({
  query,
  places,
  error,
  ready,
  onSelect,
}: {
  query: string
  places: Place[]
  error: boolean
  ready: boolean
  onSelect: (place: Place) => void
}) {
  if (!query.trim() || !ready) return null

  return (
    <div className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-xl border border-[#E5E7EB] bg-white shadow-sm">
      {error ? (
        <div className="px-4 py-3 text-[13px] text-[#9CA3AF]">검색에 실패했습니다</div>
      ) : places.length === 0 ? (
        <div className="px-4 py-3 text-[13px] text-[#9CA3AF]">검색 결과가 없습니다</div>
      ) : (
        places.map((place, index) => (
          <button
            type="button"
            key={`${place.name}-${place.lat}-${place.lng}-${index}`}
            onMouseDown={event => event.preventDefault()}
            onClick={() => onSelect(place)}
            className="w-full px-4 py-2.5 text-left hover:bg-[#F9FAFB]"
          >
            <div className="text-[14px] font-semibold text-[#111827]">{place.name}</div>
            <div className="text-[12px] text-[#9CA3AF]">{place.address}</div>
          </button>
        ))
      )}
    </div>
  )
}

function DepartSheet({
  initialIso,
  onClose,
  onConfirm,
}: {
  initialIso: string | null
  onClose: () => void
  onConfirm: (iso: string) => void
}) {
  const initial = seoulParts(initialIso)
  const today = seoulParts(null)
  const [dayOffset, setDayOffset] = useState(() => {
    const a = Date.UTC(today.year, today.month - 1, today.day)
    const b = Date.UTC(initial.year, initial.month - 1, initial.day)
    const diff = Math.round((b - a) / 86400000)
    return Math.min(7, Math.max(0, diff))
  })
  const [hour, setHour] = useState(Number.isFinite(initial.hour) ? initial.hour : today.hour)
  const [minute, setMinute] = useState(Math.round((Number.isFinite(initial.minute) ? initial.minute : today.minute) / 10) * 10 % 60)
  const days = Array.from({ length: 8 }, (_, offset) => {
    const utc = new Date(Date.UTC(today.year, today.month - 1, today.day + offset))
    const label = offset === 0 ? '오늘' : offset === 1 ? '내일' : `${utc.getUTCMonth() + 1}/${utc.getUTCDate()}`
    return { offset, label, year: utc.getUTCFullYear(), month: utc.getUTCMonth() + 1, day: utc.getUTCDate() }
  })
  const selected = days[dayOffset]

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/40" onClick={onClose}>
      <div
        className="w-full max-w-[430px] rounded-t-2xl bg-white px-5 pb-6 pt-4"
        onClick={event => event.stopPropagation()}
      >
        <div className="mb-4 text-[15px] font-semibold text-[#111827]">출발 시각 설정</div>
        <div className="mb-2 text-[12px] font-medium text-[#6B7280]">날짜</div>
        <div className="mb-4 flex flex-wrap gap-2">
          {days.map(day => (
            <button
              key={day.offset}
              type="button"
              onClick={() => setDayOffset(day.offset)}
              className={`rounded-full px-3 py-1.5 text-[12px] font-semibold ${
                dayOffset === day.offset ? 'bg-[#2F7BF6] text-white' : 'bg-[#F2F4F7] text-[#687386]'
              }`}
            >
              {day.label}
            </button>
          ))}
        </div>
        <div className="mb-2 text-[12px] font-medium text-[#6B7280]">시간 (10분 단위)</div>
        <div className="mb-4 flex items-center gap-2">
          <select
            value={hour}
            onChange={event => setHour(Number(event.target.value))}
            className="flex-1 rounded-xl border border-[#E5E7EB] bg-white px-3 py-2.5 text-[14px] text-[#374151] outline-none"
          >
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>{String(h).padStart(2, '0')}시</option>
            ))}
          </select>
          <select
            value={minute}
            onChange={event => setMinute(Number(event.target.value))}
            className="flex-1 rounded-xl border border-[#E5E7EB] bg-white px-3 py-2.5 text-[14px] text-[#374151] outline-none"
          >
            {[0, 10, 20, 30, 40, 50].map(m => (
              <option key={m} value={m}>{String(m).padStart(2, '0')}분</option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={() => onConfirm(buildSeoulIso(selected.year, selected.month, selected.day, hour, minute))}
          className="h-12 w-full rounded-xl bg-[#2F7BF6] text-[15px] font-semibold text-white"
        >
          이 시각으로 설정
        </button>
      </div>
    </div>
  )
}

function HomeScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const trip = useTrip()
  const sp = useSp()
  const [originQuery, setOriginQuery] = useState(trip.origin?.name ?? '')
  const [destinationQuery, setDestinationQuery] = useState(trip.destination?.name ?? '')
  const [origin, setOrigin] = useState<Place | null>(trip.origin)
  const [destination, setDestination] = useState<Place | null>(trip.destination)
  const [activeField, setActiveField] = useState<'origin' | 'destination' | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const searchBoxRef = useRef<HTMLDivElement>(null)

  const originSearch = usePlaceSearch(activeField === 'origin' ? originQuery : '')
  const destinationSearch = usePlaceSearch(activeField === 'destination' ? destinationQuery : '')
  const canSearch = origin !== null && destination !== null

  useEffect(() => {
    const closeOnOutside = (event: MouseEvent) => {
      if (!searchBoxRef.current?.contains(event.target as Node)) {
        setActiveField(null)
      }
    }
    document.addEventListener('mousedown', closeOnOutside)
    return () => document.removeEventListener('mousedown', closeOnOutside)
  }, [])

  const swapPlaces = () => {
    setOriginQuery(destinationQuery)
    setDestinationQuery(originQuery)
    setOrigin(destination)
    setDestination(origin)
    setActiveField(null)
  }

  return (
    <div className="flex min-h-dvh flex-col bg-white">
      <div className="flex-1 overflow-y-auto pb-[88px]">
        <div className="flex flex-shrink-0 flex-col items-center px-6 pt-14">
          <div className="text-[32px] font-bold text-[#2F7BF6] tracking-tight">OPTI</div>
          <div className="text-[13px] text-[#6B7280] mt-1">대중교통·택시 환승경로 탐색 서비스</div>
        </div>

        <div className="flex flex-shrink-0 flex-col gap-0 px-5 pt-8">
          <div ref={searchBoxRef} className="relative z-20 bg-white border border-[#E5E7EB] rounded-2xl shadow-sm overflow-visible">
            <button
              type="button"
              onClick={swapPlaces}
              className="absolute right-3 top-1/2 z-30 -translate-y-1/2 text-[#9CA3AF]"
              aria-label="출발지와 도착지 바꾸기"
            >
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <path d="M9 3v12M5 7l4-4 4 4M5 11l4 4 4-4" stroke="#9CA3AF" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
            <div className="relative flex items-center gap-3 px-4 py-3.5 pr-12 border-b border-[#F3F4F6]">
              <div className="w-2.5 h-2.5 rounded-full border-2 border-[#9CA3AF] flex-shrink-0" />
              <input
                className="flex-1 text-[14px] text-[#374151] outline-none placeholder-[#9CA3AF] bg-transparent"
                placeholder="출발지를 입력하세요"
                value={originQuery}
                onFocus={() => setActiveField('origin')}
                onChange={event => {
                  setOriginQuery(event.target.value)
                  setOrigin(null)
                }}
              />
              {activeField === 'origin' && (
                <PlaceSuggestList
                  query={originQuery}
                  places={originSearch.places}
                  error={originSearch.error}
                  ready={originSearch.ready}
                  onSelect={place => {
                    setOrigin(place)
                    setOriginQuery(place.name)
                    setActiveField(null)
                  }}
                />
              )}
            </div>
            <div className="relative flex items-center gap-3 px-4 py-3.5 pr-12">
              <div className="w-2.5 h-2.5 rounded-full bg-[#2F7BF6] flex-shrink-0" />
              <input
                className="flex-1 text-[14px] text-[#374151] outline-none placeholder-[#9CA3AF] bg-transparent"
                placeholder="도착지를 입력하세요"
                value={destinationQuery}
                onFocus={() => setActiveField('destination')}
                onChange={event => {
                  setDestinationQuery(event.target.value)
                  setDestination(null)
                }}
              />
              {activeField === 'destination' && (
                <PlaceSuggestList
                  query={destinationQuery}
                  places={destinationSearch.places}
                  error={destinationSearch.error}
                  ready={destinationSearch.ready}
                  onSelect={place => {
                    setDestination(place)
                    setDestinationQuery(place.name)
                    setActiveField(null)
                  }}
                />
              )}
            </div>
          </div>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => trip.setDepartNow()}
              className={`flex-1 h-9 rounded-full text-[12px] font-semibold ${
                trip.departMode === 'now' ? 'bg-[#EAF2FF] text-[#2F7BF6]' : 'bg-[#F2F4F7] text-[#687386]'
              }`}
            >
              지금 출발
            </button>
            <button
              type="button"
              onClick={() => {
                if (trip.departMode === 'scheduled' && trip.scheduledDepart) {
                  setSheetOpen(true)
                  return
                }
                setSheetOpen(true)
              }}
              className={`flex-1 h-9 rounded-full text-[12px] font-semibold ${
                trip.departMode === 'scheduled' ? 'bg-[#EAF2FF] text-[#2F7BF6]' : 'bg-[#F2F4F7] text-[#687386]'
              }`}
            >
              {trip.departMode === 'scheduled' && trip.scheduledDepart
                ? departChipLabel(trip.scheduledDepart)
                : '출발 시각 설정'}
            </button>
          </div>
          {sheetOpen && (
            <DepartSheet
              initialIso={trip.scheduledDepart}
              onClose={() => setSheetOpen(false)}
              onConfirm={iso => {
                trip.setScheduledDepart(iso)
                setSheetOpen(false)
              }}
            />
          )}
          <button onClick={() => onNav('search-input')} className="flex items-center gap-1.5 mt-3 text-[13px] text-[#2F7BF6] font-medium">
            <span>📍</span> 내 위치로 출발
          </button>
          <button
            type="button"
            disabled={!canSearch}
            onClick={() => {
              if (!origin || !destination) return
              trip.setOrigin(origin)
              trip.setDestination(destination)
              trip.startAnalyze(origin, destination)
              onNav('reservation')
            }}
            className={`w-full py-4 mt-4 rounded-xl text-[16px] font-semibold ${
              canSearch ? 'bg-[#2F7BF6] text-white' : 'bg-[#E5E7EB] text-[#9CA3AF]'
            }`}
          >
            길찾기
          </button>
        </div>

        <div className="flex flex-shrink-0 flex-col gap-3 px-5 pt-8 pb-8">
          <button
            onClick={() => onNav('sp-setup')}
            className="flex items-center justify-between px-4 py-3.5 bg-white border border-[#E5E7EB] rounded-2xl text-left shadow-sm"
          >
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 bg-[#EBF2FF] rounded-xl flex items-center justify-center text-base">📋</div>
              <div>
                <div className="text-[14px] font-semibold text-[#111827]">개인 맞춤형 시간가치(VOT) 설정하기</div>
                <div className="text-[12px] text-[#9CA3AF] mt-0.5">
                  {(() => {
                    const effective = getEffectiveParams({
                      manualVot: sp.manualVot,
                      manualParams: sp.manualParams,
                      routeParams: sp.routeParams,
                      usePersonal: sp.usePersonal,
                    })
                    if (effective.sources.vot === 'default') return '나의 VOT · 수단 선호 추정'
                    const tag = effective.sources.vot === 'manual' ? '직접 입력' : '설문 추정'
                    return `내 시간가치 ${fmt(Math.round(effective.vot))}원/분 적용 중 (${tag})`
                  })()}
                </div>
              </div>
            </div>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6 12L10 8L6 4" stroke="#9CA3AF" strokeWidth="1.5" strokeLinecap="round"/></svg>
          </button>
          <button
            onClick={() => onNav('sp-profile')}
            className="flex items-center justify-between px-4 py-3.5 bg-white border border-[#E5E7EB] rounded-2xl text-left shadow-sm"
          >
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 bg-[#F3F4F6] rounded-xl flex items-center justify-center text-base">👤</div>
              <div>
                <div className="text-[14px] font-semibold text-[#111827]">내 프로필 보기</div>
                <div className="text-[12px] text-[#9CA3AF] mt-0.5">파라미터 확인 · 개인화 적용</div>
              </div>
            </div>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6 12L10 8L6 4" stroke="#9CA3AF" strokeWidth="1.5" strokeLinecap="round"/></svg>
          </button>
        </div>
      </div>

      <div className="fixed bottom-0 left-0 right-0 mx-auto w-full max-w-[430px] border-t border-[#F3F4F6] px-8 pt-3 pb-5 flex justify-around bg-white">
        {[
          { icon: '🗺️', label: '홈', active: true, screen: 'home' },
          { icon: '📋', label: 'SP 설문', active: false, screen: 'sp-setup' },
          { icon: '👤', label: '프로필', active: false, screen: 'sp-profile' },
        ].map(({ icon, label, active, screen }) => (
          <button key={label} onClick={() => onNav(screen as Screen)} className="flex flex-col items-center gap-1">
            <span className="text-xl">{icon}</span>
            <span className={`text-[10px] font-medium ${active ? 'text-[#2F7BF6]' : 'text-[#9CA3AF]'}`}>{label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function SPSetupScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const sp = useSp()
  const [age, setAge] = useState(sp.displayAge || '20대')
  const [purpose, setPurpose] = useState(sp.displayPurpose || '업무/비즈니스')
  const [vot, setVot] = useState(sp.manualVot != null ? String(sp.manualVot) : '400')
  const [qCount, setQCount] = useState(sp.questionCount === 6 || sp.questionCount === 24 ? sp.questionCount : 12)
  const [votError, setVotError] = useState('')

  const handleStart = async () => {
    const votDirect = parseManualVot(vot)
    if (votDirect == null) {
      setVotError(`${VOT_MIN}~${fmt(VOT_MAX)}원/분 사이 숫자만 입력해 주세요`)
      return
    }
    setVotError('')
    try {
      sp.setManualVot(null)
      await sp.startSurvey(age, purpose, votDirect, qCount)
      onNav('sp-question')
    } catch {
      /* surveyError is shown below */
    }
  }

  const handleSkip = () => {
    const votDirect = parseManualVot(vot)
    if (vot.trim() && votDirect == null) {
      setVotError(`${VOT_MIN}~${fmt(VOT_MAX)}원/분 사이 숫자만 입력해 주세요`)
      return
    }
    if (votDirect != null) sp.setManualVot(votDirect)
    onNav('home')
  }

  return (
    <div className="flex flex-col h-full bg-white">
      <NavHeader title="SP 개인화 설문" subtitle="진술선호 설문으로 나의 파라미터를 추정합니다" onBack={() => onNav('home')} />
      <div className="flex-1 overflow-y-auto px-5 py-5 space-y-6">
        {/* Age */}
        <div>
          <div className="text-[13px] text-[#374151] font-medium mb-2.5">연령대</div>
          <div className="flex flex-wrap gap-2">
            {['10대', '20대', '30대', '40대', '50대 이상'].map(a => (
              <button key={a} onClick={() => setAge(a)}
                className={`px-5 py-2 rounded-full text-[13px] font-medium border transition-all ${age === a ? 'bg-[#2F7BF6] text-white border-[#2F7BF6]' : 'bg-white text-[#374151] border-[#E5E7EB]'}`}
              >{a}</button>
            ))}
          </div>
        </div>

        {/* Purpose */}
        <div>
          <div className="text-[13px] text-[#374151] font-medium mb-2.5">통행 목적</div>
          <div className="flex flex-wrap gap-2">
            {['통근/통학', '쇼핑/여가', '업무/비즈니스', '의료/병원', '기타'].map(p => (
              <button key={p} onClick={() => setPurpose(p)}
                className={`px-4 py-2 rounded-full text-[13px] font-medium border transition-all ${purpose === p ? 'bg-[#2F7BF6] text-white border-[#2F7BF6]' : 'bg-white text-[#374151] border-[#E5E7EB]'}`}
              >{p}</button>
            ))}
          </div>
        </div>

        {/* Direct VOT */}
        <div>
          <div className="text-[13px] text-[#374151] font-medium mb-1">직접 VOT</div>
          <div className="text-[11px] text-[#9CA3AF] mb-2.5">1분 절약을 위해 지불할 의향이 있는 금액</div>
          <div className="flex items-center gap-2 border border-[#E5E7EB] rounded-xl overflow-hidden">
            <input
              type="text"
              inputMode="numeric"
              value={vot}
              onChange={e => { setVot(e.target.value.replace(/[^\d.]/g, '')); setVotError('') }}
              className="flex-1 px-4 py-3 text-[15px] font-medium text-[#374151] outline-none bg-transparent"
            />
            <span className="pr-4 text-[13px] text-[#9CA3AF]">원/분</span>
          </div>
          {votError && <div className="mt-2 text-[12px] text-[#EF4444]">{votError}</div>}
        </div>

        {/* Question count */}
        <div>
          <div className="text-[13px] text-[#374151] font-medium mb-2.5">문항 수</div>
          <div className="grid grid-cols-3 gap-2">
            {[{ n: 6, sub: '약 3분' }, { n: 12, sub: '약 6분' }, { n: 24, sub: '약 12분' }].map(({ n, sub }) => (
              <button key={n} onClick={() => setQCount(n)}
                className={`py-3 rounded-xl border text-center transition-all ${qCount === n ? 'border-[#2F7BF6] bg-[#EBF2FF]' : 'border-[#E5E7EB] bg-white'}`}
              >
                <div className={`text-[14px] font-semibold ${qCount === n ? 'text-[#2F7BF6]' : 'text-[#374151]'}`}>{n}문항</div>
                <div className="text-[11px] text-[#9CA3AF]">{sub}</div>
              </button>
            ))}
          </div>
        </div>

        <div className="flex gap-3 pb-4">
          <button onClick={handleSkip} className="flex-1 py-4 border border-[#E5E7EB] rounded-xl text-[14px] text-[#6B7280] font-medium bg-white">
            건너뛰기
          </button>
          <button
            type="button"
            disabled={sp.surveyBusy}
            onClick={() => { void handleStart() }}
            className="flex-[2] py-4 bg-[#2F7BF6] rounded-xl text-[14px] text-white font-semibold disabled:opacity-60"
          >
            설문 시작
          </button>
        </div>
        {sp.surveyError && (
          <div className="text-[12px] text-[#EF4444] pb-2">{sp.surveyError}</div>
        )}
      </div>
    </div>
  )
}

// ─── SP Question Types ────────────────────────────────────────────────────────
type SpOption = {
  label: string; time: number; cost: number; walkMin: number; transfers: number; crowdMin?: number;
  segments: Segment[]
}
type SpQuestion = { block: string; total: number; title: string; isTrap?: boolean; options: SpOption[] }

const SP_QUESTIONS: SpQuestion[] = [
  {
    block: 'A', total: 12,
    title: '요금과 소요시간이 다른 세 경로 중 실제로 이용할 경로를 고르세요.',
    options: [
      {
        label: 'A', time: 124, cost: 3450, walkMin: 15, transfers: 3,
        segments: [
          { label: '도보', duration: 12, color: '#9CA3AF', mode: 'walk' },
          { label: '1호선', duration: 55, color: '#2F7BF6', mode: 'transit' },
          { label: '도보', duration: 4, color: '#9CA3AF', mode: 'walk' },
          { label: '3호선', duration: 45, color: '#F97316', mode: 'transit' },
          { label: '도보', duration: 8, color: '#9CA3AF', mode: 'walk' },
        ] as Segment[],
      },
      {
        label: 'B', time: 97, cost: 21950, walkMin: 8, transfers: 2,
        segments: [
          { label: '도보', duration: 5, color: '#9CA3AF', mode: 'walk' },
          { label: '2호선', duration: 18, color: '#2F7BF6', mode: 'transit' },
          { label: 'GTX-A', duration: 31, color: '#8B5CF6', mode: 'transit' },
          { label: '택시', duration: 40, color: '#FF6B3D', mode: 'taxi' },
          { label: '도보', duration: 3, color: '#9CA3AF', mode: 'walk' },
        ] as Segment[],
      },
      {
        label: 'C', time: 49, cost: 69150, walkMin: 0, transfers: 0,
        segments: [
          { label: '택시', duration: 49, color: '#FF6B3D', mode: 'taxi' },
        ] as Segment[],
      },
    ],
  },
  {
    block: 'A', total: 12,
    title: '아래 두 경로 중 어느 쪽을 더 선호하시나요?',
    isTrap: false,
    options: [
      {
        label: 'A', time: 82, cost: 5550, walkMin: 7, transfers: 2,
        segments: [
          { label: '도보', duration: 7, color: '#9CA3AF', mode: 'walk' },
          { label: '1호선', duration: 38, color: '#2F7BF6', mode: 'transit' },
          { label: '9호선', duration: 18, color: '#EAB308', mode: 'transit' },
          { label: '도보', duration: 14, color: '#9CA3AF', mode: 'walk' },
        ] as Segment[],
      },
      {
        label: 'B', time: 67, cost: 13950, walkMin: 4, transfers: 2,
        segments: [
          { label: '도보', duration: 4, color: '#9CA3AF', mode: 'walk' },
          { label: 'GTX-A', duration: 12, color: '#8B5CF6', mode: 'transit' },
          { label: '3호선', duration: 21, color: '#F97316', mode: 'transit' },
          { label: '택시', duration: 13, color: '#FF6B3D', mode: 'taxi' },
        ] as Segment[],
      },
      {
        label: 'C', time: 68, cost: 34230, walkMin: 6, transfers: 1,
        segments: [
          { label: '도보', duration: 6, color: '#9CA3AF', mode: 'walk' },
          { label: 'GTX-A', duration: 18, color: '#8B5CF6', mode: 'transit' },
          { label: '택시', duration: 40, color: '#FF6B3D', mode: 'taxi' },
        ] as Segment[],
      },
    ],
  },
  {
    block: 'A', total: 12,
    title: '확인 문항: 가장 짧은 경로를 선택해주세요.',
    isTrap: true,
    options: [
      {
        label: 'A', time: 90, cost: 8000, walkMin: 10, transfers: 2,
        segments: [
          { label: '도보', duration: 10, color: '#9CA3AF', mode: 'walk' },
          { label: '지하철', duration: 70, color: '#2F7BF6', mode: 'transit' },
          { label: '도보', duration: 10, color: '#9CA3AF', mode: 'walk' },
        ] as Segment[],
      },
      {
        label: 'B', time: 30, cost: 25000, walkMin: 0, transfers: 0,
        segments: [
          { label: '택시', duration: 30, color: '#FF6B3D', mode: 'taxi' },
        ] as Segment[],
      },
      {
        label: 'C', time: 60, cost: 4500, walkMin: 8, transfers: 1,
        segments: [
          { label: '도보', duration: 8, color: '#9CA3AF', mode: 'walk' },
          { label: '지하철', duration: 45, color: '#F97316', mode: 'transit' },
          { label: '도보', duration: 7, color: '#9CA3AF', mode: 'walk' },
        ] as Segment[],
      },
    ],
  },
]

function SPQuestionScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const sp = useSp()
  const [qIdx, setQIdx] = useState(0)
  const cards = sp.survey?.cards ?? []
  const selected = qIdx in sp.answers ? sp.answers[qIdx] : null
  const card = cards[qIdx]
  const totalQ = cards.length
  const isLast = totalQ > 0 && qIdx >= totalQ - 1
  const options = (card?.alternatives ?? []).map((alt, index) => ({
    label: String.fromCharCode(65 + index),
    altIndex: alt.index ?? index,
    time: alt.total_min,
    cost: alt.cost,
    walkMin: alt.walk_min,
    transfers: alt.transfers,
    crowdMin: (alt.legs || []).reduce((sum, leg) => sum + (Number(leg.crowded_minutes) || 0), 0),
    segments: altToSegments(alt),
  }))
  const scaleMax = Math.max(1, ...options.map(o => o.segments.reduce((a, s) => a + s.duration, 0)))

  useEffect(() => {
    if (!sp.survey) onNav('sp-setup')
  }, [sp.survey, onNav])

  const handleNext = () => {
    if (selected === null) return
    if (isLast) {
      onNav('sp-complete')
      void sp.runEstimate()
      return
    }
    setQIdx(i => i + 1)
  }

  const handleKey = (e: React.KeyboardEvent, altIndex: number) => {
    if (e.key === 'Enter' || e.key === ' ') { sp.setAnswer(qIdx, altIndex); e.preventDefault() }
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
      const idx = options.findIndex(o => o.altIndex === altIndex)
      const next = options[(idx + 1) % options.length]
      if (next) sp.setAnswer(qIdx, next.altIndex)
    }
    if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
      const idx = options.findIndex(o => o.altIndex === altIndex)
      const prev = options[(idx - 1 + options.length) % options.length]
      if (prev) sp.setAnswer(qIdx, prev.altIndex)
    }
  }

  if (!card) {
    return (
      <div className="flex flex-col h-full bg-[#F8F9FB]">
        <div style={{ height: 56, background: 'white', display: 'flex', alignItems: 'center', paddingLeft: 12, paddingRight: 18, paddingTop: 24, gap: 4, flexShrink: 0 }}>
          <button onClick={() => onNav('sp-setup')} style={{ width: 36, height: 36, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M11 14L6 9L11 4" stroke="#111827" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-[#F8F9FB]">
      {/* Header */}
      <div style={{ height: 56, background: 'white', display: 'flex', alignItems: 'center', paddingLeft: 12, paddingRight: 18, paddingTop: 24, gap: 4, flexShrink: 0 }}>
        <button
          onClick={() => qIdx > 0 ? setQIdx(i => i - 1) : onNav('sp-setup')}
          style={{ width: 36, height: 36, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}
        >
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M11 14L6 9L11 4" stroke="#111827" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </button>
        <span style={{ fontSize: 17, fontWeight: 700, color: '#111827' }}>설문</span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 14, color: '#6B7280', fontVariantNumeric: 'tabular-nums' }}>{qIdx + 1} / {totalQ}</span>
      </div>

      {/* Progress bar */}
      <div style={{ height: 3, background: '#EEF1F4', flexShrink: 0 }}>
        <div style={{ height: '100%', background: '#2F7BF6', width: `${((qIdx + 1) / Math.max(totalQ, 1)) * 100}%`, transition: 'width 0.3s ease' }} />
      </div>

      <div className="flex-1 overflow-y-auto" style={{ padding: '16px 16px 8px' }}>
        {/* Prompt */}
        <div style={{ marginBottom: 12 }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: '#2F7BF6' }}>Q{qIdx + 1}</span>
          <p style={{ fontSize: 15, fontWeight: 600, color: '#111827', lineHeight: 1.55, marginTop: 4, wordBreak: 'keep-all' }}>
            {sp.survey?.scenario_text}
          </p>
        </div>

        {/* Legend */}
        <div style={{ display: 'flex', gap: 12, marginBottom: 14, flexWrap: 'wrap' }}>
          {[
            { color: '#EEF1F4', border: '#9CA3AF', label: '도보' },
            { color: '#2F7BF6', border: undefined, label: '지하철' },
            { color: '#FF6B3D', border: undefined, label: '택시' },
          ].map(({ color, border, label }) => (
            <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <div style={{ width: 8, height: 8, borderRadius: 4, background: color, border: border ? `1.5px solid ${border}` : undefined, flexShrink: 0 }} />
              <span style={{ fontSize: 12, color: '#6B7280' }}>{label}</span>
            </div>
          ))}
        </div>

        {/* Choice group */}
        <div role="radiogroup" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {options.map((opt) => {
            const isSelected = selected === opt.altIndex

            return (
              <div
                key={opt.label}
                role="radio"
                aria-checked={isSelected}
                tabIndex={0}
                onClick={() => sp.setAnswer(qIdx, opt.altIndex)}
                onKeyDown={(e) => handleKey(e, opt.altIndex)}
                style={{
                  padding: 16, borderRadius: 14, cursor: 'pointer',
                  border: isSelected ? '2px solid #2F7BF6' : '1px solid #E5E7EB',
                  background: isSelected ? '#F5F9FF' : 'white',
                  position: 'relative', outline: 'none',
                  transition: 'border-color 0.15s, background 0.15s',
                  marginLeft: isSelected ? 0 : 1,
                  marginRight: isSelected ? 0 : 1,
                }}
              >
                {/* Check circle */}
                {isSelected && (
                  <div style={{ position: 'absolute', top: 12, right: 12, width: 20, height: 20, borderRadius: 10, background: '#2F7BF6', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <svg width="11" height="8" viewBox="0 0 11 8" fill="none">
                      <path d="M1 4L4 7L10 1" stroke="white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  </div>
                )}

                {/* Row 1: badge + time + cost */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                  <div style={{
                    width: 24, height: 24, borderRadius: 12, flexShrink: 0,
                    background: isSelected ? '#2F7BF6' : '#F2F4F7',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 13, fontWeight: 700,
                    color: isSelected ? 'white' : '#4B5563',
                    transition: 'background 0.15s, color 0.15s',
                  }}>
                    {opt.label}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'baseline', flex: 1, gap: 4 }}>
                    <span style={{ fontSize: 24, fontWeight: 800, color: '#111827', letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>{opt.time}</span>
                    <span style={{ fontSize: 15, fontWeight: 600, color: '#111827' }}>분</span>
                  </div>
                  <span style={{ fontSize: 17, fontWeight: 700, color: '#374151', fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>{fmt(opt.cost)}원</span>
                </div>

                {/* Row 2: segment bar — compact variant, scaleMax for proportional width */}
                <SegmentBar segments={opt.segments} scaleMax={scaleMax} />

                {/* Row 2b: segment summary */}
                <div style={{ marginBottom: 10 }}>
                  <SegmentSummary segments={opt.segments} />
                </div>

                {/* Row 3: attribute chips — computed directly from segments, not from opt.walkMin */}
                {(() => {
                  const walkSumMin = opt.segments.filter(s => s.mode === 'walk').reduce((a, s) => a + s.duration, 0)
                  const vehicleSegs = opt.segments.filter(s => s.mode !== 'walk')
                  const transitSegs = vehicleSegs.filter(s => s.mode !== 'taxi')
                  const hasTaxi = vehicleSegs.some(s => s.mode === 'taxi')
                  const hasTransit = transitSegs.length > 0
                  const transferCount = opt.transfers ?? (Math.max(0, transitSegs.length - 1) + (hasTaxi && hasTransit ? 1 : 0))
                  return (
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      <span style={{ height: 22, display: 'inline-flex', alignItems: 'center', padding: '0 8px', borderRadius: 4, background: '#F2F4F7', fontSize: 12, color: '#4B5563', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
                        도보 {walkSumMin}분
                      </span>
                      <span style={{ height: 22, display: 'inline-flex', alignItems: 'center', padding: '0 8px', borderRadius: 4, background: '#F2F4F7', fontSize: 12, color: '#4B5563', whiteSpace: 'nowrap' }}>
                        환승 {transferCount}회
                      </span>
                      {(opt.crowdMin ?? 0) > 0 && (
                        <span style={{ height: 22, display: 'inline-flex', alignItems: 'center', padding: '0 8px', borderRadius: 4, background: '#FEF2F2', fontSize: 12, color: '#E5484D', whiteSpace: 'nowrap' }}>
                          혼잡 {opt.crowdMin}분
                        </span>
                      )}
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      </div>

      {/* Next button */}
      <div style={{ padding: '10px 18px 20px', background: '#F8F9FB', flexShrink: 0 }}>
        <button
          onClick={handleNext}
          disabled={selected === null}
          style={{
            width: '100%', height: 52, borderRadius: 12, border: 'none',
            fontSize: 16, fontWeight: 700, cursor: selected !== null ? 'pointer' : 'default',
            background: selected !== null ? '#2F7BF6' : '#E5E7EB',
            color: selected !== null ? 'white' : '#9AA3AF',
            transition: 'background 0.2s, color 0.2s',
          }}
        >
          {isLast ? '설문 완료' : '다음 문항'}
        </button>
      </div>
    </div>
  )
}

function SPCompleteScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const sp = useSp()
  const loading = sp.estimateBusy || (!sp.estimateResult && !sp.estimateError)
  const params = sp.routeParams
  const weights = sp.profile?.weights_relative_to_uncrowded_subway || {}
  const rows = params ? [
    { label: '나의 시간가치', value: `${fmt(Math.round(params.vot))}원/분`, color: '#2F7BF6' },
    { label: '버스 체감', value: feelLine('버스', weights.bus ?? params.alpha_bus), color: '#374151' },
    { label: '택시 체감', value: feelLine('택시', weights.taxi ?? params.delta_taxi), color: '#FF6B3D' },
    { label: '도보 체감', value: feelLine('도보', weights.walk ?? params.gamma_walk), color: '#374151' },
    { label: '환승 1회', value: `${(weights.transfer_min ?? params.transfer_penalty).toFixed(1)}분`, color: '#22C55E' },
  ] : []

  return (
    <div className="flex flex-col h-full bg-white">
      <NavHeader title="설문 완료" onBack={() => onNav('sp-question')} />
      <div className="flex-1 flex flex-col items-center justify-center px-6">
        <div className={`w-20 h-20 rounded-full flex items-center justify-center mb-6 ${loading || sp.estimateError ? 'bg-[#EBF2FF]' : 'bg-[#DCFCE7]'}`}>
          {loading ? (
            <div className="w-8 h-8 rounded-full border-4 border-[#BFDBFE] border-t-[#2F7BF6] animate-spin" />
          ) : sp.estimateError ? (
            <span className="text-[22px]">!</span>
          ) : (
            <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
              <path d="M7 18L14 25L29 10" stroke="#22C55E" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
        </div>
        <div className="text-[22px] font-bold text-[#111827] mb-2">
          {loading ? '추정 중' : sp.estimateError ? '잠시 후 다시 시도해 주세요' : '추정 완료'}
        </div>
        <div className="text-[13px] text-[#6B7280] mb-8 text-center">
          {loading
            ? '응답을 반영하고 있습니다'
            : sp.estimateError
              ? sp.estimateError
              : sp.profile?.soft_mix
                ? '응답이 일관되지 않아 기본값과 섞어 반영했어요'
                : '응답으로 나의 시간가치를 반영했습니다'}
        </div>

        {!loading && !sp.estimateError && (
          <div className="w-full bg-white border border-[#E5E7EB] rounded-2xl overflow-hidden mb-6">
            <div className="px-4 py-3 border-b border-[#F3F4F6]">
              <div className="text-[12px] font-semibold text-[#6B7280] uppercase tracking-wider">나의 설정</div>
            </div>
            {rows.map(row => (
              <div key={row.label} className="flex items-center justify-between px-4 py-3 border-b border-[#F9FAFB] last:border-0 gap-3">
                <span className="text-[13px] text-[#374151] flex-shrink-0">{row.label}</span>
                <span className="text-[13px] font-bold text-right" style={{ color: row.color }}>{row.value}</span>
              </div>
            ))}
          </div>
        )}

        {sp.estimateError ? (
          <button onClick={() => { void sp.runEstimate() }} className="w-full py-4 bg-[#22C55E] text-white rounded-xl font-semibold text-[15px]">
            다시 시도
          </button>
        ) : (
          <button
            disabled={loading}
            onClick={() => onNav('sp-profile')}
            className="w-full py-4 bg-[#22C55E] text-white rounded-xl font-semibold text-[15px] disabled:opacity-60"
          >
            내 프로필에서 확인하기
          </button>
        )}
      </div>
    </div>
  )
}

function RankingWeightSection() {
  const sp = useSp()
  const trip = useTrip()
  const betas = sp.rankingBetas
  const split = conditionShare(betas.gc, betas.knee)
  const restPct = Math.round(split.rest * 100)
  const rawRest = 1 - betas.gc - betas.knee
  const limits = trip.appliedLimits || trip.pendingLimits
  const axes: Array<'time' | 'cost' | 'transfer'> = []
  if (limits?.arrive_by || limits?.max_time_min != null) axes.push('time')
  if (limits?.max_cost_krw != null) axes.push('cost')
  if (limits?.max_transfers != null) axes.push('transfer')
  const previewAxes = axes.length ? axes : (['time', 'cost', 'transfer'] as const)
  const weights = previewWeights(betas, [...previewAxes], limits?.importance)
  const setPct = (key: 'gc' | 'knee', pct: number) => {
    const next = Math.max(0, Math.min(BETA_MAX * 100, Math.round(pct))) / 100
    sp.setRankingBetas({ ...betas, [key]: next })
  }

  return (
    <div className="rounded-xl bg-[#F9FAFB] px-3 py-3 space-y-3">
      <div>
        <div className="text-[13px] font-semibold text-[#111827]">추천 기준 비중</div>
        <div className="mt-0.5 text-[11px] text-[#9CA3AF]">가성비와 균형점 비중을 조절하면, 나머지는 내가 정한 조건에 쓰여요.</div>
      </div>
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[13px] font-semibold text-[#111827]">가성비</span>
          <span className="text-[13px] font-bold text-[#374151]">{Math.round(betas.gc * 100)}%</span>
        </div>
        <input
          type="range"
          min={0}
          max={60}
          step={1}
          value={Math.round(betas.gc * 100)}
          aria-label="가성비 비중"
          onChange={event => setPct('gc', Number(event.target.value))}
          className="w-full"
        />
      </div>
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[13px] font-semibold text-[#111827]">균형점</span>
          <span className="text-[13px] font-bold text-[#374151]">{Math.round(betas.knee * 100)}%</span>
        </div>
        <input
          type="range"
          min={0}
          max={60}
          step={1}
          value={Math.round(betas.knee * 100)}
          aria-label="균형점 비중"
          onChange={event => setPct('knee', Number(event.target.value))}
          className="w-full"
        />
      </div>
      <div className="text-[12px] text-[#374151]">
        내 조건(시간·요금·환승) {restPct}%
      </div>
      {rawRest < 0.1 - 1e-9 && (
        <div className="text-[11px] text-[#B45309]">가성비와 균형점 합이 90%를 넘어, 내 조건 몫을 10%로 맞춰 적용해요.</div>
      )}
      <div className="text-[12px] font-medium text-[#111827]">{previewLine(weights)}</div>
      <button
        type="button"
        onClick={() => sp.resetRankingBetas()}
        className="w-full py-2.5 border border-[#E5E7EB] rounded-xl text-[13px] font-medium text-[#6B7280] bg-white"
      >
        기본값으로 되돌리기
      </button>
    </div>
  )
}

function SPProfileScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const sp = useSp()
  const params = sp.routeParams
  const weights = sp.profile?.weights_relative_to_uncrowded_subway || {}
  const effective = getEffectiveParams({
    manualVot: sp.manualVot,
    manualParams: sp.manualParams,
    routeParams: sp.routeParams,
    usePersonal: sp.usePersonal,
  })
  const vot = Math.round(effective.vot)
  const [manualDraft, setManualDraft] = useState(sp.manualVot != null ? String(sp.manualVot) : '')
  const [manualError, setManualError] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [coeffDraft, setCoeffDraft] = useState<Record<string, string>>({})
  const [coeffError, setCoeffError] = useState<Partial<Record<ManualCoeffKey, string>>>({})
  const transferMin = effective.params.transfer_penalty
  const taxiW = effective.params.delta_taxi
  const details = params ? [
    { key: 'vot', label: '나의 시간가치', hint: '1분을 아끼기 위해 지불할 의향', value: `${fmt(vot!)}원/분`, bar: Math.min((params.vot / 600) * 100, 100), color: '#2F7BF6' },
    { key: 'taxi', label: '택시 체감', hint: feelLine('택시', taxiW!), value: (taxiW!).toFixed(2), bar: Math.min((taxiW! / 3) * 100, 100), color: '#FF6B3D' },
    { key: 'bus', label: '버스 체감', hint: feelLine('버스', weights.bus ?? params.alpha_bus), value: (weights.bus ?? params.alpha_bus).toFixed(2), bar: Math.min(((weights.bus ?? params.alpha_bus) / 3) * 100, 100), color: '#374151' },
    { key: 'walk', label: '도보 체감', hint: feelLine('도보', weights.walk ?? params.gamma_walk), value: (weights.walk ?? params.gamma_walk).toFixed(2), bar: Math.min(((weights.walk ?? params.gamma_walk) / 3) * 100, 100), color: '#374151' },
    { key: 'tr', label: '환승 1회', hint: `환승 1회 = ${(transferMin!).toFixed(1)}분`, value: `${(transferMin!).toFixed(1)}분`, bar: Math.min((transferMin! / 20) * 100, 100), color: '#22C55E' },
  ] : []

  return (
    <div className="flex flex-col h-full bg-[#F9FAFB]">
      <NavHeader title="내 프로필" subtitle="설문으로 맞춘 나의 시간가치" onBack={() => onNav('home')} />
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 pb-5">
        {/* Toggle */}
        <div className="bg-white rounded-2xl p-4 border border-[#E5E7EB]">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[15px] font-semibold text-[#111827]">이 값으로 추천</div>
            <Toggle on={sp.usePersonal} onChange={sp.setUsePersonal} />
          </div>
          {sp.usePersonal && (
            <div className="flex items-center gap-2 bg-[#DCFCE7] rounded-xl px-3 py-2">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 7l3 3 5-5" stroke="#16A34A" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
              <span className="text-[12px] font-medium text-[#16A34A]">개인화 추천이 활성화되어 있습니다</span>
            </div>
          )}
        </div>

        <div className="bg-white rounded-2xl p-4 border border-[#E5E7EB]">
          <div className="text-[15px] font-semibold text-[#111827] mb-1">내 시간가치 직접 수정</div>
          <div className="text-[11px] text-[#9CA3AF] mb-3">{VOT_MIN}~{fmt(VOT_MAX)}원/분</div>
          <div className="flex items-center gap-2 border border-[#E5E7EB] rounded-xl overflow-hidden">
            <input
              type="text"
              inputMode="numeric"
              value={manualDraft}
              onChange={event => { setManualDraft(event.target.value.replace(/[^\d.]/g, '')); setManualError('') }}
              className="flex-1 px-4 py-3 text-[15px] font-medium text-[#374151] outline-none bg-transparent"
              placeholder="원/분"
            />
            <span className="pr-4 text-[13px] text-[#9CA3AF]">원/분</span>
          </div>
          {manualError && <div className="mt-2 text-[12px] text-[#EF4444]">{manualError}</div>}
          <button
            type="button"
            onClick={() => {
              const parsed = parseManualVot(manualDraft)
              if (parsed == null) {
                setManualError(`${VOT_MIN}~${fmt(VOT_MAX)}원/분 사이 숫자만 입력해 주세요`)
                return
              }
              sp.setManualVot(parsed)
              setManualError('')
            }}
            className="mt-3 w-full py-3 bg-[#2F7BF6] rounded-xl text-[14px] font-semibold text-white"
          >
            저장
          </button>
          {sp.manualVot != null && (
            <button
              type="button"
              onClick={() => {
                sp.setManualVot(null)
                setManualDraft('')
                setManualError('')
              }}
              className="mt-2 w-full py-3 border border-[#E5E7EB] rounded-xl text-[14px] font-medium text-[#6B7280] bg-white"
            >
              설문 추정값으로 되돌리기
            </button>
          )}
        </div>

        {/* User summary */}
        <div className="bg-white rounded-2xl p-4 border border-[#E5E7EB]">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-9 h-9 bg-[#F3F4F6] rounded-full flex items-center justify-center text-lg">👤</div>
            <div className="flex-1">
              <div className="text-[13px] font-semibold text-[#111827]">
                {sp.displayAge || sp.profile?.age_group || '미설정'} · {sp.displayPurpose || sp.profile?.purpose || '설문 전'}
              </div>
              <div className="text-[11px] text-[#9CA3AF]">
                {formatProfileDate(sp.profile?.created_at) || '아직 설문을 하지 않았어요'}
                {sp.questionCount ? ` · ${sp.questionCount}문항` : ''}
              </div>
            </div>
            {params && (
              <span className={`text-[11px] font-semibold px-2.5 py-1 rounded-full ${sp.profile?.soft_mix ? 'text-[#B45309] bg-[#FEF3C7]' : 'text-[#22C55E] bg-[#DCFCE7]'}`}>
                {sp.profile?.soft_mix ? '기본값과 함께 반영' : '설정 완료'}
              </span>
            )}
          </div>
          {/* Key stats */}
          <div className="grid grid-cols-3 gap-2 pt-3 border-t border-[#F3F4F6]">
            <div className="text-center">
              <div className="text-[11px] text-[#9CA3AF] mb-0.5">시간가치</div>
              <div className="text-[15px] font-bold text-[#2F7BF6]">
                {effective.sources.vot === 'default' && !sp.routeParams ? '—' : `${fmt(vot)}원/분`}
              </div>
            </div>
            <div className="text-center">
              <div className="text-[11px] text-[#9CA3AF] mb-0.5">환승 1회</div>
              <div className="text-[15px] font-bold text-[#22C55E]">{`${transferMin.toFixed(1)}분`}</div>
            </div>
            <div className="text-center">
              <div className="text-[11px] text-[#9CA3AF] mb-0.5">택시 체감</div>
              <div className="text-[15px] font-bold text-[#FF6B3D]">{taxiW.toFixed(2)}</div>
            </div>
          </div>
        </div>

        {/* Parameter details */}
        <div className="bg-white rounded-2xl border border-[#E5E7EB] overflow-hidden">
          <div className="px-4 py-3 border-b border-[#F3F4F6]">
            <div className="text-[13px] font-semibold text-[#374151]">나의 체감</div>
          </div>
          {details.length === 0 ? (
            <div className="px-4 py-3.5 text-[13px] text-[#9CA3AF]">설문을 완료하면 여기에 표시됩니다</div>
          ) : details.map(row => (
            <div key={row.key} className="px-4 py-3.5 border-b border-[#F9FAFB] last:border-0">
              <div className="flex items-start justify-between mb-2">
                <div>
                  <div className="text-[13px] font-semibold text-[#111827]">{row.label}</div>
                  <div className="text-[11px] text-[#9CA3AF] mt-0.5">{row.hint}</div>
                </div>
                <div className="text-right">
                  <div className="text-[16px] font-bold" style={{ color: row.color }}>{row.value}</div>
                </div>
              </div>
              <div className="h-1.5 bg-[#F3F4F6] rounded-full overflow-hidden">
                <div className="h-full rounded-full" style={{ background: row.color, width: `${row.bar}%` }} />
              </div>
            </div>
          ))}
        </div>

        <div className="bg-white rounded-2xl border border-[#E5E7EB] overflow-hidden">
          <button
            type="button"
            onClick={() => setAdvancedOpen(open => !open)}
            className="w-full px-4 py-3.5 flex items-center justify-between"
          >
            <span className="text-[15px] font-semibold text-[#111827]">고급 설정</span>
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className={advancedOpen ? 'rotate-90' : ''}>
              <path d="M6 12L10 8L6 4" stroke="#9CA3AF" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </button>
          {advancedOpen && (
            <div className="px-4 pb-4 space-y-4 border-t border-[#F3F4F6] pt-3">
              <RankingWeightSection />
              <div className="rounded-xl bg-[#F9FAFB] px-3 py-3">
                <div className="flex items-center justify-between mb-1">
                  <div>
                    <span className="text-[13px] font-semibold text-[#111827]">여유 지하철 체감</span>
                    <span className="ml-1.5 text-[10px] text-[#9CA3AF]">beta_sub</span>
                  </div>
                  {sourceBadge('default')}
                </div>
                <div className="text-[11px] text-[#9CA3AF] mb-2">기준값이라 수정할 수 없어요. 다른 체감은 이 1.0을 기준으로 비교해요.</div>
                <div className="text-[15px] font-bold text-[#6B7280]">1.0</div>
              </div>
              {MANUAL_COEFFS.map(spec => {
                const value = effective.params[spec.key]
                const draft = coeffDraft[spec.key] ?? String(value)
                return (
                  <div key={spec.key} className="rounded-xl border border-[#F3F4F6] px-3 py-3">
                    <div className="flex items-center justify-between mb-1">
                      <div>
                        <span className="text-[13px] font-semibold text-[#111827]">{spec.label}</span>
                        <span className="ml-1.5 text-[10px] text-[#9CA3AF]">{spec.key}</span>
                      </div>
                      {sourceBadge(effective.sources[spec.key])}
                    </div>
                    <div className="text-[11px] text-[#9CA3AF] mb-2">{spec.hint(value)}</div>
                    <input
                      type="range"
                      min={spec.min}
                      max={spec.max}
                      step={spec.step}
                      value={value}
                      aria-label={spec.label}
                      onChange={event => {
                        const parsed = parseManualCoeff(spec.key, Number(event.target.value))
                        if (parsed == null) return
                        sp.setManualCoeff(spec.key, parsed)
                        setCoeffDraft(current => ({ ...current, [spec.key]: String(parsed) }))
                        setCoeffError(current => ({ ...current, [spec.key]: undefined }))
                      }}
                      className="w-full"
                    />
                    <div className="mt-2 flex items-center gap-2">
                      <input
                        type="text"
                        inputMode="decimal"
                        value={draft}
                        onChange={event => {
                          const next = event.target.value.replace(/[^\d.]/g, '')
                          setCoeffDraft(current => ({ ...current, [spec.key]: next }))
                          const parsed = parseManualCoeff(spec.key, next)
                          if (parsed == null) {
                            setCoeffError(current => ({ ...current, [spec.key]: `${spec.min}~${spec.max} 사이만 저장돼요` }))
                            return
                          }
                          sp.setManualCoeff(spec.key, parsed)
                          setCoeffError(current => ({ ...current, [spec.key]: undefined }))
                        }}
                        className="w-24 rounded-lg border border-[#E5E7EB] px-2 py-1.5 text-[13px] text-[#374151] outline-none"
                      />
                      {spec.unit ? <span className="text-[12px] text-[#9CA3AF]">{spec.unit}</span> : null}
                      <button
                        type="button"
                        onClick={() => {
                          sp.setManualCoeff(spec.key, null)
                          setCoeffDraft(current => {
                            const next = { ...current }
                            delete next[spec.key]
                            return next
                          })
                          setCoeffError(current => ({ ...current, [spec.key]: undefined }))
                        }}
                        className="ml-auto text-[12px] font-medium text-[#6B7280]"
                      >
                        되돌리기
                      </button>
                    </div>
                    {coeffError[spec.key] && (
                      <div className="mt-1 text-[11px] text-[#EF4444]">{coeffError[spec.key]}</div>
                    )}
                  </div>
                )
              })}
              <button
                type="button"
                onClick={() => {
                  sp.resetManualParams()
                  setCoeffDraft({})
                  setCoeffError({})
                }}
                className="w-full py-3 border border-[#E5E7EB] rounded-xl text-[14px] font-medium text-[#6B7280] bg-white"
              >
                설문 추정값으로 초기화
              </button>
            </div>
          )}
        </div>

        <button
          onClick={() => {
            sp.restartSurvey()
            onNav('sp-setup')
          }}
          className="w-full py-3.5 border border-[#E5E7EB] rounded-xl text-[14px] font-medium text-[#6B7280] bg-white"
        >
          다시 설문하기
        </button>
      </div>
    </div>
  )
}

function SearchInputScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const [origin, setOrigin] = useState('e편한세상 용인 구성역 플랫폼시티')
  const [dest, setDest] = useState('63빌딩')
  const [time, setTime] = useState('2026-08-26T10:30')

  return (
    <div className="flex flex-col h-full bg-white">
      <NavHeader title="경로 검색" onBack={() => onNav('home')} />
      <div className="px-5 py-4 space-y-3">
        {/* OD inputs */}
        <div className="border border-[#E5E7EB] rounded-2xl overflow-hidden">
          <div className="flex items-center gap-3 px-4 py-3.5 border-b border-[#F3F4F6]">
            <div className="w-2.5 h-2.5 rounded-full border-2 border-[#9CA3AF]" />
            <input className="flex-1 text-[14px] text-[#374151] outline-none bg-transparent" value={origin} onChange={e => setOrigin(e.target.value)} />
          </div>
          <div className="flex items-center gap-3 px-4 py-3.5">
            <div className="w-2.5 h-2.5 rounded-full bg-[#2F7BF6]" />
            <input className="flex-1 text-[14px] text-[#374151] outline-none bg-transparent" value={dest} onChange={e => setDest(e.target.value)} />
          </div>
        </div>

        {/* Time */}
        <div className="border border-[#E5E7EB] rounded-xl px-4 py-3 flex items-center gap-3">
          <span className="text-[#9CA3AF]">🕐</span>
          <input type="datetime-local" value={time} onChange={e => setTime(e.target.value)} className="flex-1 text-[14px] text-[#374151] outline-none bg-transparent" />
        </div>

        <button onClick={() => onNav('reservation')} className="w-full py-4 bg-[#2F7BF6] rounded-xl text-white font-semibold text-[15px]">
          길찾기
        </button>

        <div className="pt-2">
          <div className="text-[12px] text-[#9CA3AF] mb-3">최근 검색</div>
          {[
            { from: '서울대 35동', to: '삼성역' },
            { from: '동탄', to: '공덕역' },
          ].map(({ from, to }, i) => (
            <div key={i} className="flex items-center gap-3 py-3 border-b border-[#F9FAFB]">
              <span className="text-[#9CA3AF] text-sm">🕐</span>
              <span className="text-[13px] text-[#374151]">{from} → {to}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function AnalyzingScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const trip = useTrip()
  const finishing = useRef(false)
  const progress = trip.analyzeProgress
  const stage = trip.analyzeStage || '서버를 깨우는 중이에요'

  const finishSearch = () => {
    if (finishing.current) return
    finishing.current = true
    trip
      .waitForAnalyze()
      .then(async data => {
        const limits = trip.pendingLimits
        if (hasLimits(limits) && limits) {
          await trip.applyRank(limits, data.candidates)
        } else {
          trip.clearRanking()
        }
        onNav('results')
      })
      .catch(() => {
        finishing.current = false
      })
  }

  useEffect(() => {
    finishSearch()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const r = 52, circ = 2 * Math.PI * r
  const offset = circ * (1 - progress / 100)
  const failed = Boolean(trip.analyzeError || trip.rankError)
  const loading = !failed

  return (
    <div className="flex flex-col h-full bg-white">
      <div className="pt-10 px-5 pb-3 flex items-center justify-center">
        <div className="text-[24px] font-bold text-[#2F7BF6] tracking-tight">OPTI</div>
      </div>

      <div className="flex-1 flex flex-col items-center justify-center px-8">
        {loading && (
          <>
            <div className="relative w-36 h-36 mb-8">
              <svg className="w-full h-full -rotate-90" viewBox="0 0 120 120">
                <circle cx="60" cy="60" r={r} fill="none" stroke="#F3F4F6" strokeWidth="10"/>
                <circle cx="60" cy="60" r={r} fill="none" stroke="#2F7BF6" strokeWidth="10"
                  strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round"
                  style={{ transition: 'stroke-dashoffset 0.15s ease' }}
                />
              </svg>
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="text-[28px] font-bold text-[#111827]">{Math.round(progress)}%</span>
              </div>
            </div>

            <div className="text-[17px] font-semibold text-[#111827] mb-1.5">최적 경로를 찾고 있어요</div>
            <div className="text-[13px] text-[#9CA3AF] mb-3">거리에 따라 최대 1~2분 걸릴 수 있어요</div>
            <div className="text-[13px] text-[#2F7BF6] font-medium mb-7">{stage}</div>

            <div className="flex gap-1.5 mb-10">
              {[0,1,2].map(i => <div key={i} className={`w-2 h-2 rounded-full ${i === 0 ? 'bg-[#2F7BF6]' : 'bg-[#E5E7EB]'}`} />)}
            </div>
          </>
        )}

        {failed && (
          <div className="w-full mb-8 text-center">
            <div className="text-[17px] font-semibold text-[#111827] mb-2">경로를 불러오지 못했어요</div>
            <div className="text-[13px] text-[#6B7280] mb-6 leading-relaxed">{trip.analyzeError || trip.rankError}</div>
            <button
              type="button"
              onClick={() => {
                if (trip.analyzeError) trip.retryAnalyze()
                finishing.current = false
                finishSearch()
              }}
              className="w-full py-4 mb-3 bg-[#2F7BF6] rounded-xl text-white font-semibold text-[15px]"
            >
              다시 시도
            </button>
            <button
              type="button"
              onClick={() => onNav('home')}
              className="w-full py-4 border border-[#E5E7EB] rounded-xl text-[14px] text-[#6B7280] font-medium bg-white"
            >
              홈으로
            </button>
          </div>
        )}

        <div className="w-full space-y-2">
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full border-2 border-[#9CA3AF]" />
            <span className="text-[13px] text-[#374151]">{trip.origin?.name ?? '출발지'}</span>
          </div>
          <div className="ml-1.5 w-0.5 h-4 bg-[#E5E7EB]" />
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full bg-[#2F7BF6]" />
            <span className="text-[13px] text-[#374151]">{trip.destination?.name ?? '도착지'}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

type RouteType = 'all' | 'hybrid' | 'transit' | 'taxi'

function ResultsScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const trip = useTrip()
  const sp = useSp()
  const [tab, setTab] = useState<RouteType>('all')
  const data = trip.analysis
  const candidates = data?.candidates ?? []
  const list = data
    ? (trip.ranking ? orderByRanking(candidates, trip.ranking.ranking) : orderByTopKnee(data))
    : []
  const byId = new Map(candidates.map(item => [item.id, item]))
  const overById = new Map((trip.ranking?.ranking ?? []).map(item => [item.id, item]))
  const fastest = data?.anchors.fastest ? byId.get(data.anchors.fastest) : undefined
  const cheapest = data?.anchors.cheapest ? byId.get(data.anchors.cheapest) : undefined
  const taxiOnly = list.find(item => item.type === 'TT') ?? (fastest?.type === 'TT' ? fastest : undefined)
  const transitOnly = list.find(item => item.type === 'PP') ?? (cheapest?.type === 'PP' ? cheapest : undefined)
  const rank1 = list[0]

  const tabs: { key: RouteType; label: string; count: number }[] = [
    { key: 'all', label: '전체', count: list.length },
    { key: 'hybrid', label: '복합환승', count: list.filter(r => r.type === 'PT' || r.type === 'TP').length },
    { key: 'transit', label: '대중교통', count: list.filter(r => r.type === 'PP').length },
    { key: 'taxi', label: '택시', count: list.filter(r => r.type === 'TT').length },
  ]

  const filtered = list.filter(r => {
    if (tab === 'all') return true
    if (tab === 'hybrid') return r.type === 'PT' || r.type === 'TP'
    if (tab === 'transit') return r.type === 'PP'
    if (tab === 'taxi') return r.type === 'TT'
    return true
  })

  const openDetail = (route: RouteCandidate) => {
    trip.setSelectedRoute(route)
    onNav('detail')
  }

  const rank1VsTransitMin = rank1 && transitOnly ? Math.round(transitOnly.total_time - rank1.total_time) : 0
  const rank1VsTaxiWon = rank1 && taxiOnly ? Math.round(taxiOnly.cost - rank1.cost) : 0

  return (
    <div className="flex flex-col h-full bg-white">
      <div className="bg-white px-4 pt-10 pb-0 border-b border-[#F3F4F6]">
        <div className="flex items-center gap-2 mb-2">
          <button onClick={() => onNav('home')} className="flex-shrink-0">
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M11 14L6 9L11 4" stroke="#111827" strokeWidth="1.75" strokeLinecap="round"/></svg>
          </button>
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-semibold text-[#111827] truncate">
              {trip.origin?.name ?? '출발지'} → {trip.destination?.name ?? '도착지'}
            </div>
            <div className="flex items-center gap-1 text-[11px] text-[#9CA3AF]">
              <span>{formatDepartLabel(trip.departTime)}</span>
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 4l3 3 3-3" stroke="#9CA3AF" strokeWidth="1.2"/></svg>
            </div>
          </div>
          <button className="flex-shrink-0">
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M3 9h12M3 5h12M3 13h12" stroke="#9CA3AF" strokeWidth="1.5" strokeLinecap="round"/></svg>
          </button>
        </div>

        <div className="flex">
          {tabs.map(({ key, label, count }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`flex-1 pb-2.5 pt-1 text-[12px] font-semibold border-b-2 transition-all ${
                tab === key ? 'border-[#111827] text-[#111827]' : 'border-transparent text-[#9CA3AF]'
              }`}
            >
              {label} <span className={tab === key ? 'text-[#111827]' : 'text-[#C4C9D4]'}>{count}</span>
            </button>
          ))}
        </div>
      </div>

        <div className="flex items-center justify-between px-4 py-2 border-b border-[#F3F4F6]">
        <button type="button" onClick={() => onNav('home')} className="text-[12px] font-semibold text-[#374151]">
          출발 {clockLabel(trip.departTime)} → 도착 예정 {rank1 ? clockAfter(trip.departTime, rank1.total_time) : '--:--'}
        </button>
        <button
          type="button"
          onClick={() => onNav('reservation')}
          className="max-w-[70%] truncate rounded-full bg-[#EAF2FF] px-3 py-1 text-[12px] font-semibold text-[#2F7BF6]"
        >
          {limitsSummary(trip.appliedLimits) || '조건 없음'}
        </button>
      </div>

      {trip.ranking && trip.appliedLimits && !betasEqual(sp.rankingBetas, trip.rankedBetas) && (
        <div className="px-4 py-2.5 border-b border-[#F3F4F6] bg-[#FFF7ED]">
          <button
            type="button"
            disabled={trip.rankingBusy}
            onClick={() => { void trip.applyRank(trip.appliedLimits!) }}
            className="w-full rounded-xl bg-[#2F7BF6] py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {trip.rankingBusy ? '정렬 중…' : '설정이 바뀌었어요 · 다시 정렬'}
          </button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto pb-20">
        {rank1 && (taxiOnly || transitOnly) && (
          <div style={{ padding: '16px 18px', background: 'white', borderBottom: '8px solid #F2F4F7' }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#111827', marginBottom: 8 }}>추천 경로 절약 효과</div>
            <div style={{ fontSize: 12, color: '#6B7280', lineHeight: 1.6, wordBreak: 'keep-all' }}>
              {transitOnly && (
                <div>대중교통만 {Math.round(transitOnly.total_time)}분 · {fmt(Math.round(transitOnly.cost))}원</div>
              )}
              {taxiOnly && (
                <div>택시만 {Math.round(taxiOnly.total_time)}분 · {fmt(Math.round(taxiOnly.cost))}원</div>
              )}
            </div>
            <div style={{ marginTop: 10, fontSize: 12, fontWeight: 600, color: '#2F7BF6', wordBreak: 'keep-all', lineHeight: 1.5 }}>
              {[
                rank1VsTransitMin > 0 ? `대중교통만보다 ${rank1VsTransitMin}분 빠름` : null,
                rank1VsTaxiWon > 0 ? `택시만보다 ${fmt(rank1VsTaxiWon)}원 저렴` : null,
              ].filter(Boolean).join(' · ') || '추천 경로를 확인해 보세요'}
            </div>
          </div>
        )}

        {filtered.map(r => {
          const isFirst = r.id === rank1?.id
          const segments = (r.legs || []).map(legToSegment)
          const compareParts: string[] = []
          if (transitOnly && r.id !== transitOnly.id) {
            const d = Math.round(transitOnly.total_time - r.total_time)
            if (d > 0) compareParts.push(`대중교통만보다 ${d}분 빠름`)
          }
          if (taxiOnly && r.id !== taxiOnly.id) {
            const d = Math.round(taxiOnly.cost - r.cost)
            if (d > 0) compareParts.push(`택시만보다 ${fmt(d)}원 저렴`)
          }
          const ranked = overById.get(r.id)
          const gapText = ranked && !ranked.meets_all ? overHint(ranked.over) : ''
          const tags = [
            isFirst ? '추천' : '',
            ROUTE_TYPE_LABEL[r.type] || r.type,
            r.transfer_station ? r.transfer_station : '',
          ].filter(Boolean)

          return (
            <div key={r.id} style={{ opacity: isFirst ? 1 : 0.92 }}>
              <div
                onClick={() => openDetail(r)}
                className="cursor-pointer"
                style={{
                  padding: '20px 18px',
                  background: isFirst ? '#F5F9FF' : 'white',
                }}
              >
                {tags.length > 0 && (
                  <div style={{ display: 'flex', gap: 5, marginBottom: 8, flexWrap: 'wrap' }}>
                    {tags.map(t => (
                      <span key={t} style={{
                        height: 22, display: 'inline-flex', alignItems: 'center',
                        padding: '0 8px', borderRadius: 4,
                        fontSize: 12, fontWeight: 600,
                        background: t === '추천' ? '#EAF2FE' : '#F2F4F7',
                        color: t === '추천' ? '#2F7BF6' : '#4B5563',
                      }}>{t}</span>
                    ))}
                  </div>
                )}

                <div style={{ marginBottom: 4 }}>
                  <span style={{
                    fontSize: 30, fontWeight: 800, color: '#111827',
                    letterSpacing: '-0.03em', fontVariantNumeric: 'tabular-nums', lineHeight: 1.1,
                  }}>{Math.round(r.total_time)}</span>
                  <span style={{ fontSize: 20, fontWeight: 600, color: '#111827', marginLeft: 2 }}>분</span>
                </div>

                <div style={{
                  fontSize: 13, color: '#6B7280', marginBottom: 14,
                  fontVariantNumeric: 'tabular-nums', wordBreak: 'keep-all',
                }}>
                  {arrivalLabel(trip.departTime, 0)} - {arrivalLabel(trip.departTime, r.total_time)} · {fmt(Math.round(r.cost))}원 · 환승 {r.transfers}회
                  {r.transfer_station ? ` · ${r.transfer_station}` : ''}
                </div>

                <SegmentBar segments={segments} />
                <SegmentSummary segments={segments} />

                {gapText && (
                  <div style={{
                    marginTop: 8, fontSize: 12, color: '#9CA3AF',
                    wordBreak: 'keep-all', lineHeight: 1.5,
                  }}>
                    {gapText}
                  </div>
                )}

                {compareParts.length > 0 && (
                  <div style={{
                    marginTop: 12, fontSize: 12, fontWeight: 600, color: '#2F7BF6',
                    wordBreak: 'keep-all', lineHeight: 1.5,
                  }}>
                    {compareParts.join(' · ')}
                  </div>
                )}

                <button
                  onClick={e => { e.stopPropagation(); openDetail(r) }}
                  style={{
                    marginTop: 14, width: '100%', height: 44,
                    background: 'white', border: '1px solid #E5E7EB',
                    borderRadius: 10, fontSize: 14, fontWeight: 600, color: '#374151',
                    cursor: 'pointer',
                  }}
                >
                  상세 보기
                </button>
              </div>
              <div style={{ height: 8, background: '#F2F4F7' }} />
            </div>
          )
        })}

        {filtered.length === 0 && (
          <div style={{ padding: 24, textAlign: 'center', fontSize: 13, color: '#9CA3AF' }}>
            표시할 경로가 없습니다
          </div>
        )}
      </div>

      <div className="absolute bottom-0 left-0 right-0 bg-white border-t border-[#F3F4F6] px-8 pt-3 pb-5 flex justify-around">
        {[
          { icon: '📋', label: 'SP 설문', screen: 'sp-setup' },
          { icon: '👤', label: '프로필', screen: 'sp-profile' },
        ].map(({ icon, label, screen }) => (
          <button key={label} onClick={() => onNav(screen as Screen)} className="flex flex-col items-center gap-1">
            <span className="text-xl">{icon}</span>
            <span className="text-[10px] text-[#9CA3AF] font-medium">{label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function DetailScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const trip = useTrip()
  const route = trip.selectedRoute
  const originName = trip.origin?.name ?? '출발지'
  const destName = trip.destination?.name ?? '도착지'
  const timeline = route
    ? legsToTimeline(route, originName, destName, trip.departTime)
    : []

  return (
    <div className="flex flex-col h-full bg-white">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 pt-10 pb-3 bg-white border-b border-[#F3F4F6]">
        <button onClick={() => onNav('results')}>
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M11 14L6 9L11 4" stroke="#111827" strokeWidth="1.75" strokeLinecap="round"/></svg>
        </button>
        <div>
          <div className="text-[11px] text-[#9CA3AF]">{originName}</div>
          <div className="text-[11px] text-[#2F7BF6]">● {destName}</div>
        </div>
        <button className="ml-auto text-[#9CA3AF]" onClick={() => onNav('results')}>✕</button>
      </div>

      {/* Cost/time summary */}
      <div className="px-5 py-4 border-b border-[#F3F4F6]">
        <div className="text-[20px] font-bold text-[#111827]">
          {route
            ? `${fmt(Math.round(route.cost))}원 · ${Math.round(route.total_time)}분 · ${arrivalLabel(trip.departTime, route.total_time)} 도착`
            : '경로를 선택해 주세요'}
        </div>
      </div>

      {/* Timeline */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {timeline.map((item, i) => (
          <div key={i} className="flex gap-4 min-h-[40px]">
            {/* Time */}
            <div className="w-10 flex-shrink-0 text-[12px] text-[#9CA3AF] font-medium pt-0.5 text-right">{item.time}</div>
            {/* Spine */}
            <div className="flex flex-col items-center w-5 flex-shrink-0">
              {item.dot ? (
                <div className="w-3 h-3 rounded-full flex-shrink-0 mt-1" style={{ background: item.dot }} />
              ) : (
                <div className="w-1.5 h-1.5 rounded-full bg-transparent flex-shrink-0 mt-1.5" />
              )}
              {item.line && i < timeline.length - 1 && (
                <div className="flex-1 w-0.5 mt-1" style={{ background: item.line }} />
              )}
            </div>
            {/* Content */}
            <div className={`flex-1 pb-4 ${item.taxi ? 'bg-[#FFF7ED] -mx-2 px-3 py-2 rounded-xl border border-[#FED7AA]' : ''}`}>
              <div className={`text-[13px] text-[#111827] ${item.station ? 'font-bold' : 'font-medium'}`}>
                {item.taxi && <span className="text-[#FF6B3D] font-semibold">🚕 여기서 택시로 환승 · </span>}
                {item.label}
              </div>
              {item.sub && <div className="text-[11px] text-[#9CA3AF]">{item.sub}</div>}
              {item.badge && (
                <div className="inline-flex items-center gap-1 mt-1 px-2 py-0.5 rounded-full text-[10px] font-semibold text-white"
                  style={{ background: item.dot || '#374151' }}
                >
                  {item.badge}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Bottom buttons */}
      <div className="px-5 pt-3 border-t border-[#F3F4F6] bg-white">
        {route && (
          <div className="pb-3 text-[13px] font-semibold text-[#111827]">
            {Math.round(route.total_time)}분 · {fmt(Math.round(route.cost))}원 · 환승 {route.transfers}회
          </div>
        )}
        <div className="pb-8 flex gap-3">
          <button className="flex-1 py-4 bg-[#FF6B3D] text-white rounded-xl font-semibold text-[15px]">택시 호출</button>
          <button className="flex-1 py-4 border border-[#E5E7EB] text-[#374151] rounded-xl font-semibold text-[14px] bg-white">경로 저장</button>
        </div>
      </div>
      <div className="px-5 pb-4 text-center text-[11px] text-[#9CA3AF]">⚠️ 입구 주변 혼잡에 따라 달라질 수 있음</div>
    </div>
  )
}

function ReservationScreen({ onNav }: { onNav: (s: Screen) => void }) {
  type Condition = 'time' | 'cost' | 'transfer' | 'duration'
  type Strictness = 'low' | 'medium' | 'high'
  const trip = useTrip()
  const seed = trip.appliedLimits || trip.pendingLimits
  const candidates = trip.analysis?.candidates ?? []
  const taxiOnly = candidates.find(item => item.type === 'TT')
  const transitOnly = candidates.find(item => item.type === 'PP')
  const timeHint = rangeHint(
    taxiOnly ? { label: '택시만', value: `${Math.round(taxiOnly.total_time)}분` } : undefined,
    transitOnly ? { label: '대중교통만', value: `${Math.round(transitOnly.total_time)}분` } : undefined,
  )
  const costHint = rangeHint(
    taxiOnly ? { label: '택시만', value: `${fmt(Math.round(taxiOnly.cost))}원` } : undefined,
    transitOnly ? { label: '대중교통만', value: `${fmt(Math.round(transitOnly.cost))}원` } : undefined,
  )
  const transferHint = rangeHint(
    taxiOnly ? { label: '택시만', value: `${taxiOnly.transfers}회` } : undefined,
    transitOnly ? { label: '대중교통만', value: `${transitOnly.transfers}회` } : undefined,
  )
  const arriveHint = rangeHint(
    taxiOnly ? { label: '택시만', value: arrivalLabel(trip.departTime, taxiOnly.total_time) } : undefined,
    transitOnly ? { label: '대중교통만', value: arrivalLabel(trip.departTime, transitOnly.total_time) } : undefined,
  )

  const [arrivalTime, setArrivalTime] = useState(() => seed?.arrive_by ? clockLabel(seed.arrive_by) : clockLabel(trip.departTime))
  const [quickTime, setQuickTime] = useState('직접 입력')
  const [maxTime, setMaxTime] = useState(seed?.max_time_min != null ? String(Math.round(seed.max_time_min)) : '')
  const [minCost, setMinCost] = useState(5000)
  const [maxCost, setMaxCost] = useState(seed?.max_cost_krw != null ? Number(seed.max_cost_krw) : 20000)
  const [transferLimit, setTransferLimit] = useState(seed?.max_transfers != null ? Number(seed.max_transfers) : 2)
  const [strictness, setStrictness] = useState<Record<Condition, Strictness>>({
    time: seed?.importance?.time || 'medium',
    cost: seed?.importance?.cost || 'medium',
    transfer: seed?.importance?.transfer || 'medium',
    duration: seed?.importance?.duration || seed?.importance?.time || 'medium',
  })
  const [enabled, setEnabled] = useState<Record<Condition, boolean>>({
    time: Boolean(seed?.arrive_by),
    cost: seed?.max_cost_krw != null,
    transfer: seed?.max_transfers != null,
    duration: seed?.max_time_min != null,
  })

  const strictnessOptions: { value: Strictness; label: string }[] = [
    { value: 'low', label: '하' },
    { value: 'medium', label: '중' },
    { value: 'high', label: '상' },
  ]

  const formatTime = (value: string) => {
    const [hour, minute] = value.split(':').map(Number)
    return `${hour < 12 ? '오전' : '오후'} ${hour % 12 || 12}:${String(minute).padStart(2, '0')}`
  }

  const minutesToTime = (value: number) =>
    `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`

  const selectQuickTime = (minutes: number, label: string) => {
    const base = trip.departTime ? new Date(trip.departTime) : new Date()
    const total = base.getHours() * 60 + base.getMinutes() + minutes
    setArrivalTime(minutesToTime(total % (24 * 60)))
    setQuickTime(label)
  }

  const [arrivalHour, arrivalMinute] = arrivalTime.split(':').map(Number)
  const period = arrivalHour < 12 ? '오전' : '오후'
  const hour12 = arrivalHour % 12 || 12
  const minuteOptions = [0, 10, 20, 30, 40, 50]

  const setWheelHour = (nextHour: number) => {
    const hour24 = period === '오전' ? nextHour % 12 : nextHour % 12 + 12
    setArrivalTime(minutesToTime(hour24 * 60 + arrivalMinute))
    setQuickTime('직접 입력')
  }

  const setWheelMinute = (nextMinute: number) => {
    setArrivalTime(minutesToTime(arrivalHour * 60 + nextMinute))
    setQuickTime('직접 입력')
  }

  const setWheelPeriod = (nextPeriod: string) => {
    const hour24 = nextPeriod === '오전' ? arrivalHour % 12 : arrivalHour % 12 + 12
    setArrivalTime(minutesToTime(hour24 * 60 + arrivalMinute))
    setQuickTime('직접 입력')
  }

  const renderToggle = (key: Condition) => (
    <button
      type="button"
      role="switch"
      aria-checked={enabled[key]}
      aria-label={`${key} 조건 ${enabled[key] ? '끄기' : '켜기'}`}
      onClick={() => setEnabled(current => ({ ...current, [key]: !current[key] }))}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${enabled[key] ? 'bg-[#2F7BF6]' : 'bg-[#D6DBE3]'}`}
    >
      <span className={`absolute top-[3px] h-[18px] w-[18px] rounded-full bg-white shadow-sm transition-[left] ${enabled[key] ? 'left-[23px]' : 'left-[3px]'}`} />
    </button>
  )

  const renderStrictness = (key: Condition) => {
    return (
      <div className="mt-4 border-t border-[#F0F2F5] pt-3">
        <div className="mb-2 text-[12px] font-semibold text-[#596273]">중요도</div>
        <div className="grid grid-cols-3 gap-1 rounded-[10px] bg-[#F1F3F6] p-1">
          {strictnessOptions.map(option => (
            <button
              type="button"
              key={option.value}
              onClick={() => setStrictness(current => ({ ...current, [key]: option.value }))}
              className={`h-8 rounded-lg text-[13px] font-semibold transition-all ${
                strictness[key] === option.value
                  ? 'bg-[#2F7BF6] text-white shadow-sm'
                  : 'text-[#8A94A6]'
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
    )
  }

  const collectLimits = (): RankLimits | null => {
    const limits: RankLimits = {}
    const importance: NonNullable<RankLimits['importance']> = {}
    if (enabled.duration && maxTime.trim()) {
      limits.max_time_min = Number(maxTime)
      importance.duration = strictness.duration
    }
    if (enabled.cost) {
      limits.max_cost_krw = maxCost
      importance.cost = strictness.cost
    }
    if (enabled.transfer) {
      limits.max_transfers = transferLimit
      importance.transfer = strictness.transfer
    }
    if (enabled.time) {
      limits.arrive_by = arrivalToIso(trip.departTime, arrivalTime)
      importance.time = strictness.time
    }
    if (Object.keys(importance).length) limits.importance = importance
    return hasLimits(limits) ? limits : null
  }

  return (
    <div className="flex h-full flex-col bg-[#F5F7FA]">
      <NavHeader title="희망 조건 설정" onBack={() => onNav('home')} />

      <div className="flex-1 space-y-3 overflow-y-auto px-5 pb-4 pt-4">
        <div className="rounded-2xl bg-white p-4 shadow-[0_2px_14px_rgba(15,23,42,0.05)]">
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full border-2 border-[#9CA3AF]" />
            <span className="text-[13px] text-[#374151] truncate">{trip.origin?.name ?? '출발지'}</span>
          </div>
          <div className="ml-1.5 w-0.5 h-4 bg-[#E5E7EB]" />
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full bg-[#2F7BF6]" />
            <span className="text-[13px] text-[#374151] truncate">{trip.destination?.name ?? '도착지'}</span>
          </div>
        </div>
        <div className="rounded-2xl bg-white p-4 shadow-[0_2px_14px_rgba(15,23,42,0.05)]">
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[15px] font-semibold text-[#182230]">도착 시간</span>
            <span className={`text-[13px] font-semibold ${enabled.time ? 'text-[#2F7BF6]' : 'text-[#8A94A6]'}`}>
              {enabled.time ? `${arrivalTime}까지` : '상관없음'}
            </span>
            {renderToggle('time')}
          </div>
          {enabled.time && (
            <>
              <div className="mt-4 rounded-xl bg-[#F7F9FC] px-4 py-3 text-center">
                <div className="mb-1 text-[11px] font-medium text-[#8A94A6]">희망 도착 시간</div>
                <div className="relative mx-auto mt-2 flex w-fit items-center justify-center gap-1 overflow-hidden rounded-xl px-3">
                  <div className="pointer-events-none absolute left-0 right-0 top-9 h-9 rounded-lg border-y border-[#DDE3EB] bg-white/80" />
                  <div className="pointer-events-none absolute inset-x-0 top-0 z-10 h-8 bg-gradient-to-b from-[#F7F9FC] to-transparent" />
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 h-8 bg-gradient-to-t from-[#F7F9FC] to-transparent" />
                  <WheelColumn
                    options={['오전', '오후']}
                    value={period}
                    onChange={setWheelPeriod}
                    ariaLabel="오전 오후"
                    widthClass="w-14"
                  />
                  <WheelColumn
                    options={[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
                    value={hour12}
                    onChange={setWheelHour}
                    ariaLabel="시"
                  />
                  <span className="relative z-20 text-[18px] font-bold text-[#8A94A6]">:</span>
                  <WheelColumn
                    options={minuteOptions}
                    value={arrivalMinute}
                    onChange={setWheelMinute}
                    ariaLabel="분"
                  />
                </div>
                <div className="mt-1 text-[12px] font-semibold text-[#2F7BF6]">{formatTime(arrivalTime)} 도착</div>
                {arriveHint && <div className="mt-2 text-[12px] text-[#9CA3AF]">{arriveHint}</div>}
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2">
                {[
                  { label: '30분 후', minutes: 30 },
                  { label: '1시간 후', minutes: 60 },
                  { label: '직접 입력', minutes: null },
                ].map(option => (
                  <button
                    type="button"
                    key={option.label}
                    onClick={() => option.minutes === null ? setQuickTime(option.label) : selectQuickTime(option.minutes, option.label)}
                    className={`h-9 rounded-full text-[12px] font-semibold ${
                      quickTime === option.label
                        ? 'bg-[#EAF2FF] text-[#2F7BF6]'
                        : 'bg-[#F2F4F7] text-[#687386]'
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              {renderStrictness('time')}
            </>
          )}
        </div>

        <div className="rounded-2xl bg-white p-4 shadow-[0_2px_14px_rgba(15,23,42,0.05)]">
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[15px] font-semibold text-[#182230]">최대 소요시간</span>
            <span className={`text-[13px] font-semibold ${enabled.duration ? 'text-[#2F7BF6]' : 'text-[#8A94A6]'}`}>
              {enabled.duration && maxTime ? `${maxTime}분 이내` : '상관없음'}
            </span>
            {renderToggle('duration')}
          </div>
          {enabled.duration && (
            <>
              <input
                type="number"
                min="1"
                inputMode="numeric"
                placeholder={timeHint || '최대 소요시간 (분)'}
                value={maxTime}
                onChange={event => setMaxTime(event.target.value)}
                className="mt-4 w-full rounded-xl bg-[#F7F9FC] px-4 py-3 text-[14px] text-[#182230] outline-none placeholder-[#9CA3AF]"
              />
              {renderStrictness('duration')}
            </>
          )}
        </div>

        <div className="rounded-2xl bg-white p-4 shadow-[0_2px_14px_rgba(15,23,42,0.05)]">
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[15px] font-semibold text-[#182230]">비용</span>
            <span className={`text-[13px] font-semibold ${enabled.cost ? 'text-[#2F7BF6]' : 'text-[#8A94A6]'}`}>
              {enabled.cost ? `${fmt(minCost)}원 ~ ${fmt(maxCost)}원` : '상관없음'}
            </span>
            {renderToggle('cost')}
          </div>
          {enabled.cost && (
            <>
              <div className="relative mt-7 h-6">
                <div className="absolute left-0 right-0 top-[10px] h-1 rounded-full bg-[#E9EDF2]" />
                <div
                  className="absolute top-[10px] h-1 rounded-full bg-[#2F7BF6]"
                  style={{ left: `${minCost / 500}%`, right: `${100 - maxCost / 500}%` }}
                />
                <input
                  type="range"
                  min="0"
                  max="50000"
                  step="1000"
                  value={minCost}
                  aria-label="최소 비용"
                  onChange={event => setMinCost(Math.min(Number(event.target.value), maxCost - 1000))}
                  className="range-dual absolute inset-0 w-full"
                />
                <input
                  type="range"
                  min="0"
                  max="50000"
                  step="1000"
                  value={maxCost}
                  aria-label="최대 비용"
                  onChange={event => setMaxCost(Math.max(Number(event.target.value), minCost + 1000))}
                  className="range-dual absolute inset-0 w-full"
                />
              </div>
              <div className="mt-1 flex justify-between text-[12px] text-[#687386]">
                <span>최소 {fmt(minCost)}원</span>
                <span>최대 {fmt(maxCost)}원</span>
              </div>
              {costHint && <div className="mt-2 text-[12px] text-[#9CA3AF]">{costHint}</div>}
              {renderStrictness('cost')}
            </>
          )}
        </div>

        <div className="rounded-2xl bg-white p-4 shadow-[0_2px_14px_rgba(15,23,42,0.05)]">
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[15px] font-semibold text-[#182230]">환승 횟수</span>
            <span className={`text-[13px] font-semibold ${enabled.transfer ? 'text-[#2F7BF6]' : 'text-[#8A94A6]'}`}>
              {enabled.transfer ? `${transferLimit}회 이하` : '상관없음'}
            </span>
            {renderToggle('transfer')}
          </div>
          {enabled.transfer && (
            <>
              <div className="mt-5 flex items-center justify-center gap-6">
                <button
                  type="button"
                  aria-label="환승 횟수 줄이기"
                  disabled={transferLimit === 0}
                  onClick={() => setTransferLimit(value => Math.max(0, value - 1))}
                  className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#F2F4F7] text-[22px] text-[#596273] disabled:text-[#C8CED8]"
                >
                  −
                </button>
                <span className="min-w-20 text-center text-[17px] font-bold text-[#182230]">{transferLimit}회 이하</span>
                <button
                  type="button"
                  aria-label="환승 횟수 늘리기"
                  disabled={transferLimit === 4}
                  onClick={() => setTransferLimit(value => Math.min(4, value + 1))}
                  className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#F2F4F7] text-[22px] text-[#596273] disabled:text-[#C8CED8]"
                >
                  +
                </button>
              </div>
              {transferHint && <div className="mt-2 text-center text-[12px] text-[#9CA3AF]">{transferHint}</div>}
              {renderStrictness('transfer')}
            </>
          )}
        </div>
      </div>

      <div className="shrink-0 bg-[#F5F7FA] px-5 pb-5 pt-3">
        {trip.rankError && (
          <div className="mb-3 text-center text-[13px] text-[#6B7280]">{trip.rankError}</div>
        )}
        <button
          type="button"
          onClick={() => {
            trip.setPendingLimits(collectLimits())
            onNav('analyzing')
          }}
          className="h-14 w-full rounded-[14px] bg-[#2F7BF6] text-[16px] font-bold text-white shadow-[0_8px_20px_rgba(47,123,246,0.24)]"
        >
          이 조건으로 찾기
        </button>
        <button
          type="button"
          onClick={() => {
            trip.setPendingLimits(null)
            onNav('analyzing')
          }}
          className="mt-3 h-12 w-full rounded-[14px] border border-[#E5E7EB] bg-white text-[14px] font-medium text-[#6B7280]"
        >
          조건 없이 찾기
        </button>
      </div>
    </div>
  )
}

function RegretScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const top3 = ROUTES.filter(r => r.rank !== null)

  const minCost = Math.min(...ROUTES.map(r => r.cost))
  const maxCost = Math.max(...ROUTES.map(r => r.cost))
  const minTime = Math.min(...ROUTES.map(r => r.time))
  const maxTime = Math.max(...ROUTES.map(r => r.time))
  const pts = ROUTES.map(r => ({
    id: r.id, rank: r.rank,
    x: 24 + ((r.cost - minCost) / (maxCost - minCost)) * 200,
    y: 150 - ((r.time - minTime) / (maxTime - minTime)) * 120,
    dominated: r.dominated,
    color: r.rank === 1 ? '#2F7BF6' : r.rank === 2 ? '#8B5CF6' : r.rank === 3 ? '#FF6B3D' : '#D1D5DB',
  }))

  return (
    <div className="flex flex-col h-full bg-[#F9FAFB]">
      <NavHeader title="후회율 추천 3개" onBack={() => onNav('results')} />
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 pb-5">
        {/* Pareto */}
        <div className="bg-white rounded-2xl p-4 border border-[#E5E7EB]">
          <div className="text-[13px] font-semibold text-[#374151] mb-1">파레토 프론티어</div>
          <div className="text-[11px] text-[#9CA3AF] mb-2">x: 비용 · y: 시간 (아래 왼쪽이 우수)</div>
          <svg viewBox="0 0 248 170" className="w-full">
            <line x1="24" y1="10" x2="24" y2="155" stroke="#F3F4F6" strokeWidth="1"/>
            <line x1="24" y1="155" x2="240" y2="155" stroke="#F3F4F6" strokeWidth="1"/>
            <text x="14" y="14" fontSize="8" fill="#9CA3AF">시간↑</text>
            <text x="210" y="165" fontSize="8" fill="#9CA3AF">비용→</text>
            <polyline
              points={pts.filter(p => !p.dominated).sort((a,b) => a.x - b.x).map(p => `${p.x},${p.y}`).join(' ')}
              fill="none" stroke="#93C5FD" strokeWidth="1.5" strokeDasharray="5 3"
            />
            {pts.map(p => (
              <g key={p.id}>
                <circle cx={p.x} cy={p.y} r={p.dominated ? 4 : 7} fill={p.color} opacity={p.dominated ? 0.3 : 1}/>
                {p.rank && <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize="8" fill="white" fontWeight="bold">{p.rank}</text>}
                {p.rank && <text x={p.x + 10} y={p.y - 9} fontSize="9" fill={p.color} fontWeight="600">{p.id}</text>}
              </g>
            ))}
          </svg>
        </div>

        {/* Rank cards */}
        {top3.map(r => {
          const rankColor = r.rank === 1 ? '#2F7BF6' : r.rank === 2 ? '#8B5CF6' : '#FF6B3D'
          return (
            <div key={r.id} onClick={() => onNav('detail')} className="bg-white rounded-2xl p-4 border border-[#E5E7EB] cursor-pointer">
              <div className="flex items-start gap-3 mb-3">
                <div className="w-9 h-9 rounded-full flex items-center justify-center text-white text-[14px] font-bold flex-shrink-0" style={{ background: rankColor }}>
                  {r.rank}
                </div>
                <div className="flex-1">
                  <div className="text-[13px] font-semibold text-[#111827]">{r.label}</div>
                  <div className="text-[11px] text-[#9CA3AF]">{r.id} · {r.type}</div>
                </div>
                <div className="text-right">
                  <div className="text-[11px] text-[#9CA3AF]">S 점수</div>
                  <div className="text-[20px] font-bold text-[#111827]">{r.S}</div>
                </div>
              </div>

              {/* Badges */}
              <div className="flex flex-wrap gap-1.5 mb-3">
                {r.time <= 70
                  ? <span className="text-[11px] bg-[#DCFCE7] text-[#16A34A] px-2.5 py-1 rounded-full font-medium">✓ 시간 충족</span>
                  : <span className="text-[11px] bg-red-50 text-red-500 px-2.5 py-1 rounded-full font-medium">✕ 시간 초과</span>}
                {r.cost <= 10000
                  ? <span className="text-[11px] bg-[#DCFCE7] text-[#16A34A] px-2.5 py-1 rounded-full font-medium">✓ 비용 충족</span>
                  : <span className="text-[11px] bg-red-50 text-red-500 px-2.5 py-1 rounded-full font-medium">✕ 비용 초과</span>}
                {r.transfers <= 1
                  ? <span className="text-[11px] bg-[#DCFCE7] text-[#16A34A] px-2.5 py-1 rounded-full font-medium">✓ 환승 충족</span>
                  : <span className="text-[11px] bg-red-50 text-red-500 px-2.5 py-1 rounded-full font-medium">✕ 환승 초과</span>}
              </div>

              {/* λ·d bars */}
              {[
                { l: 'GC', v: r.S! * 0.55, c: '#2F7BF6' },
                { l: 'Knee', v: r.S! * 0.38, c: '#8B5CF6' },
                { l: '시간', v: r.S! * 0.25, c: '#F97316' },
              ].map(({ l, v, c }) => (
                <div key={l} className="flex items-center gap-2 mb-1.5">
                  <span className="text-[10px] text-[#9CA3AF] w-8">{l}</span>
                  <div className="flex-1 h-1.5 bg-[#F3F4F6] rounded-full overflow-hidden">
                    <div className="h-full rounded-full" style={{ width: `${Math.min(v * 350, 100)}%`, background: c }} />
                  </div>
                  <span className="text-[10px] font-semibold text-[#374151] w-10 text-right">{v.toFixed(3)}</span>
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── App shell ────────────────────────────────────────────────────────────────
export default function App() {
  const [screen, setScreen] = useState<Screen>('home')
  const [origin, setOrigin] = useState<Place | null>(null)
  const [destination, setDestination] = useState<Place | null>(null)
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)
  const [analyzeError, setAnalyzeError] = useState<string | null>(null)
  const [analyzeProgress, setAnalyzeProgress] = useState(0)
  const [analyzeStage, setAnalyzeStage] = useState('서버를 깨우는 중이에요')
  const [selectedRoute, setSelectedRoute] = useState<RouteCandidate | null>(null)
  const [departTime, setDepartTime] = useState<string | null>(null)
  const [ranking, setRanking] = useState<RankResponse | null>(null)
  const [rankError, setRankError] = useState<string | null>(null)
  const [rankingBusy, setRankingBusy] = useState(false)
  const [pendingLimits, setPendingLimits] = useState<RankLimits | null>(null)
  const [appliedLimits, setAppliedLimits] = useState<RankLimits | null>(null)
  const requestId = useRef(0)
  const estimateId = useRef(0)
  const analysisRef = useRef<AnalyzeResponse | null>(null)
  const analyzePromiseRef = useRef<Promise<AnalyzeResponse> | null>(null)

  const storedProfile = readStorage<SpProfile>(PROFILE_KEY)
  const [survey, setSurvey] = useState<SurveyResponse | null>(null)
  const [surveyBusy, setSurveyBusy] = useState(false)
  const [surveyError, setSurveyError] = useState<string | null>(null)
  const [answers, setAnswers] = useState<Record<number, number>>({})
  const [estimateBusy, setEstimateBusy] = useState(false)
  const [estimateError, setEstimateError] = useState<string | null>(null)
  const [estimateResult, setEstimateResult] = useState<EstimateResponse | null>(null)
  const [profile, setProfile] = useState<SpProfile | null>(storedProfile)
  const [routeParams, setRouteParams] = useState<RouteParams | null>(() => readStorage<RouteParams>(PARAMS_KEY))
  const [usePersonal, setUsePersonalState] = useState(() => readStorage<boolean>(USE_PERSONAL_KEY) !== false)
  const [manualVot, setManualVotState] = useState<number | null>(() => {
    const stored = readStorage<number | null>(MANUAL_VOT_KEY)
    return typeof stored === 'number' ? stored : null
  })
  const [manualParams, setManualParamsState] = useState<ManualParams>(() => {
    const dedicated = sanitizeManualParams(readStorage(MANUAL_PARAMS_KEY))
    if (Object.keys(dedicated).length) return dedicated
    const store = readStorage<{ manual_params?: unknown }>(USER_STORE_KEY)
    return sanitizeManualParams(store?.manual_params)
  })
  const [rankingBetas, setRankingBetasState] = useState<RankingBetas>(() => {
    const dedicated = parseManualBetas(readStorage(MANUAL_BETAS_KEY))
    if (dedicated) return dedicated
    const store = readStorage<{ manual_betas?: unknown }>(USER_STORE_KEY)
    return parseManualBetas(store?.manual_betas) ?? { ...DEFAULT_BETAS }
  })
  const [rankedBetas, setRankedBetas] = useState<RankingBetas | null>(null)
  const [displayAge, setDisplayAge] = useState(storedProfile?.display_age || storedProfile?.age_group || '')
  const [displayPurpose, setDisplayPurpose] = useState(storedProfile?.display_purpose || storedProfile?.purpose || '')
  const [questionCount, setQuestionCount] = useState(storedProfile?.length || 0)
  const [departMode, setDepartMode] = useState<'now' | 'scheduled'>('now')
  const [scheduledDepart, setScheduledDepartState] = useState<string | null>(null)

  const setUsePersonal = (value: boolean) => {
    setUsePersonalState(value)
    writeStorage(USE_PERSONAL_KEY, value)
  }

  const setManualVot = (value: number | null) => {
    setManualVotState(value)
    writeStorage(MANUAL_VOT_KEY, value)
    persistUserStore(value, profile, routeParams, manualParams, rankingBetas)
  }

  const persistManualParams = (next: ManualParams) => {
    writeStorage(MANUAL_PARAMS_KEY, next)
    persistUserStore(manualVot, profile, routeParams, next, rankingBetas)
  }

  const setManualCoeff = (key: ManualCoeffKey, value: number | null) => {
    if (value != null && parseManualCoeff(key, value) == null) return false
    setManualParamsState(prev => {
      const next: ManualParams = { ...prev }
      if (value == null) delete next[key]
      else next[key] = value
      persistManualParams(next)
      return next
    })
    return true
  }

  const resetManualParams = () => {
    setManualParamsState({})
    persistManualParams({})
  }

  const persistRankingBetas = (next: RankingBetas) => {
    writeStorage(MANUAL_BETAS_KEY, next)
    persistUserStore(manualVot, profile, routeParams, manualParams, next)
  }

  const setRankingBetas = (value: RankingBetas) => {
    const parsed = parseManualBetas(value) ?? { ...DEFAULT_BETAS }
    setRankingBetasState(parsed)
    persistRankingBetas(parsed)
  }

  const resetRankingBetas = () => {
    setRankingBetas({ ...DEFAULT_BETAS })
  }

  const setDepartNow = () => {
    setDepartMode('now')
    setScheduledDepartState(null)
  }

  const setScheduledDepart = (iso: string) => {
    setDepartMode('scheduled')
    setScheduledDepartState(iso)
  }

  const setAnswer = (cardIndex: number, altIndex: number) => {
    setAnswers(prev => ({ ...prev, [cardIndex]: altIndex }))
  }

  const startSurvey = async (age: string, purpose: string, votDirect: number, length: number) => {
    setSurveyBusy(true)
    setSurveyError(null)
    setEstimateError(null)
    setEstimateResult(null)
    setAnswers({})
    try {
      const data = await createSurvey({
        age_group: age,
        purpose,
        vot_direct: votDirect,
        length,
      })
      setSurvey(data)
      setDisplayAge(age)
      setDisplayPurpose(purpose)
      setQuestionCount(length)
    } catch (error) {
      const message = error instanceof Error ? error.message : '설문을 만들지 못했습니다.'
      setSurveyError(message)
      throw error
    } finally {
      setSurveyBusy(false)
    }
  }

  const runEstimate = async () => {
    if (!survey) {
      setEstimateError('설문을 먼저 시작해 주세요.')
      return
    }
    const responses = survey.cards.map((_, index) => answers[index])
    if (responses.some(value => value === undefined || value === null)) {
      setEstimateError('모든 문항에 답해 주세요.')
      return
    }
    const id = ++estimateId.current
    setEstimateBusy(true)
    setEstimateError(null)
    try {
      const result = await estimateProfile(survey.survey_token, responses)
      if (id !== estimateId.current) return
      const softMix = Boolean(result.quality_warnings && result.quality_warnings.length > 0)
      const savedProfile: SpProfile = {
        ...result.profile,
        display_age: displayAge,
        display_purpose: displayPurpose,
        length: questionCount || survey.cards.length,
        soft_mix: softMix,
      }
      setEstimateResult(result)
      setProfile(savedProfile)
      setRouteParams(result.route_params)
      writeStorage(PROFILE_KEY, savedProfile)
      writeStorage(PARAMS_KEY, result.route_params)
      persistUserStore(manualVot, savedProfile, result.route_params, manualParams, rankingBetas)
    } catch (error) {
      if (id !== estimateId.current) return
      setEstimateError(error instanceof Error ? error.message : '추정에 실패했습니다.')
    } finally {
      if (id === estimateId.current) setEstimateBusy(false)
    }
  }

  const restartSurvey = () => {
    setSurvey(null)
    setAnswers({})
    setEstimateResult(null)
    setEstimateError(null)
    setSurveyError(null)
  }

  const runAnalyze = (from: Place, to: Place, when: string) => {
    const id = ++requestId.current
    setAnalyzeError(null)
    setAnalyzeProgress(0)
    setAnalyzeStage('서버를 깨우는 중이에요')
    setAnalysis(null)
    analysisRef.current = null
    setRanking(null)
    setRankError(null)
    setAppliedLimits(null)
    setRankedBetas(null)
    const effective = getEffectiveParams({
      manualVot,
      manualParams,
      routeParams,
      usePersonal,
    })
    const request = analyzeRoutes(
      { name: from.name, lat: from.lat, lng: from.lng },
      { name: to.name, lat: to.lat, lng: to.lng },
      when,
      effective.params,
      info => {
        if (id !== requestId.current) return
        setAnalyzeProgress(info.progress)
        setAnalyzeStage(info.stage)
      },
    )
      .then(result => {
        if (id !== requestId.current) throw new Error('stale')
        analysisRef.current = result
        setAnalysis(result)
        return result
      })
      .catch(error => {
        if (id !== requestId.current) throw error
        if (error instanceof Error && error.message === 'stale') throw error
        const message = error instanceof Error ? error.message : '경로 분석에 실패했습니다.'
        setAnalyzeError(message)
        throw error instanceof Error ? error : new Error(message)
      })
    analyzePromiseRef.current = request
    void request.catch(() => undefined)
  }

  const startAnalyze = (from: Place, to: Place) => {
    const when = departMode === 'scheduled' && scheduledDepart ? scheduledDepart : seoulIso(new Date())
    setOrigin(from)
    setDestination(to)
    setDepartTime(when)
    setSelectedRoute(null)
    runAnalyze(from, to, when)
  }

  const retryAnalyze = () => {
    if (!origin || !destination) return
    const when = departTime ?? new Date().toISOString()
    setDepartTime(when)
    runAnalyze(origin, destination, when)
  }

  const waitForAnalyze = () => {
    if (analysisRef.current) return Promise.resolve(analysisRef.current)
    if (analyzePromiseRef.current) return analyzePromiseRef.current
    return Promise.reject(new Error('경로 분석이 시작되지 않았습니다.'))
  }

  const applyRank = async (limits: RankLimits, candidates?: RouteCandidate[]) => {
    const source = candidates ?? analysisRef.current?.candidates
    if (!source) {
      setRankError('먼저 경로를 찾아 주세요.')
      throw new Error('no analysis')
    }
    setRankingBusy(true)
    setRankError(null)
    try {
      const result = await rankRoutes(source, limits, rankingBetas)
      setRanking(result)
      setAppliedLimits(limits)
      setRankedBetas({ ...rankingBetas })
    } catch (error) {
      const message = error instanceof Error ? error.message : '조건 적용에 실패했습니다.'
      setRankError(message)
      throw error
    } finally {
      setRankingBusy(false)
    }
  }

  const clearRanking = () => {
    setRanking(null)
    setRankError(null)
    setAppliedLimits(null)
    setRankedBetas(null)
  }

  const trip: TripContextValue = {
    origin,
    destination,
    setOrigin,
    setDestination,
    analysis,
    analyzeError,
    analyzeProgress,
    analyzeStage,
    selectedRoute,
    setSelectedRoute,
    departTime,
    ranking,
    rankError,
    rankingBusy,
    pendingLimits,
    appliedLimits,
    setPendingLimits,
    departMode,
    scheduledDepart,
    setDepartNow,
    setScheduledDepart,
    startAnalyze,
    retryAnalyze,
    waitForAnalyze,
    applyRank,
    clearRanking,
    rankedBetas,
  }

  const sp: SpContextValue = {
    survey,
    surveyBusy,
    surveyError,
    answers,
    setAnswer,
    startSurvey,
    estimateBusy,
    estimateError,
    estimateResult,
    runEstimate,
    profile,
    routeParams,
    usePersonal,
    setUsePersonal,
    displayAge,
    displayPurpose,
    questionCount,
    restartSurvey,
    manualVot,
    setManualVot,
    manualParams,
    setManualCoeff,
    resetManualParams,
    rankingBetas,
    setRankingBetas,
    resetRankingBetas,
  }

  const screens: Record<Screen, React.ReactNode> = {
    'home': <HomeScreen onNav={setScreen} />,
    'sp-setup': <SPSetupScreen onNav={setScreen} />,
    'sp-question': <SPQuestionScreen onNav={setScreen} />,
    'sp-complete': <SPCompleteScreen onNav={setScreen} />,
    'sp-profile': <SPProfileScreen onNav={setScreen} />,
    'search-input': <SearchInputScreen onNav={setScreen} />,
    'analyzing': <AnalyzingScreen onNav={setScreen} />,
    'results': <ResultsScreen onNav={setScreen} />,
    'reservation': <ReservationScreen onNav={setScreen} />,
    'regret': <RegretScreen onNav={setScreen} />,
    'detail': <DetailScreen onNav={setScreen} />,
  }

  return (
    <TripContext.Provider value={trip}>
      <SpContext.Provider value={sp}>
        <div className="min-h-dvh bg-[#E8EAEF] flex justify-center">
          <div className="relative w-full max-w-[430px] h-dvh overflow-hidden bg-white">
            {screens[screen]}
          </div>
        </div>
      </SpContext.Provider>
    </TripContext.Provider>
  )
}
