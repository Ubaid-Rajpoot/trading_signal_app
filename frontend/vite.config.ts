import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.BACKEND_URL ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // The Arena preview proxies through a generated hostname.
    allowedHosts: true,
    hmr: { clientPort: 443 },
    proxy: {
      '/api': { target: backend, changeOrigin: true },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
    allowedHosts: true,
  },
})
