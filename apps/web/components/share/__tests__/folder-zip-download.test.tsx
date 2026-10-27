import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FolderShareViewer } from '../folder-share-viewer'
import { downloadShareArchive } from '../share-download'

vi.mock('../share-download', async (original) => ({
  ...await original<typeof import('../share-download')>(),
  downloadShareArchive: vi.fn(),
}))

function mount(allowDownload = true) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({
    assets: [], subfolders: [], total: 0,
  }) })))
  render(<FolderShareViewer token="share-token" shareSession="guest-session" folderName="Photos" title="Project"
    description={null} permission="view" allowDownload={allowDownload} showVersions={false}
    appearance={{ layout: 'grid', theme: 'dark', accent_color: null, open_in_viewer: true,
      sort_by: 'name', card_size: 'm', aspect_ratio: 'landscape', thumbnail_scale: 'fit', show_card_info: true }}
    branding={null} />)
}

afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })

describe('guest Download All ZIP', () => {
  it('starts one archive with the guest session and reports server completion accurately', async () => {
    let finish!: () => void
    vi.mocked(downloadShareArchive).mockImplementation(async (_token, _folder, _session, onStatus) => {
      const status = { id: 'job', status: 'downloading' as const, file_count: 12, completed_files: 2, filename: 'Photos.zip', error: null }
      onStatus?.(status)
      await new Promise<void>((resolve) => { finish = resolve })
      onStatus?.({ ...status, status: 'complete', completed_files: 12 })
    })
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Download All' }))
    expect(downloadShareArchive).toHaveBeenCalledWith('share-token', null, 'guest-session', expect.any(Function))
    expect(screen.getByRole('button', { name: 'Downloading ZIP…' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('2 of 12 files')
    await act(async () => finish())
    expect(await screen.findByRole('button', { name: 'Download All' })).not.toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('Check Downloads to confirm it was saved')
    expect(downloadShareArchive).toHaveBeenCalledTimes(1)
  })

  it('shows a failed archive and permits a deliberate retry', async () => {
    vi.mocked(downloadShareArchive).mockRejectedValue(new Error('A file is no longer available. Please retry.'))
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Download All' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A file is no longer available')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download All' })).not.toBeDisabled())
  })

  it('does not offer a ZIP when the link disallows downloads', () => {
    mount(false)
    expect(screen.queryByRole('button', { name: 'Download All' })).not.toBeInTheDocument()
    expect(downloadShareArchive).not.toHaveBeenCalled()
  })
})
