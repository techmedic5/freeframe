import * as React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageViewer } from '../image-viewer'
import { useReviewStore } from '@/stores/review-store'
import type { Asset, AssetVersion } from '@/types'

const transform = vi.hoisted(() => ({ changed: null as null | ((_: unknown, state: { scale: number }) => void) }))
vi.mock('react-zoom-pan-pinch', () => ({
  TransformWrapper: ({ children, onTransformed }: { children: () => React.ReactNode; onTransformed: typeof transform.changed }) => {
    transform.changed = onTransformed
    return <>{children()}</>
  },
  TransformComponent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  useControls: () => ({ zoomIn() {}, zoomOut() {}, resetTransform() {}, centerView() {} }),
}))
vi.mock('../review-provider', () => ({ useReview: () => ({ shareToken: 'token', shareSession: 'session' }) }))

const asset = { id: 'image', name: 'Photo', asset_type: 'image' } as Asset
const version = { id: 'version', files: [] } as unknown as AssetVersion
function gesture(image: Element) {
  fireEvent.touchStart(image, { touches: [{ identifier: 1, clientX: 250, clientY: 100 }] })
  fireEvent.touchEnd(image, { touches: [], changedTouches: [{ identifier: 1, clientX: 100, clientY: 100 }] })
}

beforeEach(() => {
  useReviewStore.getState().reset()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ url: '/photo.jpg' }) })))
})
afterEach(() => vi.unstubAllGlobals())

describe('image viewer sibling swipes', () => {
  it('allows a fitted photo to advance but preserves zoom/pan and annotation gestures', async () => {
    const next = vi.fn()
    render(<ImageViewer asset={asset} version={version} onNextAsset={next} />)
    const image = await screen.findByAltText('Photo')
    gesture(image)
    expect(next).toHaveBeenCalledOnce()
    act(() => transform.changed?.({}, { scale: 2 }))
    gesture(image)
    expect(next).toHaveBeenCalledTimes(1)
    act(() => transform.changed?.({}, { scale: 1 }))
    act(() => useReviewStore.getState().setIsDrawingMode(true))
    gesture(image)
    expect(next).toHaveBeenCalledTimes(1)
  })
})
