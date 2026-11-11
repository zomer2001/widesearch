import type { Metadata } from "next";
import SkillLibraryPage from "@/components/skills/SkillLibraryPage";

export const metadata: Metadata = {
  title: "技能库 — 资料研究台",
};

export default function Page() {
  return <SkillLibraryPage />;
}
