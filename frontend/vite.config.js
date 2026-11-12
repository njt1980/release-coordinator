import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind to all interfaces so devcontainer port forwarding (IPv4) can reach
    // the server; the default `localhost` resolves to ::1 only in this image.
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // The workspace lives on a WSL2 9p/drvfs mount where inotify events are
    // not delivered, so poll for file changes to keep HMR working.
    watch: {
      usePolling: true,
    },
    // Forward API calls to the FastAPI backend (uvicorn default port).
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
