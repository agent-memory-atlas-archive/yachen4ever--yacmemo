import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { execSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'url'

// 构建期注入版本号与 commit（侧边栏底部展示）——以本文件所在仓库为准
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf-8'))
let commit = 'unknown'
try {
  commit = execSync('git rev-parse --short HEAD', { cwd: fileURLToPath(new URL('..', import.meta.url)) })
    .toString().trim()
} catch { /* 非 git 环境（如 CI 深拷贝）降级为 unknown */ }

export default defineConfig({
  plugins: [vue()],
  root: 'src',
  base: '/ui/',
  define: {
    __BUILD__: JSON.stringify({
      version: pkg.version,
      commit,
      builtAt: new Date().toISOString().slice(0, 10),
    }),
  },
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
