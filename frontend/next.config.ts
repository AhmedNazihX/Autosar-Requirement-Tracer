import type { NextConfig } from "next";

// Where the proxy forwards `/api/py/*`. An env override rather than a
// hardcode because this is the single value that must change to run the
// frontend against anything but a local backend — read at build/dev start
// (rewrites are config, not runtime), so changing it means restarting.
const BACKEND_ORIGIN =
  process.env.REQTRACE_BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Build-time auto-memoization (React Compiler, via
  // babel-plugin-react-compiler). Components and hooks that follow the Rules
  // of React get their skip-unchanged-work logic generated at compile time,
  // which is what lets the code below stay free of manual useMemo/useCallback;
  // a component the compiler cannot prove safe is left untouched, not broken.
  reactCompiler: true,
  async rewrites() {
    return [
      {
        source: "/api/py/:path*",
        destination: `${BACKEND_ORIGIN}/:path*`,
      },
    ];
  },
};

export default nextConfig;
