import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FolderShareViewer } from '../folder-share-viewer'
import type { ShareAssetNavigation } from '../share-review-screen'

vi.mock('../share-review-screen', () => ({
  ShareReviewScreen: ({ assetName, navigation, onBack }: { assetName: string; navigation: ShareAssetNavigation; onBack: () => void }) => (
    <div>
      <h1>{assetName}</h1>
      <button onClick={onBack}>Back</button>
      <button onClick={navigation.onPrevious} disabled={navigation.loading || !!navigation.error || !navigation.onPrevious}>Previous</button>
      <span>{navigation.loading ? 'Loading items' : `${navigation.position} of ${navigation.total}`}</span>
      <button onClick={navigation.onNext} disabled={navigation.loading || !!navigation.error || !navigation.onNext}>Next</button>
      {navigation.error && <button onClick={navigation.onRetry}>Retry</button>}
    </div>
  ),
}))

function asset(name: string, size = 1) {
  return { id: name, name, asset_type: 'image', thumbnail_url: null, created_at: '2026-01-01T00:00:00Z', file_size: size }
}

function mount(sort = 'name') {
  render(<FolderShareViewer token="token" shareSession="session value" folderName="Photos" title="Project" description={null}
    permission="view" allowDownload={false} showVersions={false} appearance={{ open_in_viewer: true, sort_by: sort } as never} branding={null} />)
}

afterEach(() => vi.unstubAllGlobals())

describe('guest gallery asset navigation', () => {
  it('loads every remaining page with the share session before following the sorted order', async () => {
    const pages = [[asset('Delta')], [asset('Alpha')], [asset('Bravo')]]
    const fetcher = vi.fn(async (url: string) => ({ ok: true, json: async () => ({
      assets: pages[Number(new URL(url).searchParams.get('page')) - 1], subfolders: [], total: 3,
    }) }))
    vi.stubGlobal('fetch', fetcher)
    mount()
    fireEvent.doubleClick(await screen.findByText('Delta'))
    await screen.findByText('3 of 3')
    expect(screen.getByText('Next')).toBeDisabled()
    fireEvent.click(screen.getByText('Previous'))
    expect(screen.getByRole('heading')).toHaveTextContent('Bravo')
    fireEvent.click(screen.getByText('Previous'))
    expect(screen.getByRole('heading')).toHaveTextContent('Alpha')
    expect(screen.getByText('Previous')).toBeDisabled()
    expect(fetcher).toHaveBeenCalledTimes(3)
    for (const [url] of fetcher.mock.calls) expect(new URL(url).searchParams.get('share_session')).toBe('session value')
  })

  it('keeps the search filter and file-size order when later pages load', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => ({
      assets: new URL(url).searchParams.get('page') === '1'
        ? [asset('Match middle', 20), asset('Other', 50)]
        : [asset('Match largest', 30), asset('Match smallest', 10)], subfolders: [], total: 4,
    }) })))
    mount('file_size')
    await screen.findByText('Match middle')
    fireEvent.change(screen.getByPlaceholderText('Search assets…'), { target: { value: 'Match' } })
    fireEvent.doubleClick(screen.getByText('Match middle'))
    await screen.findByText('2 of 3')
    fireEvent.click(screen.getByText('Next'))
    expect(screen.getByRole('heading')).toHaveTextContent('Match smallest')
    expect(screen.getByText('Next')).toBeDisabled()
  })

  it('keeps the current media visible and permits a retry after pagination fails', async () => {
    let failed = false
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const first = new URL(url).searchParams.get('page') === '1'
      if (!first && !failed) { failed = true; return { ok: false } }
      return { ok: true, json: async () => ({ assets: [asset(first ? 'Alpha' : 'Bravo')], subfolders: [], total: 2 }) }
    }))
    mount()
    fireEvent.doubleClick(await screen.findByText('Alpha'))
    fireEvent.click(await screen.findByText('Retry'))
    await waitFor(() => expect(screen.getByText('Next')).not.toBeDisabled())
    fireEvent.click(screen.getByText('Next'))
    expect(screen.getByRole('heading')).toHaveTextContent('Bravo')
  })
})
