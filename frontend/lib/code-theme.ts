import type { ThemeRegistrationRaw } from "shiki";

/**
 * The Shiki theme for the Code tab.
 *
 * Deliberately NOT a Shiki preset. Every colour is a `var(--code-*)` reference,
 * so the file re-themes with the app instead of carrying a baked-in palette —
 * one theme object serves light and dark, and the eight `--code-*` tokens in
 * `globals.css` are the single source of truth (canvas artboard 3).
 *
 * Comments stay the lowest-contrast token on purpose: the `@req` / `!req`
 * annotation chips drawn on top of them have to read first.
 */
export const reqtraceCodeTheme: ThemeRegistrationRaw = {
  name: "reqtrace",
  type: "light",
  colors: {
    "editor.foreground": "var(--foreground)",
    "editor.background": "var(--code-bg)",
  },
  settings: [
    {
      settings: {
        foreground: "var(--foreground)",
        background: "var(--code-bg)",
      },
    },
    {
      scope: ["comment", "punctuation.definition.comment"],
      settings: { foreground: "var(--code-comment)" },
    },
    {
      scope: [
        "keyword",
        "keyword.control",
        "keyword.operator",
        "storage.modifier",
      ],
      settings: { foreground: "var(--code-keyword)" },
    },
    {
      scope: [
        "storage.type",
        "entity.name.type",
        "support.type",
        "meta.tag.type",
      ],
      settings: { foreground: "var(--code-type)" },
    },
    {
      scope: [
        "entity.name.function",
        "support.function",
        "meta.function-call",
        "meta.function-call entity.name.function",
      ],
      settings: { foreground: "var(--code-fn)" },
    },
    {
      scope: ["string", "string.quoted", "constant.character"],
      settings: { foreground: "var(--code-string)" },
    },
    {
      scope: ["constant.numeric", "constant.language"],
      settings: { foreground: "var(--code-number)" },
    },
    {
      scope: [
        "meta.preprocessor",
        "keyword.control.directive",
        "entity.name.function.preprocessor",
        "punctuation.definition.directive",
      ],
      settings: { foreground: "var(--code-macro)" },
    },
  ],
};
