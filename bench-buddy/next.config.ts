import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    // Intake photos are shrunk in the browser first, so this is plenty.
    serverActions: { bodySizeLimit: "4mb" },
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
