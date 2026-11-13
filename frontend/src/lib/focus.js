import { createContext } from "react";

// What the user is looking at, so the assistant can be asked about it.
export const FocusContext = createContext({ runId: null, setRunId: () => {} });
