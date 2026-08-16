/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["../frontend/*.html", "../frontend/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        smoke: "rgb(var(--c-smoke) / <alpha-value>)",
        mist: "rgb(var(--c-mist) / <alpha-value>)",
        hairline: "var(--hairline)",
        surface: "var(--surface)",
      },
      fontFamily: {
        sans: [
          "-apple-system", "BlinkMacSystemFont", "SF Pro Display", "Inter",
          "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
