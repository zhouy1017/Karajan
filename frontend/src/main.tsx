import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
const isCommanderPrototype =
  import.meta.env.DEV && window.location.pathname === "/prototype/commander";

if (isCommanderPrototype) {
  import("./prototype/CommanderPrototype").then(({ CommanderPrototype }) =>
    root.render(<CommanderPrototype />),
  );
} else {
  root.render(<App />);
}
