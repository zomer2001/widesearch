"use client";

import { useCallback, useRef } from "react";
import { ExternalLink, X } from "lucide-react";
import useDialogFocus from "@/hooks/useDialogFocus";

export interface OpenedSource { title: string; url: string; text_content: string; quote?: string }
export default function AssetSourceDrawer({ source, onClose }: { source: OpenedSource; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  const focusQuote = useCallback((node: HTMLElement | null) => node?.scrollIntoView({ block: "center" }), []);
  useDialogFocus({ containerRef: root, onClose });
  const index = source.quote ? source.text_content.indexOf(source.quote) : -1;
  return <div className="ka-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <section className="ka-source-drawer" role="dialog" aria-modal="true" aria-label="来源原文" ref={root} tabIndex={-1}>
      <header><div><small>归档原文</small><h2>{source.title}</h2></div><button className="tw-icon" aria-label="关闭来源原文" onClick={onClose}><X size={20} /></button></header>
      {/^https?:\/\//i.test(source.url) && <a className="ka-external" href={source.url} target="_blank" rel="noreferrer"><ExternalLink size={13} />访问来源网页</a>}
      <pre>{index >= 0 && source.quote ? <>{source.text_content.slice(0, index)}<mark ref={focusQuote}>{source.quote}</mark>{source.text_content.slice(index + source.quote.length)}</> : source.text_content}</pre>
    </section>
  </div>;
}
