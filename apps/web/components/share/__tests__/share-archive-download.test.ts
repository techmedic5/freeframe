import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { downloadShareArchive } from '../share-download'

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@/lib/auth', () => ({ getAccessToken: () => 'existing-auth-token' }))

const job = { id: 'job-id', status: 'ready', file_count: 2, completed_files: 0, filename: 'Media.zip', error: null }

describe('guest ZIP download', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.post.mockResolvedValue(job)
    mocks.get.mockResolvedValue({ ...job, status: 'complete', completed_files: 2 })
    vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  it('submits one native POST download with session and auth in its body', async () => {
    let action = ''
    let fields: Record<string, string> = {}
    vi.mocked(HTMLFormElement.prototype.submit).mockImplementation(function (this: HTMLFormElement) {
      action = this.action
      fields = Object.fromEntries(new FormData(this).entries()) as Record<string, string>
      expect(this.method).toBe('post')
      expect(document.querySelector(`iframe[name="${this.target}"]`)).not.toBeNull()
    })
    const status = vi.fn()
    const done = downloadShareArchive('share-token', 'folder-id', 'password-session', status)
    await vi.advanceTimersByTimeAsync(3000)
    await done
    expect(mocks.post).toHaveBeenCalledWith('/share/share-token/archives?share_session=password-session', { folder_id: 'folder-id' })
    expect(action).toContain('/share/share-token/archives/job-id/download')
    expect(action).not.toContain('existing-auth-token')
    expect(fields).toEqual({ share_session: 'password-session', access_token: 'existing-auth-token' })
    expect(HTMLFormElement.prototype.submit).toHaveBeenCalledOnce()
    expect(document.querySelector('iframe')).toBeNull()
    expect(status).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'complete' }))
  })

  it('keeps the download alive through transient status errors', async () => {
    mocks.get.mockRejectedValueOnce(new TypeError('Network unavailable'))
    const done = downloadShareArchive('token', null)
    await vi.advanceTimersByTimeAsync(3000)
    expect(document.querySelector('iframe')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(3000)
    await done
    expect(mocks.get).toHaveBeenCalledTimes(2)
  })

  it('reports server failure instead of claiming a completed download', async () => {
    mocks.get.mockResolvedValue({ ...job, status: 'failed', error: 'A file is no longer available' })
    const done = downloadShareArchive('token', null)
    const rejected = expect(done).rejects.toThrow('A file is no longer available')
    await vi.advanceTimersByTimeAsync(3000)
    await rejected
  })

  it('does not submit a download after preparation is refused', async () => {
    mocks.post.mockRejectedValue(new Error('Downloads are not allowed'))
    await expect(downloadShareArchive('token', null)).rejects.toThrow('Downloads are not allowed')
    expect(HTMLFormElement.prototype.submit).not.toHaveBeenCalled()
  })
})
