import type { NextConfig } from "next";

/** Where the Python API is listening. */
const API = process.env.ATTIC_API ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Serve the Python API under the same origin, so the browser needs no CORS
  // and page images can be used directly in <img src="/api/...">.
  rewrites: async () => [{ source: "/api/:path*", destination: `${API}/api/:path*` }],
};

export default nextConfig;
