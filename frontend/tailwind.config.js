/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Primary accent: buttons, links, focus rings, the changed service.
        brand: {
          50: "#eef2ff",
          100: "#e0e7ff",
          200: "#c7d2fe",
          300: "#a5b4fc",
          400: "#818cf8",
          500: "#6366f1",
          600: "#4f46e5",
          700: "#4338ca",
          800: "#3730a3",
          900: "#312e81",
        },
        // Dark surfaces (hero, navigation) and their text.
        ink: {
          950: "#0b1020",
          900: "#111832",
          800: "#1b2445",
          700: "#2a3560",
          300: "#a9b4d6",
          200: "#cdd5ee",
        },
        // Secondary accent: observed traffic, verified evidence.
        signal: {
          300: "#5eead4",
          400: "#2dd4bf",
          500: "#14b8a6",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
      },
      keyframes: {
        // Traffic flowing along a dashed edge.
        flow: { to: { strokeDashoffset: "-24" } },
        "fade-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        ring: {
          "0%": { opacity: "0.7", transform: "scale(1)" },
          "100%": { opacity: "0", transform: "scale(1.9)" },
        },
      },
      animation: {
        flow: "flow 1.2s linear infinite",
        "fade-up": "fade-up 0.6s ease-out both",
        ring: "ring 1.8s ease-out infinite",
      },
    },
  },
  plugins: [],
};
