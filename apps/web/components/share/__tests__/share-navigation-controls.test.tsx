import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ShareReviewScreen, type ShareAssetNavigation } from '../share-review-screen'
import { useReviewStore } from '@/stores/review-store'

beforeEach(() => {
  useReviewStore.getState().reset()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ name: 'Photo', asset_type: 'image' }) })))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})
afterEach(() => vi.unstubAllGlobals())

function Viewer({ navigation }: { navigation?: ShareAssetNavigation }) {
  return <ShareReviewScreen token="token" assetId="asset" assetName="Photo" permission="view" allowDownload={false} showVersions={false} navigation={navigation} />
}

describe('share viewer navigation controls', () => {
  it('provides labeled sibling controls without hijacking left/right keyboard shortcuts', async () => {
    const next = vi.fn(), previous = vi.fn()
    render(<Viewer navigation={{ position: 2, total: 3, loading: false, error: null, onPrevious: previous, onNext: next, onRetry() {} }} />)
    const nextButton = await screen.findByRole('button', { name: 'Next asset' }, { timeout: 3000 })
    expect(screen.getByText('2 of 3')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'ArrowRight' })
    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(next).not.toHaveBeenCalled()
    expect(previous).not.toHaveBeenCalled()
    fireEvent.click(nextButton)
    fireEvent.click(screen.getByRole('button', { name: 'Previous asset' }))
    expect(next).toHaveBeenCalledOnce()
    expect(previous).toHaveBeenCalledOnce()
    act(() => useReviewStore.getState().setIsDrawingMode(true))
    expect(nextButton).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Previous asset' })).toBeDisabled()
  })

  it('leaves single-asset share links without sibling controls', async () => {
    render(<Viewer />)
    await screen.findByText('Photo', {}, { timeout: 3000 })
    expect(screen.queryByRole('navigation', { name: 'Media navigation' })).not.toBeInTheDocument()
  })
})
