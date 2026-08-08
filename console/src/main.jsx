import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";

function ConsoleShell() {
  return (
    <main>
      <h1>DocuMind</h1>
      <p>Self-hosted document intelligence is initializing.</p>
      <p className="detail">The authenticated console is delivered with the document-domain features.</p>
    </main>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ConsoleShell />
  </StrictMode>,
);
