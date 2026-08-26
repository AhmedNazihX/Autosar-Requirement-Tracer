"use client";

import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

import { ChatCodeBlock } from "./chat-code-block";

/**
 * Assistant message body.
 *
 * Three deliberate constraints:
 *
 *  - **No raw HTML.** `rehype-raw` is not installed and must not be. This text
 *    is an LLM summarising untrusted specifications and untrusted C, and spec §8
 *    treats all of that as data, not instructions. react-markdown escapes HTML
 *    by default; that default is the security control.
 *  - **Fenced code goes through Shiki**, not react-markdown's `<pre><code>`, so
 *    a snippet in an answer is coloured by the same `--code-*` tokens as the
 *    same snippet in the Code tab.
 *  - **Memoised on the text.** The caller throttles the streamed text before it
 *    gets here (see `useThrottled`), and this component re-parses only when that
 *    throttled string changes — never once per `token` delta.
 *
 * Nothing in the body is a link to a source. Citations are separate structured
 * chips built from `citation` events, so there is no path by which a citation
 * could be parsed out of prose.
 */
export const MarkdownBody = memo(function MarkdownBody({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "text-sm leading-[21px] [&>*+*]:mt-2.5 [&_strong]:font-semibold",
        className,
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
});

const COMPONENTS: Components = {
  // `pre` becomes a passthrough: the `code` override below renders the whole
  // block, so wrapping it in another `<pre>` would nest a block inside a block.
  pre({ children }) {
    return <>{children}</>;
  },
  code({ className, children }) {
    const text = String(children ?? "");
    const language = /language-([\w+-]+)/.exec(className ?? "")?.[1];
    // A fence with no info string still contains a newline; an inline span never
    // does. That is the only reliable block/inline signal react-markdown gives.
    const isBlock = Boolean(language) || text.includes("\n");
    if (isBlock) {
      return (
        <ChatCodeBlock code={text.replace(/\n$/, "")} info={language} />
      );
    }
    return (
      <code className="rounded-[4px] bg-muted px-1 py-px font-mono text-[13px]">
        {children}
      </code>
    );
  },
  p({ children }) {
    return <p className="text-sm leading-[21px]">{children}</p>;
  },
  ul({ children }) {
    return <ul className="ml-4 list-disc space-y-1 text-sm leading-[21px]">{children}</ul>;
  },
  ol({ children }) {
    return (
      <ol className="ml-4 list-decimal space-y-1 text-sm leading-[21px]">{children}</ol>
    );
  },
  li({ children }) {
    return <li className="pl-0.5">{children}</li>;
  },
  h1({ children }) {
    return <h3 className="text-[15px] font-semibold">{children}</h3>;
  },
  h2({ children }) {
    return <h3 className="text-[14.5px] font-semibold">{children}</h3>;
  },
  h3({ children }) {
    return <h4 className="text-sm font-semibold">{children}</h4>;
  },
  blockquote({ children }) {
    return (
      <blockquote className="rounded-r-lg border-l-2 border-source-border bg-muted px-2.5 py-2 text-[12.5px] leading-[17.5px]">
        {children}
      </blockquote>
    );
  },
  a({ children, href }) {
    // Links inside a model-written answer point outside the app. They open in a
    // new tab with no referrer, and they are never how a source is opened.
    // `href` is already sanitised: overriding `a` does NOT bypass
    // react-markdown's default `urlTransform`, which strips `javascript:` and
    // other non-safe protocols before the prop reaches here. Do not pass
    // `urlTransform={(url) => url}` — that default is the second security
    // control in this file, and it is invisible.
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer nofollow"
        className="text-source underline underline-offset-2"
      >
        {children}
      </a>
    );
  },
  table({ children }) {
    return (
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]">{children}</table>
      </div>
    );
  },
  th({ children }) {
    return (
      <th className="border-b px-2 py-1 text-left font-medium">{children}</th>
    );
  },
  td({ children }) {
    return <td className="border-b px-2 py-1 align-top">{children}</td>;
  },
  hr() {
    return <hr className="border-border" />;
  },
};
