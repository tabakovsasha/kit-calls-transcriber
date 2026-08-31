/**
 * Minimal hash-based router.
 *
 * Deliberately hand-rolled instead of pulling in react-router: the app needs
 * four static sections and no nested routes, params or loaders. Hash routing
 * also means the Vite dev server and any later static host serve the SPA
 * without a history-fallback rewrite rule.
 */

import { useCallback, useEffect, useState } from "react";

function currentPath() {
  const hash = window.location.hash.replace(/^#/, "");
  return hash || "/";
}

export function useRoute(fallback) {
  const [path, setPath] = useState(currentPath);

  useEffect(() => {
    const onChange = () => setPath(currentPath());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((next) => {
    window.location.hash = next;
  }, []);

  // Normalise unknown paths to the default section.
  useEffect(() => {
    if (path === "/" && fallback) window.location.replace(`#${fallback}`);
  }, [path, fallback]);

  return { path, navigate };
}

export function Link({ to, className, children, ...props }) {
  return (
    <a href={`#${to}`} className={className} {...props}>
      {children}
    </a>
  );
}
