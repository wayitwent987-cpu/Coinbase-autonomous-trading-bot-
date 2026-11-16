import type { MetadataRoute } from "next";

// Lets techs "Add to Home Screen" so Bench Buddy opens like its own app.
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Bench Buddy",
    short_name: "Bench Buddy",
    description: "Repair shop intake, triage, guides and quotes.",
    start_url: "/board",
    display: "standalone",
    background_color: "#f1f5f9",
    theme_color: "#1d4ed8",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
