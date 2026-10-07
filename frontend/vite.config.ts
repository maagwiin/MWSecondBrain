import { defineConfig } from 'vite';

export default defineConfig({
  base: '/',
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/obsidian': 'http://127.0.0.1:8765',
    },
  },
});
