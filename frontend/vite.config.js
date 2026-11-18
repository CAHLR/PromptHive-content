import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    base: './',
    build: {
      outDir: 'build',
    },
    plugins: [react()],
    server: {
      proxy: {
        '/api/course-materials': {
          target: env.VITE_COURSE_MATERIALS_PROXY_TARGET || 'http://127.0.0.1:8082',
          changeOrigin: true,
        },
        '/api': {
          target: env.VITE_REACT_APP_API_PROXY_URL,
          changeOrigin: true,
          secure: false,
          rewrite: (path) => path.replace(/^\/api/, '')
        },
        cors: false
      },
    },
  };
});
