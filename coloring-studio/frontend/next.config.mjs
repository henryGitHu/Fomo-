/** @type {import('next').NextConfig} */
const backend = process.env.STUDIO_API_URL || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Same-origin API: the browser talks to Next, which proxies /api/* to FastAPI.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
