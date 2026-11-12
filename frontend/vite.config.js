import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Ports are overridable so the app can run alongside other services;
// defaults match the devcontainer's forwarded port and uvicorn's default.
const frontendPort = Number(process.env.RC_FRONTEND_PORT) || 5173;
const backendUrl =
  process.env.RC_BACKEND_URL ||
  `http://127.0.0.1:${Number(process.env.RC_BACKEND_PORT) || 8000}`;

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind to all interfaces so devcontainer port forwarding (IPv4) can reach
    // the server; the default `localhost` resolves to ::1 only in this image.
    host: "0.0.0.0",
    port: frontendPort,
    strictPort: true,
    // The workspace lives on a WSL2 9p/drvfs mount where inotify events are
    // not delivered, so poll for file changes to keep HMR working.
    watch: {
      usePolling: true,
    },
    // Forward API calls to the FastAPI backend.
    proxy: {
      "/api": backendUrl,
    },
  },
});
