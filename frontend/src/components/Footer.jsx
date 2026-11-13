import { LogoMark } from "./Nav.jsx";
import { ROUTES, href } from "../lib/router.js";

export default function Footer() {
  return (
    <footer className="border-t border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-8 text-sm text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div className="flex items-center gap-2">
          <LogoMark className="h-6 w-6" />
          <span>Release Coordinator · pilot · read-only by design</span>
        </div>
        <div className="flex gap-4">
          <a href={href(ROUTES.howItWorks)} className="hover:text-slate-900">
            How it works
          </a>
          <a href={href(ROUTES.onboard)} className="hover:text-slate-900">
            Onboard
          </a>
        </div>
      </div>
    </footer>
  );
}
