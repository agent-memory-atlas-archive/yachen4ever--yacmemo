import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'url'

export default defineConfig({
  plugins: [vue()],
  root: 'src',
  base: '/ui/',
  build: {
    // 注意：new URL 相对本文件（frontend/vite.config.js）解析——上一级即仓库根，
    // 产物必须落包内 yacmemo/webui/dist/，与 app.py 的 STATIC_DIR 保持一致
    outDir: fileURLToPath(new URL('../yacmemo/webui/dist', import.meta.url)),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 大依赖各自成 chunk：vue 运行时 / naive-ui 组件库改动少，可长缓存
        manualChunks: {
          vue: ['vue'],
          'naive-ui': ['naive-ui'],
        },
      },
    },
    // naive-ui 单包较大（gzip ~190KB），属正常，抬高阈值避免噪音
    chunkSizeWarningLimit: 800,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:9721',
    },
  },
})
