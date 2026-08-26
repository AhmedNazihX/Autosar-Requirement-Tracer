"use client";

/**
 * Trailing-edge throttle for a value that changes far faster than it needs to
 * be rendered.
 *
 * It exists for exactly one reason: assistant text arrives as `token` deltas,
 * and the markdown renderer parses the whole accumulated message each time it
 * runs. Feeding it every delta would re-parse the answer character by character.
 * Feeding it a value that updates at most every `intervalMs` keeps markdown live
 * while taking the parse out of the per-token path.
 *
 * The final value always lands: the trailing timer fires after the last change,
 * so a stream that stops mid-word does not leave the last few tokens unrendered.
 */

import { useEffect, useRef, useState } from "react";

export function useThrottled<T>(value: T, intervalMs = 100): T {
  const [throttled, setThrottled] = useState(value);
  const lastEmit = useRef(0);

  useEffect(() => {
    const wait = Math.max(0, intervalMs - (Date.now() - lastEmit.current));
    const timer = setTimeout(() => {
      lastEmit.current = Date.now();
      setThrottled(value);
    }, wait);
    return () => clearTimeout(timer);
  }, [value, intervalMs]);

  return throttled;
}
