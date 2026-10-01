import { useEffect, useState } from 'react'
import { loadKakao } from '@/lib/kakao'

export type Place = {
  name: string
  address: string
  lat: number
  lng: number
}

function mapPlaces(data: any[]): Place[] {
  return data.slice(0, 5).map(item => ({
    name: item.place_name,
    address: item.road_address_name || item.address_name,
    lat: Number(item.y),
    lng: Number(item.x),
  }))
}

export function usePlaceSearch(query: string) {
  const [places, setPlaces] = useState<Place[]>([])
  const [error, setError] = useState(false)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    const keyword = query.trim()
    if (!keyword) {
      setPlaces([])
      setError(false)
      setReady(false)
      return
    }

    setReady(false)
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        await loadKakao()
        if (cancelled) return

        const placesService = new window.kakao.maps.services.Places()
        placesService.keywordSearch(keyword, (data: any[], status: string) => {
          if (cancelled) return
          const Status = window.kakao.maps.services.Status
          if (status === Status.OK) {
            setPlaces(mapPlaces(data))
            setError(false)
          } else if (status === Status.ZERO_RESULT) {
            setPlaces([])
            setError(false)
          } else {
            console.error(
              'Kakao 장소 검색 실패:',
              status,
              'JavaScript 키의 SDK 도메인에 현재 사이트 주소를 등록하고, 카카오맵 사용 설정을 ON으로 바꿔 주세요.',
            )
            setPlaces([])
            setError(true)
          }
          setReady(true)
        })
      } catch (error) {
        if (cancelled) return
        console.error('Kakao 장소 검색 실패:', error)
        setPlaces([])
        setError(true)
        setReady(true)
      }
    }, 300)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [query])

  return { places, error, ready }
}
