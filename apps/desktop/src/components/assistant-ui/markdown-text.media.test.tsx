import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { MarkdownImage, MarkdownTextContent } from './markdown-text'

const REMOTE_IMAGE_PATH = '/home/user/project/images/remote-preview.png'
const REMOTE_IMAGE_DATA_URL = 'data:image/png;base64,cmVtb3RlLWltYWdl'

describe('MarkdownTextContent remote images', () => {
  const api = vi.fn(async ({ path }: { path: string }) => {
    if (path.startsWith('/api/fs/read-data-url?')) {
      return { dataUrl: REMOTE_IMAGE_DATA_URL }
    }

    throw new Error(`unexpected path ${path}`)
  })

  let originalDesktop: typeof window.hermesDesktop

  beforeEach(() => {
    api.mockClear()
    originalDesktop = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })
    $connection.set({
      mode: 'remote',
      profile: 'remote-work',
      baseUrl: 'https://gw',
      token: 'secret'
    } as never)
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: originalDesktop
    })
  })

  it('passes the gateway bridge data URL through Streamdown to the zoomable image', async () => {
    render(<MarkdownTextContent isRunning={false} text={`![Remote preview](${REMOTE_IMAGE_PATH})`} />)

    const image = await screen.findByRole('img', { name: 'Remote preview' })

    expect(image.getAttribute('src')).toBe(REMOTE_IMAGE_DATA_URL)
    expect(JSON.parse(image.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      downloadUrl:
        'https://gw/api/files/download?path=%2Fhome%2Fuser%2Fproject%2Fimages%2Fremote-preview.png&token=secret',
      remote: true,
      source: REMOTE_IMAGE_PATH
    })
    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2Fhome%2Fuser%2Fproject%2Fimages%2Fremote-preview.png',
      profile: 'remote-work'
    })
  })

  it('preserves the remote source when an image arrives as a media attachment', async () => {
    render(
      <MarkdownTextContent
        isRunning={false}
        text="[preview](#media:%2Fhome%2Fuser%2Fproject%2Fimages%2Fremote-preview.png)"
      />
    )

    const image = await screen.findByRole('img', { name: 'remote-preview.png' })

    expect(JSON.parse(image.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      remote: true,
      source: REMOTE_IMAGE_PATH
    })
  })
})

// Regression for #40896: generated media often arrives as image markdown
// (`![clip](clip.mp4)`). A raw <img> with a video/audio source paints a
// broken-image icon even though the file is valid, so MarkdownImage must route
// video/audio sources to the proper <video>/<audio> element.
describe('MarkdownImage media routing', () => {
  afterEach(cleanup)

  it('renders a <video> (not a broken <img>) for a video source', async () => {
    const { container } = render(<MarkdownImage alt="clip" src="file:///tmp/clip.mp4" />)

    await waitFor(() => expect(container.querySelector('video')).not.toBeNull())
    expect(container.querySelector('img')).toBeNull()

    const contextTarget = container.querySelector('video')?.closest('[data-hermes-context-file]')
    expect(contextTarget).not.toBeNull()
    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'clip.mp4',
      remote: false,
      source: 'file:///tmp/clip.mp4'
    })
  })

  it('renders an <audio> element for an audio source', async () => {
    const { container } = render(<MarkdownImage alt="note" src="file:///tmp/note.mp3" />)

    await waitFor(() => expect(container.querySelector('audio')).not.toBeNull())
    expect(container.querySelector('img')).toBeNull()

    const contextTarget = container.querySelector('audio')?.closest('[data-hermes-context-file]')
    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'note.mp3',
      remote: false,
      source: 'file:///tmp/note.mp3'
    })
  })

  it('renders a context descriptor on a regular file attachment', async () => {
    render(<MarkdownTextContent isRunning={false} text="[report](#media:%2Ftmp%2Ffactory%20review.pdf)" />)

    const label = await screen.findByRole('link', { name: 'Open factory review.pdf' })
    const contextTarget = label.closest('[data-hermes-context-file]')

    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'factory review.pdf',
      remote: false,
      source: '/tmp/factory review.pdf'
    })
  })

  it('renders a context descriptor on an explicit file preview link', async () => {
    render(<MarkdownTextContent isRunning={false} text="[spec](#preview/%2Ftmp%2Fproduct%20spec.pdf)" />)

    const label = await screen.findByText('product spec.pdf')
    const contextTarget = label.closest('[data-hermes-context-file]')

    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'product spec.pdf',
      remote: false,
      source: '/tmp/product spec.pdf'
    })
  })

  it('still renders an <img> for an image source', async () => {
    const { container } = render(<MarkdownImage alt="pic" src="file:///tmp/pic.png" />)

    await waitFor(() => expect(container.querySelector('img')).not.toBeNull())
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('audio')).toBeNull()

    const image = container.querySelector('img')
    const contextTarget = image?.closest('[data-hermes-context-file]')
    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'pic.png',
      remote: false,
      source: 'file:///tmp/pic.png'
    })
  })
})
