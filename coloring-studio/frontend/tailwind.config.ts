import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#1d1b29", soft: "#4b4860" },
        brand: { 50: "#eef4ff", 100: "#dbe7ff", 500: "#3b6cf6", 600: "#2953d9", 700: "#2142ad" },
        paper: "#fbfaf7",
      },
      fontFamily: { sans: ["ui-sans-serif", "system-ui", "Segoe UI", "Roboto", "sans-serif"] },
    },
  },
  plugins: [],
} satisfies Config;
