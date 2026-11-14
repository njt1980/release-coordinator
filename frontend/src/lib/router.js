import { useEffect, useState } from "react";

// Hash-based routing: three static pages, no server rewrites, no dependency.
export const ROUTES = {
  home: "/",
  howItWorks: "/how-it-works",
  onboard: "/onboard",
  runs: "/runs",
  assessments: "/assessments",
  review: "/review",
  inventory: "/inventory",
  observability: "/observability",
};

const KNOWN = new Set(Object.values(ROUTES));

const currentPath = () => {
  const path = window.location.hash.replace(/^#/, "") || ROUTES.home;
  return KNOWN.has(path) ? path : ROUTES.home;
};

export const href = (path) => `#${path}`;

/** Returns the current route path and re-renders when it changes. */
export function useRoute() {
  const [path, setPath] = useState(currentPath);
  useEffect(() => {
    const onChange = () => {
      setPath(currentPath());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}
