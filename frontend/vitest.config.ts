import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  define: {
    // React 19's index.js picks dev vs prod off process.env.NODE_ENV. Vitest
    // does not set NODE_ENV=development by default, so React loads its
    // production build which throws "act(...) is not supported in production
    // builds of React" inside @testing-library/react.
    'process.env.NODE_ENV': JSON.stringify('development'),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/__tests__/setup.ts',
    globals: true,
    css: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
