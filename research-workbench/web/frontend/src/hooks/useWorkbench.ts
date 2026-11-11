"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { getWorkbenchSettings, putWorkbenchSettings, readableError, type EngineId, type WorkbenchSettings } from "@/lib/workbench";

export function useWorkbench() {
  const [settings, setSettings] = useState<WorkbenchSettings | null>(null);
  const [engine, updateEngine] = useState<EngineId>("ollama");
  const [switching, setSwitching] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const initialized = useRef(false);
  const refresh = useCallback(async () => {
    try {
      const next = await getWorkbenchSettings();
      setSettings(next);
      if (!initialized.current) { updateEngine(next.engine); initialized.current = true; }
      setError(null);
    } catch (error) { setError(readableError(error)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const apply = useCallback((next: WorkbenchSettings) => { setSettings(next); setError(null); }, []);
  const setEngine = useCallback(async (engine: EngineId) => {
    setSwitching(true);
    try { const next = await putWorkbenchSettings({ engine }); apply(next); updateEngine(engine); }
    catch (error) { setError(readableError(error)); }
    finally { setSwitching(false); }
  }, [apply]);
  return { settings, engine, setEngine, switching, loading, error, refresh, apply };
}
