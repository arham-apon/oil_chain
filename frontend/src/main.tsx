import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import { connectWs, qc } from "@/lib/api";
import Layout from "@/components/Layout";
import Overview from "@/pages/Overview";
import Inventory from "@/pages/Inventory";
import Risk from "@/pages/Risk";
import Supply from "@/pages/Supply";
import Decisions from "@/pages/Decisions";
import Copilot from "@/pages/Copilot";
import Health from "@/pages/Health";
import Audit from "@/pages/Audit";

function App() {
  useEffect(() => connectWs(), []);
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="inventory" element={<Inventory />} />
          <Route path="risk" element={<Risk />} />
          <Route path="supply" element={<Supply />} />
          <Route path="decisions" element={<Decisions />} />
          <Route path="copilot" element={<Copilot />} />
          <Route path="health" element={<Health />} />
          <Route path="audit" element={<Audit />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><QueryClientProvider client={qc}><App /></QueryClientProvider></React.StrictMode>,
);
