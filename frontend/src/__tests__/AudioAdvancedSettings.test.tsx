import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import { AudioAdvancedSettings } from '@/components/features/audio/AudioAdvancedSettings'
import type { ConversionConfig } from '@/lib/api'

const mockGetAudioCapabilities = vi.fn()
const mockGetVocabularyPacks = vi.fn()
const mockSaveVocabularyPack = vi.fn()
const mockDeleteVocabularyPack = vi.fn()

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    getAudioCapabilities: (...args: unknown[]) => mockGetAudioCapabilities(...args),
    getVocabularyPacks: (...args: unknown[]) => mockGetVocabularyPacks(...args),
    saveVocabularyPack: (...args: unknown[]) => mockSaveVocabularyPack(...args),
    deleteVocabularyPack: (...args: unknown[]) => mockDeleteVocabularyPack(...args),
  }
})

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

const mockCapabilities = [
  {
    provider_id: 'local_faster_whisper',
    provider_label: 'Local faster-whisper',
    implementation_state: 'implemented',
    runtime_type: 'local',
    cloud: false,
    requires_api_key: false,
    requires_model_license_acceptance: false,
    privacy_level: 'local',
    supports_word_timestamps: true,
    supports_segment_timestamps: true,
    supports_confidence: true,
    supports_diarization: false,
    supports_speaker_confidence: false,
    supports_custom_vocabulary: true,
    supports_prompt_context: true,
    supports_translation: false,
    supports_batch_compare: true,
    max_file_size_hint_mb: null,
    default_model: 'tiny.en',
  },
  {
    provider_id: 'deepgram',
    provider_label: 'Deepgram Nova',
    implementation_state: 'deferred',
    available: false,
    runtime_type: 'cloud',
    cloud: true,
    requires_api_key: true,
    requires_model_license_acceptance: false,
    privacy_level: 'cloud',
    supports_word_timestamps: true,
    supports_segment_timestamps: true,
    supports_confidence: true,
    supports_diarization: true,
    supports_speaker_confidence: true,
    supports_custom_vocabulary: false,
    supports_prompt_context: false,
    supports_translation: false,
    supports_batch_compare: true,
    max_file_size_hint_mb: 500,
    default_model: 'nova-3',
  },
]

const baseConfig: ConversionConfig = {
  output_formats: ['markdown'],
  converter: 'PdfConverter',
}

describe('AudioAdvancedSettings', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mockGetAudioCapabilities.mockResolvedValue(mockCapabilities)
    mockGetVocabularyPacks.mockResolvedValue([])
    mockSaveVocabularyPack.mockResolvedValue({ id: 'v1', name: 'Test', terms: [], category: '', created_at: '' })
    mockDeleteVocabularyPack.mockResolvedValue(undefined)
  })

  it('renders audio settings with transcription section', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(screen.getByText('Transcription')).toBeInTheDocument()
    expect(screen.getByText(/Local faster-whisper/)).toBeInTheDocument()
  })

  it('renders output style cards', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(screen.getByText('Raw Transcript')).toBeInTheDocument()
    expect(screen.getByText('Evidence-First Notes')).toBeInTheDocument()
    expect(screen.getByText('Meeting Notes')).toBeInTheDocument()
    expect(screen.getByText('Lecture Notes')).toBeInTheDocument()
    expect(screen.getByText('Interview / Q&A')).toBeInTheDocument()
    expect(screen.getByText('Action + Decision Log')).toBeInTheDocument()
  })

  it('shows advanced controls when toggle clicked', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(screen.queryByText('Speakers')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Show Advanced Controls'))

    expect(screen.getByText('Speakers')).toBeInTheDocument()
  })

  it('lets users map anonymous speaker labels to confirmed names', async () => {
    let config: ConversionConfig = {
      ...baseConfig,
      audio_max_speakers: 3,
      audio_speaker_aliases: { speaker_2: 'Charlie' },
    }
    const onChange = vi.fn((key: keyof ConversionConfig, value: unknown) => {
      config = { ...config, [key]: value }
      rerender(<AudioAdvancedSettings config={config} onChange={onChange} />)
    })
    const { rerender } = render(<AudioAdvancedSettings config={config} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Show Advanced Controls'))
    fireEvent.click(screen.getByText('Speakers'))

    expect(screen.getByText('Speaker Names')).toBeInTheDocument()
    expect(screen.getByLabelText('speaker_2 name')).toHaveValue('Charlie')

    fireEvent.change(screen.getByLabelText('speaker_0 name'), { target: { value: ' Alice ' } })
    expect(onChange).toHaveBeenCalledWith('audio_speaker_aliases', {
      speaker_0: 'Alice',
      speaker_2: 'Charlie',
    })
    expect(screen.getByLabelText('speaker_0 name')).toHaveValue('Alice')

    fireEvent.click(screen.getByRole('button', { name: /remove speaker_2 alias/i }))
    expect(onChange).toHaveBeenCalledWith('audio_speaker_aliases', { speaker_0: 'Alice' })
  })

  it('shows deferred provider warning when saved cloud provider is selected', async () => {
    const onChange = vi.fn()
    const cloudConfig: ConversionConfig = {
      ...baseConfig,
      audio_provider: 'deepgram',
      audio_allow_cloud_stt: true,
    }
    render(<AudioAdvancedSettings config={cloudConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(await screen.findByText(/deferred in this build/i)).toBeInTheDocument()
    expect(screen.getByText(/Cloud provider/)).toBeInTheDocument()
  })

  it('does not offer deferred providers in provider picker', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(screen.getByText(/Local faster-whisper/)).toBeInTheDocument()
    expect(screen.queryByText(/Deepgram Nova/)).not.toBeInTheDocument()
  })

  it('does not auto-enable cloud STT when selecting an implemented cloud provider', async () => {
    const onChange = vi.fn()
    mockGetAudioCapabilities.mockResolvedValue([
      mockCapabilities[0],
      {
        ...mockCapabilities[1],
        provider_id: 'openai',
        provider_label: 'OpenAI Speech-to-Text',
        implementation_state: 'implemented',
        available: true,
        supports_diarization: false,
        default_model: 'gpt-4o-mini-transcribe',
      },
    ])

    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByRole('button', { name: /local faster-whisper/i }))
    fireEvent.click(screen.getByText(/OpenAI Speech-to-Text \(cloud\)/i))

    expect(onChange).toHaveBeenCalledWith('audio_provider', 'openai')
    expect(onChange).not.toHaveBeenCalledWith('audio_allow_cloud_stt', true)
  })

  it('warns when a cloud provider is selected without cloud STT consent', async () => {
    const onChange = vi.fn()
    mockGetAudioCapabilities.mockResolvedValue([
      mockCapabilities[0],
      {
        ...mockCapabilities[1],
        provider_id: 'openai',
        provider_label: 'OpenAI Speech-to-Text',
        implementation_state: 'implemented',
        available: true,
        supports_diarization: false,
        default_model: 'gpt-4o-mini-transcribe',
      },
    ])

    render(
      <AudioAdvancedSettings
        config={{ ...baseConfig, audio_provider: 'openai', audio_allow_cloud_stt: false }}
        onChange={onChange}
      />,
    )

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    expect(screen.getByText(/Cloud STT is selected but not allowed yet/i)).toBeInTheDocument()
  })

  it('calls onChange when output style selected', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Meeting Notes'))

    expect(onChange).toHaveBeenCalledWith('audio_output_mode', 'meeting_notes')
  })

  it('shows enhancement controls when text enhancement enabled', async () => {
    const onChange = vi.fn()
    const enhancedConfig: ConversionConfig = {
      ...baseConfig,
      audio_text_enhancement_enabled: true,
    }
    render(<AudioAdvancedSettings config={enhancedConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    // Open advanced controls to reveal Enhancement section
    fireEvent.click(screen.getByText('Show Advanced Controls'))

    // Click Enhancement section header to expand it
    fireEvent.click(screen.getByText('Enhancement & Correction'))

    expect(screen.getByText('Strength')).toBeInTheDocument()
  })

  it('sets minimal strength when enabling transcript wording cleanup', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Show Advanced Controls'))
    fireEvent.click(screen.getByText('Enhancement & Correction'))
    fireEvent.click(screen.getByRole('button', { name: /clean transcript wording/i }))

    expect(onChange).toHaveBeenCalledWith('audio_text_enhancement_enabled', true)
    expect(onChange).toHaveBeenCalledWith('audio_text_enhancement_strength', 1)
  })

  it('describes enhancement as local source-bound cleanup, not unrestricted rewriting', async () => {
    const onChange = vi.fn()
    const enhancedConfig: ConversionConfig = {
      ...baseConfig,
      audio_text_enhancement_enabled: true,
      audio_text_enhancement_strength: 5,
    }
    render(<AudioAdvancedSettings config={enhancedConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Show Advanced Controls'))
    fireEvent.click(screen.getByText('Enhancement & Correction'))

    expect(screen.getByText(/Most aggressive local cleanup; no new claims/i)).toBeInTheDocument()
    expect(screen.queryByText(/Full rewrite/i)).not.toBeInTheDocument()
  })

  it('disables provider comparison because the benchmark runner is not shipped', async () => {
    const onChange = vi.fn()
    mockGetAudioCapabilities.mockResolvedValue([
      mockCapabilities[0],
      { ...mockCapabilities[1], implementation_state: 'deferred', available: false },
    ])

    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Show Advanced Controls'))
    fireEvent.click(screen.getByText('Benchmark / Compare'))

    const compare = screen.getByRole('button', { name: /compare providers/i })
    expect(compare).toBeDisabled()
    expect(screen.getByText(/provider comparison is not shipped/i)).toBeInTheDocument()
  })

  it('disables cloud enhancement because no cloud enhancement adapter ships', async () => {
    const onChange = vi.fn()
    render(<AudioAdvancedSettings config={baseConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    fireEvent.click(screen.getByText('Show Advanced Controls'))
    fireEvent.click(screen.getByText('Privacy & Providers'))

    const cloudEnhancement = screen.getByRole('button', { name: /allow cloud enhancement/i })
    expect(cloudEnhancement).toBeDisabled()
    expect(screen.getByText(/cloud transcript enhancement is not shipped/i)).toBeInTheDocument()
  })
})
