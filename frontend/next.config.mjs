/** @type {import('next').NextConfig} */
const backend = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000'

const nextConfig = {
  images: {
    unoptimized: true,
  },
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${backend}/api/:path*` },
      { source: '/uploads/:path*', destination: `${backend}/uploads/:path*` },
    ]
  },
}

export default nextConfig
