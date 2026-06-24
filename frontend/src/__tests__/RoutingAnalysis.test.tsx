import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RoutingAnalysis } from '@/components/features/conversion/RoutingAnalysis'
import type { ConverterPlanResponse } from '@/lib/api'
import '@testing-library/jest-dom'

describe('RoutingAnalysis Component', () => {
  const mockPlan: ConverterPlanResponse = {
    engine: 'liteparse_pdf',
    label: 'LiteParse Fast PDF',
    confidence: 0.85,
    reasons: ['Simple layout detected', 'Sufficient text quality'],
    needs_marker_models: false,
    needs_gpu: false,
    execution_backend: 'cpu_thread',
    needs_cloud: false,
    optional_dependencies: [],
    fallback_chain: ['liteparse_pdf', 'marker_pdf'],
    warnings: ['Page count warning'],
    preliminary: false,
    probe_result: {
      text_layer_score: 0.95,
      text_quality_score: 0.90,
      scan_likelihood: 0.05,
      sandwich_likelihood: 0.10,
      visual_complexity_score: 0.15,
      layout_complexity_score: 0.20,
    },
  }

  it('renders engine label, confidence, backend, reasons and warnings', () => {
    render(<RoutingAnalysis plan={mockPlan} title="Test Title" />)

    expect(screen.getByText('Test Title')).toBeInTheDocument()
    expect(screen.getByText('LiteParse Fast PDF')).toBeInTheDocument()
    expect(screen.getByText('Confidence: 85%')).toBeInTheDocument()
    expect(screen.getByText('CPU')).toBeInTheDocument()
    expect(screen.getByText('Simple layout detected')).toBeInTheDocument()
    expect(screen.getByText('Page count warning')).toBeInTheDocument()
  })

  it('renders probe scores grid when not preliminary', () => {
    render(<RoutingAnalysis plan={mockPlan} />)

    expect(screen.getByText('Text Layer Score')).toBeInTheDocument()
    expect(screen.getByText('95%')).toBeInTheDocument()
    expect(screen.getByText('Layout Complexity')).toBeInTheDocument()
    expect(screen.getByText('20%')).toBeInTheDocument()
  })

  it('suppresses probe grid and shows preliminary notice when preliminary is true', () => {
    const prelimPlan = { ...mockPlan, preliminary: true }
    render(<RoutingAnalysis plan={prelimPlan} />)

    expect(screen.getByText('Preliminary Route Decision')).toBeInTheDocument()
    expect(screen.queryByText('Text Layer Score')).not.toBeInTheDocument()
  })

  it('renders completed-job metadata shape from status/history', () => {
    render(
      <RoutingAnalysis
        plan={{
          engine: mockPlan,
          probe_result: {
            text_layer_score: 0.92,
            text_quality_score: 0.88,
            scan_likelihood: 0.04,
            sandwich_likelihood: 0.08,
            visual_complexity_score: 0.12,
            layout_complexity_score: 0.18,
          },
        }}
      />
    )

    expect(screen.getByText('LiteParse Fast PDF')).toBeInTheDocument()
    expect(screen.getByText('92%')).toBeInTheDocument()
  })

  it('renders mixed PDF segment metadata from completed jobs', () => {
    render(
      <RoutingAnalysis
        plan={{
          engine: {
            ...mockPlan,
            engine: 'mixed_pdf',
            label: 'Mixed PDF routing',
            execution_backend: 'marker_worker',
          },
          mixed_engine_segments: [
            {
              page_range: '1',
              requested_engine: 'liteparse_pdf',
              actual_engine: 'liteparse_pdf',
            },
            {
              page_range: '2-3',
              requested_engine: 'marker_pdf',
              actual_engine: 'marker_pdf',
              fallback_reason: null,
            },
          ],
        }}
      />
    )

    expect(screen.getByTestId('mixed-routing-segments')).toBeInTheDocument()
    expect(screen.getByText('Page Segments')).toBeInTheDocument()
    expect(screen.getByText('Pages 1')).toBeInTheDocument()
    expect(screen.getByText('Pages 2-3')).toBeInTheDocument()
    expect(screen.getByText('LiteParse')).toBeInTheDocument()
    expect(screen.getByText('Marker')).toBeInTheDocument()
  })

  it('highlights unsafe scores with AlertTriangle', () => {
    const unsafePlan: ConverterPlanResponse = {
      ...mockPlan,
      probe_result: {
        text_layer_score: 0.50, // unsafe (< 0.70)
        text_quality_score: 0.90, // safe
        scan_likelihood: 0.05, // safe
        sandwich_likelihood: 0.10, // safe
        visual_complexity_score: 0.60, // unsafe (> 0.35)
        layout_complexity_score: 0.20, // safe
      }
    }
    render(<RoutingAnalysis plan={unsafePlan} />)

    // Unsafe Text Layer Score: 50%
    expect(screen.getByText('50%')).toBeInTheDocument()
    // Unsafe Visual Complexity: 60%
    expect(screen.getByText('60%')).toBeInTheDocument()
    
    // There should be AlertTriangle warning icons rendered
    const alertIcons = screen.getAllByTitle('Outside safe LiteParse threshold')
    expect(alertIcons.length).toBe(2)
  })
})
