"use client";

import { MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";

import { Button } from "@/components/ui/button";

/**
 * The header's theme switch from the canvas.
 *
 * Which glyph shows is decided by CSS (`dark:`), not by React state. That is
 * deliberate: the resolved theme is unknown during SSR, so choosing the icon in
 * JavaScript means either a hydration mismatch or a mounted-flag flicker. The
 * `.dark` class is on `<html>` before React hydrates, so CSS already knows.
 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
      aria-label="Toggle light and dark theme"
    >
      <MoonIcon className="dark:hidden" />
      <SunIcon className="hidden dark:block" />
    </Button>
  );
}
