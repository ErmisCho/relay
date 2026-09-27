/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Real mode: the Delegator (TASK-42) serves /demo/* on port 8000; Vite proxies it so
// the passcode cookie and the SSE stream stay same-origin in development.
const DELEGATOR = process.env.RELAY_DELEGATOR_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/demo": { target: DELEGATOR, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
