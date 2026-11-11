"use client";

import { ChevronDown, ChevronUp, Copy, CornerDownRight, Download, RotateCcw } from "lucide-react";
import { useSettings } from "@/components/settings/SettingsProvider";

interface Props {
  query: string;
  markdown: string;
  collapsible: boolean;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onRerun: () => void;
  onContinue?: () => void;
  runDisabled?: boolean;
}

const buttonClass = "grid h-8 w-8 shrink-0 place-items-center rounded-md text-ink-faint transition-colors hover:bg-surface-2 hover:text-ink active:bg-clay disabled:cursor-not-allowed disabled:opacity-30";

function markdownFilename(query: string): string {
  const stem = query
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/\s+/g, "-")
    .slice(0, 64)
    .replace(/^-+|-+$/g, "");
  return `${stem || "research-answer"}.md`;
}

export default function AnswerActions({
  query,
  markdown,
  collapsible,
  collapsed,
  onToggleCollapse,
  onRerun,
  onContinue,
  runDisabled = false,
}: Props) {
  const { notify } = useSettings();

  const copyAnswer = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      notify("回答已复制", "success");
    } catch (error) {
      notify(`复制失败： ${error instanceof Error ? error.message : String(error)}。可手动选中文字复制。`);
    }
  };

  const downloadMarkdown = () => {
    try {
      const content = `# ${query}\n\n${markdown.trim()}\n`;
      const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = markdownFilename(query);
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      globalThis.setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify("已开始下载 Markdown", "success");
    } catch (error) {
      notify(`导出失败： ${error instanceof Error ? error.message : String(error)}。可复制回答文字。`);
    }
  };

  return (
    <div role="toolbar" aria-label="回答操作" className="mt-2 flex min-h-8 items-center gap-0.5 border-t border-line pt-2">
      <button type="button" onClick={() => void copyAnswer()} aria-label="复制回答" title="复制回答" className={buttonClass}>
        <Copy size={15} />
      </button>
      <button type="button" onClick={downloadMarkdown} aria-label="下载 Markdown" title="下载 Markdown" className={buttonClass}>
        <Download size={15} />
      </button>
      {collapsible && (
        <button
          type="button"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "展开回答" : "收起回答"}
          title={collapsed ? "展开回答" : "收起回答"}
          className={buttonClass}
        >
          {collapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
        </button>
      )}
      <span className="mx-1 h-4 w-px bg-line" />
      <button
        type="button"
        onClick={onRerun}
        disabled={runDisabled}
        aria-label="重新运行"
        title={runDisabled ? "请等待当前任务完成" : "重新运行"}
        className={buttonClass}
      >
        <RotateCcw size={15} />
      </button>
      {onContinue && (
        <button
          type="button"
          onClick={onContinue}
          disabled={runDisabled}
          aria-label="继续研究"
          title={runDisabled ? "请等待当前任务完成" : "继续研究"}
          className={buttonClass}
        >
          <CornerDownRight size={16} />
        </button>
      )}
    </div>
  );
}
