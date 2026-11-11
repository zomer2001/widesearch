import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep the dev indicator off the sidebar footer (health dot / settings live bottom-left).
  devIndicators: { position: "bottom-right" },
  // npm and web/start.sh run Next from this package directory. Pin Turbopack
  // here so an unrelated parent lockfile cannot become the project root.
  turbopack: { root: process.cwd() },
  async rewrites() {
    const apiOrigin = (process.env.WORKBENCH_API_ORIGIN || "http://127.0.0.1:8000").replace(/\/$/, "");
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
