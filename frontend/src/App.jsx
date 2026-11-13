import { useEffect } from "react";
import Landing from "./pages/Landing.jsx";
import HowItWorks from "./pages/HowItWorks.jsx";
import Onboarding from "./pages/Onboarding.jsx";
import { ROUTES, useRoute } from "./lib/router.js";

const PAGES = {
  [ROUTES.home]: { component: Landing, title: "Release Coordinator" },
  [ROUTES.howItWorks]: { component: HowItWorks, title: "How it works · Release Coordinator" },
  [ROUTES.onboard]: { component: Onboarding, title: "Onboarding · Release Coordinator" },
};

export default function App() {
  const path = useRoute();
  const page = PAGES[path];
  useEffect(() => {
    document.title = page.title;
  }, [page]);
  const Page = page.component;
  return <Page />;
}
