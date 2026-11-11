import type { Metadata } from "next";
import { ThemeProvider } from "next-themes";
import { SettingsProvider } from "@/components/settings/SettingsProvider";
import "./globals.css";
import "./workbench.css";
import "./anduril-knowledge.css";
import "./knowledge-database.css";
import "./topic-workbench.css";
import "./topicKnowledge.css";
import "./researchDatabase.css";
import "./knowledgeAssets.css";
import "./graphExplorer.css";

export const metadata: Metadata = {
  title: "春天研究院 · 专题研究工作台",
  description: "专题调查、行动追踪与知识维护。",
  icons: { icon: "/brand/vernal-institute-logo.png" },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      className="h-full antialiased"
      suppressHydrationWarning
    >
      <body className="h-full">
        <ThemeProvider attribute="class" defaultTheme="light" enableSystem={false}>
          <SettingsProvider>{children}</SettingsProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
