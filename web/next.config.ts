import type { NextConfig } from "next";

// The signed-in pages are client components that load their data from this app's own API
// routes, so there is no server-streamed content for Cache Components or partial
// prefetching to optimise; leaving them off avoids their instant-navigation validation.
const nextConfig: NextConfig = {
  // Don't let `next dev` write its AGENTS.md instructions file into the repo.
  agentRules: false,
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
