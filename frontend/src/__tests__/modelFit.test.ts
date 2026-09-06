import { describe, it, expect } from 'vitest'
import { unfitModels, bestFittingModel } from '@/lib/modelFit'

const options = [
  { label: 'Small', value: 'small' },
  { label: 'Medium', value: 'medium' },
  { label: 'Turbo', value: 'large-v3-turbo' },
  { label: 'Large', value: 'large-v3' },
]

describe('unfitModels', () => {
  it('returns models flagged false', () => {
    const result = unfitModels({
      small: true,
      medium: true,
      'large-v3-turbo': true,
      'large-v3': false,
    })
    expect(result.has('large-v3')).toBe(true)
    expect(result.has('large-v3-turbo')).toBe(false)
  })

  it('returns empty set when fit data is missing', () => {
    expect(unfitModels(null).size).toBe(0)
    expect(unfitModels(undefined).size).toBe(0)
  })

  it('returns empty set when everything fits', () => {
    const result = unfitModels({ small: true, medium: true })
    expect(result.size).toBe(0)
  })
})

describe('bestFittingModel', () => {
  it('keeps current model if it fits', () => {
    const fit = { small: true, medium: true, 'large-v3-turbo': true, 'large-v3': true }
    expect(bestFittingModel('large-v3-turbo', options, fit)).toBe('large-v3-turbo')
  })

  it('downgrades to the best fitting model when current does not fit', () => {
    const fit = { small: true, medium: true, 'large-v3-turbo': false, 'large-v3': false }
    expect(bestFittingModel('large-v3-turbo', options, fit)).toBe('medium')
  })

  it('downgrades to the largest fitting model, not just any fitting model', () => {
    const fit = { small: true, medium: true, 'large-v3-turbo': true, 'large-v3': false }
    expect(bestFittingModel('large-v3', options, fit)).toBe('large-v3-turbo')
  })

  it('returns current unchanged when there is no fit data', () => {
    expect(bestFittingModel('large-v3', options, null)).toBe('large-v3')
  })

  it('falls back to current if nothing fits', () => {
    const fit = { small: false, medium: false, 'large-v3-turbo': false, 'large-v3': false }
    expect(bestFittingModel('large-v3', options, fit)).toBe('large-v3')
  })
})
