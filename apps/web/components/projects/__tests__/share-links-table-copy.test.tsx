import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ShareLinksTable } from '../share-links-table'
import type { ShareLinkListItem } from '@/types'

// FreeFrame served over plain HTTP (a LAN IP) is not a secure context, so
// navigator.clipboard is undefined and a direct writeText call throws. The
// copy button must go through copyToClipboard and its execCommand fallback.

let copied: string | null
let execCommand: ReturnType<typeof vi.fn>

beforeEach(() => {
  copied = null
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
  execCommand = vi.fn(() => {
    const event = new Event('copy', { bubbles: true, cancelable: true })
    Object.defineProperty(event, 'clipboardData', {
      value: { setData: (_t: string, v: string) => { copied = v } },
    })
    ;(document.activeElement ?? document.body).dispatchEvent(event)
    return true
  })
  document.execCommand = execCommand as never
})

afterEach(() => {
  vi.restoreAllMocks()
})

const link: ShareLinkListItem = {
  id: 'l1', token: 'tok123', title: 'Launch cut', description: null,
  is_enabled: true, permission: 'view', share_type: 'asset', target_name: 'cut.mp4',
  view_count: 0, last_viewed_at: null, created_at: '2026-01-01T10:00:00.000Z',
  expires_at: null, is_expired: false,
} as ShareLinkListItem

describe('ShareLinksTable copy button on plain HTTP', () => {
  it('copies the link through the execCommand fallback and shows the copied state', async () => {
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)

    const { container } = render(
      <ShareLinksTable
        shareLinks={[link]}
        onSelectLink={() => {}}
        onToggleEnabled={() => {}}
        onViewActivity={() => {}}
        frontendUrl="http://192.168.1.20:3000"
      />,
    )
    expect(container.querySelector('.lucide-check')).toBeNull()

    fireEvent.click(screen.getByTitle('Copy link'))

    await waitFor(() => expect(container.querySelector('.lucide-check')).not.toBeNull())
    expect(copied).toBe('http://192.168.1.20:3000/share/tok123')
    await new Promise((r) => setTimeout(r, 0))
    expect(unhandled).not.toHaveBeenCalled()
    process.off('unhandledRejection', unhandled)
  })

  it('does not show the copied state when the copy fails', async () => {
    execCommand.mockImplementation(() => false)
    const { container } = render(
      <ShareLinksTable
        shareLinks={[link]}
        onSelectLink={() => {}}
        onToggleEnabled={() => {}}
        onViewActivity={() => {}}
        frontendUrl="http://192.168.1.20:3000"
      />,
    )
    fireEvent.click(screen.getByTitle('Copy link'))
    await new Promise((r) => setTimeout(r, 20))
    expect(container.querySelector('.lucide-check')).toBeNull()
  })
})
