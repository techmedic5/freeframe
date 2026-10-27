'use client'

import * as React from 'react'

interface AssetSwipeOptions {
  onPrevious?: () => void
  onNext?: () => void
  canSwipe: () => boolean
  canStart?: (target: Element) => boolean
}

export function useAssetSwipe({ onPrevious, onNext, canSwipe, canStart }: AssetSwipeOptions) {
  const start = React.useRef<{ x: number; y: number; at: number; id: number } | null>(null)

  return {
    onTouchStartCapture(event: React.TouchEvent) {
      start.current = null
      if (event.touches.length !== 1 || !canSwipe()) return
      const target = event.target
      if (!(target instanceof Element) || (canStart && !canStart(target))) return
      if (target.closest('button, a, input, textarea, select, [role="slider"], [contenteditable="true"]')) return
      const touch = event.touches[0]
      // Leave browser back/forward gestures at the screen edges alone.
      if (touch.clientX < 24 || touch.clientX > window.innerWidth - 24) return
      start.current = { x: touch.clientX, y: touch.clientY, at: Date.now(), id: touch.identifier }
    },
    onTouchMoveCapture(event: React.TouchEvent) {
      if (event.touches.length !== 1 || !canSwipe()) start.current = null
    },
    onTouchEndCapture(event: React.TouchEvent) {
      const from = start.current
      start.current = null
      if (!from || event.touches.length || !canSwipe() || Date.now() - from.at > 800) return
      const touch = Array.from(event.changedTouches).find((item) => item.identifier === from.id)
      if (!touch) return
      const dx = touch.clientX - from.x
      const dy = touch.clientY - from.y
      if (Math.abs(dx) < 64 || Math.abs(dx) < Math.abs(dy) * 1.5) return
      const navigate = dx < 0 ? onNext : onPrevious
      if (!navigate) return
      event.preventDefault()
      navigate()
    },
    onTouchCancelCapture() { start.current = null },
  }
}
