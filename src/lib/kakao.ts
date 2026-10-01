declare global {
  interface Window {
    kakao: any
  }
}

let loadPromise: Promise<void> | null = null

function isKakaoServicesReady() {
  return Boolean(window.kakao?.maps?.services?.Places)
}

export function loadKakao(): Promise<void> {
  if (typeof window === 'undefined') {
    return Promise.reject(new Error('Kakao Maps SDK can only load in the browser'))
  }

  if (isKakaoServicesReady()) {
    return Promise.resolve()
  }

  if (loadPromise) return loadPromise

  const appkey = import.meta.env.VITE_KAKAO_JS_KEY
  if (!appkey) {
    console.warn('VITE_KAKAO_JS_KEY가 설정되지 않았습니다')
    return Promise.reject(new Error('VITE_KAKAO_JS_KEY is not set'))
  }

  loadPromise = new Promise<void>((resolve, reject) => {
    const fail = (error: Error) => {
      loadPromise = null
      reject(error)
    }

    const finish = () => {
      if (!window.kakao?.maps?.load) {
        fail(new Error('Kakao Maps SDK did not initialize'))
        return
      }
      window.kakao.maps.load(() => {
        if (!isKakaoServicesReady()) {
          fail(new Error('Kakao Maps services library did not load'))
          return
        }
        resolve()
      })
    }

    const existing = document.querySelector<HTMLScriptElement>('script[data-kakao-maps-sdk]')
    if (existing) {
      if (window.kakao?.maps?.load) {
        finish()
      } else {
        existing.addEventListener('load', finish, { once: true })
        existing.addEventListener(
          'error',
          () => fail(new Error('Failed to load Kakao Maps SDK')),
          { once: true },
        )
      }
      return
    }

    const script = document.createElement('script')
    script.type = 'text/javascript'
    script.charset = 'UTF-8'
    script.src = `//dapi.kakao.com/v2/maps/sdk.js?appkey=${appkey}&libraries=services&autoload=false`
    script.async = true
    script.dataset.kakaoMapsSdk = 'true'
    script.onload = finish
    script.onerror = () => fail(new Error('Failed to load Kakao Maps SDK'))
    document.head.appendChild(script)
  })

  return loadPromise
}
