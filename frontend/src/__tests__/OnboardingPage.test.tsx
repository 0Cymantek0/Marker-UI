import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import { OnboardingPage } from '@/pages/OnboardingPage'
import {
  getModelsStatus,
  getHybridOcrStatus,
  setupHybridOcrModels,
} from '@/lib/api'

vi.mock('@/lib/api', () => ({
  getModelsStatus: vi.fn(),
  getHybridOcrStatus: vi.fn(),
  setupHybridOcrModels: vi.fn(),
  cancelModelsDownload: vi.fn(),
  retryModelsDownload: vi.fn(),
}))

vi.mock('@/components/ui/CanvasConfetti', () => ({
  CanvasConfetti: () => null,
}))

const coreReadyStatus = {
  initialized: true,
  loading: false,
  cancel_requested: false,
  error: null,
  models: {},
  overall: {
    status: 'completed',
    progress: 100,
    downloaded_bytes: 10,
    total_bytes: 10,
    speed: 0,
    eta: 0,
  },
}

const hybridMissingStatus = {
  schema_version: 'marker.hybrid_ocr_status.v1',
  model_root: 'cache',
  engines_available: ['surya'],
  warnings: [],
  engines: {
    glm_ocr: { model_id: 'zai-org/GLM-OCR', model_dir: 'cache/glm', model_present: false },
    paddleocr_vl: { model_id: 'PaddlePaddle/PaddleOCR-VL', model_dir: 'cache/paddle', model_present: false },
  },
}

const hybridReadyStatus = {
  ...hybridMissingStatus,
  engines_available: ['surya', 'paddleocr_vl'],
  engines: {
    glm_ocr: { ...hybridMissingStatus.engines.glm_ocr, model_present: true },
    paddleocr_vl: { ...hybridMissingStatus.engines.paddleocr_vl, model_present: true },
  },
}

describe('OnboardingPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('downloads missing Hybrid OCR snapshots before completing setup', async () => {
    vi.mocked(getModelsStatus).mockResolvedValue(coreReadyStatus as any)
    vi.mocked(getHybridOcrStatus).mockResolvedValue(hybridMissingStatus as any)
    vi.mocked(setupHybridOcrModels).mockResolvedValue({ status: hybridReadyStatus } as any)
    const onComplete = vi.fn()

    render(<OnboardingPage onComplete={onComplete} />)

    await waitFor(() => expect(setupHybridOcrModels).toHaveBeenCalledWith('all'))
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1))
  })
})
