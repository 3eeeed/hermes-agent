import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { MarkdownImage, MarkdownTextContent, MessageTextContent } from './markdown-text'

const REMOTE_IMAGE_PATH = '/home/user/project/images/remote-preview.png'
const REMOTE_IMAGE_DATA_URL = 'data:image/png;base64,cmVtb3RlLWltYWdl'

function contextFileAttr(element: Element): string {
  return element.closest('[data-hermes-context-file]')?.getAttribute('data-hermes-context-file') || '{}'
}

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
    expect(JSON.parse(contextFileAttr(image))).toMatchObject({
      kind: 'gateway',
      profile: 'remote-work',
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

    expect(JSON.parse(contextFileAttr(image))).toMatchObject({
      kind: 'gateway',
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
      kind: 'local',
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
      kind: 'local',
      source: 'file:///tmp/note.mp3'
    })
  })

  it('renders a context descriptor on a regular file attachment', async () => {
    render(<MarkdownTextContent isRunning={false} text="[report](#media:%2Ftmp%2Ffactory%20review.pdf)" />)

    // A non-media file routes to the preview card (#84951 et al.).
    const label = await screen.findByText('factory review.pdf')
    const contextTarget = label.closest('[data-hermes-context-file]')

    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'factory review.pdf',
      kind: 'local',
      source: '/tmp/factory review.pdf'
    })
  })

  it('renders a context descriptor on an explicit file preview link', async () => {
    render(<MarkdownTextContent isRunning={false} text="[spec](#preview/%2Ftmp%2Fproduct%20spec.pdf)" />)

    const label = await screen.findByText('product spec.pdf')
    const contextTarget = label.closest('[data-hermes-context-file]')

    expect(JSON.parse(contextTarget?.getAttribute('data-hermes-context-file') || '{}')).toMatchObject({
      name: 'product spec.pdf',
      kind: 'local',
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
      kind: 'local',
      source: 'file:///tmp/pic.png'
    })
  })
})

// Webapp audio/video play from stream-ticket URLs that expire. An element
// that already loaded needs a fresh ticket for its next range request (a seek,
// or play after a long pause) instead of degrading to "Open file" for good.
describe('MarkdownImage stream-ticket renewal', () => {
  const mint = vi.fn()
  let originalDesktop: typeof window.hermesDesktop

  const audioSrc = (container: HTMLElement) => container.querySelector('audio')?.getAttribute('src')

  beforeEach(() => {
    let minted = 0

    mint.mockReset().mockImplementation(async () => `https://hermes.test/api/files/stream?ticket=t${++minted}`)
    originalDesktop = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { getGatewayFileStreamUrl: mint } })
  })

  afterEach(() => {
    cleanup()
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
  })

  it('re-mints the source after a loaded element errors and resumes where it stopped', async () => {
    const { container } = render(<MarkdownImage alt="note" src="/srv/media/note.mp3" />)

    await waitFor(() => expect(audioSrc(container)).toBe('https://hermes.test/api/files/stream?ticket=t1'))
    const audio = container.querySelector('audio')!
    fireEvent.loadedMetadata(audio)
    audio.currentTime = 42
    fireEvent.error(audio)

    await waitFor(() => expect(audioSrc(container)).toBe('https://hermes.test/api/files/stream?ticket=t2'))
    audio.currentTime = 0
    fireEvent.loadedMetadata(audio)
    expect(audio.currentTime).toBe(42)
    expect(screen.queryByText('Open audio file')).toBeNull()
  })

  it('offers the file instead when a source fails before it ever loads', async () => {
    const { container } = render(<MarkdownImage alt="note" src="/srv/media/note.mp3" />)

    await waitFor(() => expect(audioSrc(container)).toBe('https://hermes.test/api/files/stream?ticket=t1'))
    fireEvent.error(container.querySelector('audio')!)

    expect(await screen.findByText('Open audio file')).toBeTruthy()
    expect(mint).toHaveBeenCalledTimes(1)
  })
})

describe('MessageTextContent MEDIA directives', () => {
  afterEach(cleanup)

  it('renders a raw audio MEDIA directive through the canonical player instead of exposing the directive', async () => {
    const { container } = render(<MessageTextContent text="MEDIA:/tmp/group-voice.mp3" />)

    await waitFor(() => expect(container.querySelector('audio[controls]')).not.toBeNull())
    expect(container.textContent).not.toContain('MEDIA:')
  })
})
