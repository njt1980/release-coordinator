import { useEffect, useMemo, useState } from "react";
import Landing from "./pages/Landing.jsx";
import HowItWorks from "./pages/HowItWorks.jsx";
import Onboarding from "./pages/Onboarding.jsx";
import Runs from "./pages/Runs.jsx";
import Assessments from "./pages/Assessments.jsx";
import Review from "./pages/Review.jsx";
import Inventory from "./pages/Inventory.jsx";
import Observability from "./pages/Observability.jsx";
import AskPanel from "./components/AskPanel.jsx";
import { FocusContext } from "./lib/focus.js";
import { ROUTES, useRoute } from "./lib/router.js";

const PAGES = {
  [ROUTES.home]: { component: Landing, title: "Release Coordinator", name: "overview" },
  [ROUTES.howItWorks]: { component: HowItWorks, title: "How it works · Release Coordinator", name: "how it works" },
  [ROUTES.onboard]: { component: Onboarding, title: "Onboarding · Release Coordinator", name: "onboarding" },
  [ROUTES.runs]: { component: Runs, title: "Runs · Release Coordinator", name: "runs" },
  [ROUTES.assessments]: { component: Assessments, title: "Assessments · Release Coordinator", name: "assessments" },
  [ROUTES.review]: { component: Review, title: "Dependency review · Release Coordinator", name: "dependency review" },
  [ROUTES.inventory]: { component: Inventory, title: "Inventory · Release Coordinator", name: "inventory" },
  [ROUTES.observability]: { component: Observability, title: "Telemetry · Release Coordinator", name: "telemetry" },
};

export default function App() {
  const path = useRoute();
  const page = PAGES[path];
  const [runId, setRunId] = useState(null);
  const focus = useMemo(() => ({ runId, setRunId }), [runId]);
  useEffect(() => {
    document.title = page.title;
  }, [page]);
  const Page = page.component;
  return (
    <FocusContext.Provider value={focus}>
      <Page />
      <AskPanel
        page={page.name}
        runId={path === ROUTES.runs ? runId : null}
        raised={path === ROUTES.onboard}
      />
    </FocusContext.Provider>
  );
}
