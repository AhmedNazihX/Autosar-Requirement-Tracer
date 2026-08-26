"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * Light/dark for the whole app.
 *
 * `attribute="class"` because `app/globals.css` defines the dark palette under
 * `.dark`, and `@custom-variant dark (&:is(.dark *))` keys every Tailwind `dark:`
 * utility off that same class. Both themes are designed on the canvas, so
 * neither is a fallback.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      {children}
    </NextThemesProvider>
  );
}
