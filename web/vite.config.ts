import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the page is served by Vite and talks to `dowel open` on its default port.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:4517" } },
});
