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
    getAudioCapabilities: (...args: any[]) => mockGetAudioCapabilities(...args),
    getVocabularyPacks: (...args: any[]) => mockGetVocabularyPacks(...args),
    saveVocabularyPack: (...args: any[]) => mockSaveVocabularyPack(...args),
    deleteVocabularyPack: (...args: any[]) => mockDeleteVocabularyPack(...args),
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

  it('shows cloud warning when cloud provider selected', async () => {
    const onChange = vi.fn()
    const cloudConfig: ConversionConfig = {
      ...baseConfig,
      audio_provider: 'deepgram' as any,
      audio_allow_cloud_stt: true,
    }
    render(<AudioAdvancedSettings config={cloudConfig} onChange={onChange} />)

    await waitFor(() => {
      expect(mockGetAudioCapabilities).toHaveBeenCalled()
    })

    await waitFor(() => {
      expect(screen.getByText(/Cloud provider/)).toBeInTheDocument()
    })
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
})
