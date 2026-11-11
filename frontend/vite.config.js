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
  },
});
