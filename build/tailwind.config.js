/** Production build config — mirrors the theme the pages used with the CDN. */
module.exports = {
  darkMode: "class",
  content: ["../frontend/**/*.html", "../frontend/**/*.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Display"', 'Inter',
               '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif'],
      },
      colors: {
        ink: 'rgb(var(--c-ink) / <alpha-value>)',
        smoke: 'rgb(var(--c-smoke) / <alpha-value>)',
        mist: 'rgb(var(--c-mist) / <alpha-value>)',
        hairline: 'var(--hairline)',
        surface: 'var(--surface)',
        card: 'var(--surface-solid)',
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,.04), 0 8px 24px -8px rgba(0,0,0,.10)',
        pill: '0 2px 10px -2px rgba(0,0,0,.12)',
      },
      borderRadius: {
        '2.5xl': '1.375rem',
      },
    },
  },
  plugins: [],
};
