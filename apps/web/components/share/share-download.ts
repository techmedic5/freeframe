/**
 * Download helpers shared by the share-link grid and the share review screen.
 *
 * They lived in folder-share-viewer until the review screen moved out of it
 * (#117, #123); importing them back from there would make the two modules
 * circular, so they live here instead.
 */
import type { DownloadOption } from '@/types'
import { api } from '@/lib/api'
import { getAccessToken } from '@/lib/auth'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export interface ShareArchiveStatus {
  id: string
  status: 'ready' | 'downloading' | 'complete' | 'failed'
  file_count: number
  completed_files: number
  filename: string
  error: string | null
}

/** One native browser download; neither fetch().blob() nor an in-memory ZIP. */
export async function downloadShareArchive(
  token: string, folderId: string | null, shareSession?: string | null,
  onStatus?: (status: ShareArchiveStatus) => void,
): Promise<void> {
  const sessionParam = shareSession ? `?share_session=${encodeURIComponent(shareSession)}` : ''
  const path = `/share/${encodeURIComponent(token)}/archives`
  const prepared = await api.post<ShareArchiveStatus>(path + sessionParam, { folder_id: folderId })
  onStatus?.(prepared)
  const iframe = document.createElement('iframe')
  iframe.name = `archive-${prepared.id}`
  iframe.title = 'ZIP download'
  iframe.style.display = 'none'
  const form = document.createElement('form')
  form.method = 'POST'
  form.action = `${API_URL}${path}/${encodeURIComponent(prepared.id)}/download`
  form.target = iframe.name
  form.style.display = 'none'
  for (const [name, value] of Object.entries({ share_session: shareSession, access_token: getAccessToken() })) {
    if (!value) continue
    const input = document.createElement('input')
    input.type = 'hidden'
    input.name = name
    input.value = value
    form.appendChild(input)
  }
  document.body.append(iframe, form)
  let releaseFrame = true
  try {
    form.submit()
    releaseFrame = false
    form.remove()
    const started = Date.now()
    let consecutiveFailures = 0
    while (Date.now() - started < 4 * 60 * 60 * 1000) {
      await new Promise((resolve) => setTimeout(resolve, 3000))
      let state: ShareArchiveStatus
      try {
        state = await api.get<ShareArchiveStatus>(`${path}/${prepared.id}${sessionParam}`)
        consecutiveFailures = 0
      } catch {
        consecutiveFailures += 1
        if (consecutiveFailures < 10) continue
        throw new Error('ZIP status could not be checked. Your browser may still be downloading; check its Downloads.')
      }
      onStatus?.(state)
      if (state.status === 'failed' || state.status === 'complete') releaseFrame = true
      if (state.status === 'failed') throw new Error(state.error || 'ZIP download failed; please try again')
      if (state.status === 'complete') return
      if (state.status === 'ready' && Date.now() - started > 30000) {
        releaseFrame = true
        throw new Error('Your browser did not start the ZIP download. Please try again.')
      }
    }
    releaseFrame = true
    throw new Error('ZIP download timed out. Try downloading a smaller folder.')
  } finally {
    form.remove()
    if (releaseFrame) iframe.remove()
    else setTimeout(() => iframe.remove(), 4 * 60 * 60 * 1000)
  }
}

export function triggerDownload(url: string) {
  // Let the server's Content-Disposition filename win — don't set `a.download`,
  // since it would strip the extension the backend appended.
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener noreferrer'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  setTimeout(() => a.remove(), 1000)
}

/**
 * `rendition` is an id from fetchShareDownloadOptions; left out, the transcode.
 * `preferOriginal` (Download All) asks for the original when the instance allows it.
 */
export async function fetchDownloadUrl(
  token: string, assetId: string, shareSession?: string | null, rendition?: string,
  preferOriginal = false,
): Promise<string | null> {
  let sp = shareSession ? `&share_session=${encodeURIComponent(shareSession)}` : ''
  if (rendition) sp += `&rendition=${encodeURIComponent(rendition)}`
  if (preferOriginal) sp += '&prefer_original=true'
  try {
    const response = await fetch(`${API_URL}/share/${token}/stream/${assetId}?download=true${sp}`)
    if (!response.ok) return null
    const data = await response.json()
    return data?.url ?? null
  } catch {
    return null
  }
}

export async function handleDownload(
  token: string, assetId: string, shareSession?: string | null, rendition?: string,
) {
  const url = await fetchDownloadUrl(token, assetId, shareSession, rendition)
  if (url) triggerDownload(url)
}

/** What an asset in a share link can be downloaded as: the original, then the transcode. */
export async function fetchShareDownloadOptions(
  token: string, assetId: string, shareSession?: string | null,
): Promise<DownloadOption[] | null> {
  const sp = shareSession ? `?share_session=${encodeURIComponent(shareSession)}` : ''
  try {
    const response = await fetch(`${API_URL}/share/${token}/downloads/${assetId}${sp}`)
    if (!response.ok) return null
    return (await response.json()) as DownloadOption[]
  } catch {
    return null
  }
}
