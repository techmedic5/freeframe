import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useAssetSwipe } from '../use-asset-swipe'

function Surface({ enabled = true, next = vi.fn(), previous = vi.fn(), videoOnly = false }) {
  const swipe = useAssetSwipe({ onNext: next, onPrevious: previous, canSwipe: () => enabled,
    canStart: videoOnly ? (target) => target.tagName === 'VIDEO' : undefined })
  return <div data-testid="surface" {...swipe}><video data-testid="video" /><button>Zoom</button><div role="slider" data-testid="timeline" /></div>
}

function touch(x: number, y = 100, identifier = 1) { return { clientX: x, clientY: y, identifier } }
function swipe(target: Element, from = touch(250), to = touch(100)) {
  fireEvent.touchStart(target, { touches: [from] })
  fireEvent.touchEnd(target, { touches: [], changedTouches: [to] })
}

describe('asset swipe gestures', () => {
  it('advances on horizontal swipes in either direction', () => {
    const next = vi.fn(), previous = vi.fn()
    render(<Surface next={next} previous={previous} />)
    swipe(screen.getByTestId('surface'))
    swipe(screen.getByTestId('surface'), touch(100), touch(250))
    expect(next).toHaveBeenCalledOnce()
    expect(previous).toHaveBeenCalledOnce()
  })

  it('does not use short, vertical, screen-edge or disabled gestures', () => {
    const next = vi.fn()
    const { rerender } = render(<Surface next={next} />)
    const target = screen.getByTestId('surface')
    swipe(target, touch(250), touch(220))
    swipe(target, touch(250), touch(100, 300))
    swipe(target, touch(10), touch(200))
    rerender(<Surface next={next} enabled={false} />)
    swipe(target)
    expect(next).not.toHaveBeenCalled()
  })

  it('does not convert a pinch, cancellation, or control interaction into navigation', () => {
    const next = vi.fn()
    render(<Surface next={next} />)
    const target = screen.getByTestId('surface')
    fireEvent.touchStart(target, { touches: [touch(250)] })
    fireEvent.touchMove(target, { touches: [touch(200), touch(300, 100, 2)] })
    fireEvent.touchEnd(target, { touches: [], changedTouches: [touch(100)] })
    fireEvent.touchStart(target, { touches: [touch(250)] })
    fireEvent.touchCancel(target)
    fireEvent.touchEnd(target, { touches: [], changedTouches: [touch(100)] })
    swipe(screen.getByRole('button'))
    swipe(screen.getByTestId('timeline'))
    expect(next).not.toHaveBeenCalled()
  })

  it('restricts video swipes to the picture, leaving the player timeline alone', () => {
    const next = vi.fn()
    render(<Surface next={next} videoOnly />)
    swipe(screen.getByTestId('surface'))
    swipe(screen.getByTestId('timeline'))
    expect(next).not.toHaveBeenCalled()
    swipe(screen.getByTestId('video'))
    expect(next).toHaveBeenCalledOnce()
  })
})
