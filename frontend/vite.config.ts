import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В dev-режиме /api проксируется на backend; в Docker — через nginx
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_URL ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
