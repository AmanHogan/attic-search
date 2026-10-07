import type { NextConfig } from "next";

/** Where the Python API is listening. */
const API = process.env.ATTIC_API ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Serve the Python API under the same origin, so the browser needs no CORS
  // and page images can be used directly in <img src="/api/...">.
  rewrites: async () => [{ source: "/api/:path*", destination: `${API}/api/:path*` }],
  experimental: {
    // An answer can take a while when the local model is busy (e.g. during `attic index`);
    // the proxy's default 30s cut them off and showed a 500 even though the answer was saved.
    proxyTimeout: 180_000,
  },
};

export default nextConfig;
