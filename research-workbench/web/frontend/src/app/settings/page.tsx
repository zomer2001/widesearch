import type { Metadata } from "next";

import SettingsPage from "@/components/settings/SettingsPage";

export const metadata: Metadata = {
  title: "Settings — 资料研究台",
};

export default function Page() {
  return <SettingsPage />;
}
