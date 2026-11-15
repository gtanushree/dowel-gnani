import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { Decisions } from "./Decisions";
import { Talk } from "./Talk";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {window.location.pathname.startsWith("/talk") ? <Talk /> : window.location.pathname.startsWith("/decisions") ? <Decisions /> : <App />}
  </StrictMode>,
);
