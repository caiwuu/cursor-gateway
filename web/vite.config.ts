import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_API_TARGET || "http://127.0.0.1:8788";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": apiTarget,
        "/n/": apiTarget,
        "/v1": apiTarget,
        "/bot": apiTarget,
        "/account": apiTarget,
        "/sand-direct": apiTarget,
        "/healthz": apiTarget,
      },
    },
  };
});
