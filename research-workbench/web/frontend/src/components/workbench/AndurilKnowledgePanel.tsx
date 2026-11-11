"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowRight, BookOpen, CheckCircle2, ChevronRight, Code2, ExternalLink, FileText, Layers3, Loader2, Network, RefreshCw, Search, X } from "lucide-react";
import useDialogFocus from "@/hooks/useDialogFocus";
import { getAndurilKnowledge } from "@/lib/api";
import {
  knowledgeMatches, knowledgeSources, knowledgeUpdatedLabel, maturityLabel, reproductionLabel, safeKnowledgeUrl,
  type AndurilKnowledge, type AndurilProduct, type AndurilSource,
} from "@/lib/andurilKnowledge";

type ReadingTab = "products" | "routes" | "maturity" | "reproduction";
const TABS = [
  { id: "products", name: "产品谱系", icon: Layers3 },
  { id: "routes", name: "技术路线", icon: Network },
  { id: "maturity", name: "成熟度", icon: CheckCircle2 },
  { id: "reproduction", name: "开源单元", icon: Code2 },
] as const;

export default function AndurilKnowledgePanel({ onClose, onOpenLocalSource, sourceOpen = false }: {
  onClose: () => void;
  onOpenLocalSource: (ref: string) => void;
  sourceOpen?: boolean;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const [data, setData] = useState<AndurilKnowledge | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState(0);
  const [loadedRequest, setLoadedRequest] = useState(-1);
  const [tab, setTab] = useState<ReadingTab>("products");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const busy = loadedRequest !== requestId;
  useDialogFocus({ containerRef: root, initialFocusRef: close, onClose, active: !sourceOpen });

  useEffect(() => {
    const controller = new AbortController();
    void getAndurilKnowledge(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value); setError(null); setLoadedRequest(requestId);
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : String(cause)); setLoadedRequest(requestId);
      });
    return () => controller.abort();
  }, [requestId]);

  const openProduct = (id: string) => { setSelectedId(id); setQuery(""); setTab("products"); };
  const sources = (ids: string[]) => <SourceNotes ids={ids} sources={data?.sources ?? []} onOpenLocalSource={onOpenLocalSource} />;
  const productLinks = (ids: string[]) => {
    const products = data?.products.filter((product) => ids.includes(product.id)) ?? [];
    return products.length > 0 && <div className="ak-related"><span>关联产品</span>{products.map((product) => <button key={product.id} onClick={() => openProduct(product.id)}>{product.name}<ChevronRight size={12} /></button>)}</div>;
  };
  const products = data?.products.filter((product) => knowledgeMatches(query, product.name, product.family, product.category, product.overview)) ?? [];
  const families = Array.from(new Set(products.map((product) => product.family || "未分类产品")));
  const selected = products.find((product) => product.id === selectedId) ?? products[0];
  const routes = data?.routes.filter((route) => knowledgeMatches(query, route.name, route.problem, route.mechanism)) ?? [];
  const technologies = data?.technologies.filter((technology) => knowledgeMatches(query, technology.name, technology.mechanism)) ?? [];
  const units = data?.reproduction_units.filter((unit) => knowledgeMatches(query, unit.name, unit.description)) ?? [];
  const stages = Array.from(new Set(products.map((product) => product.maturity?.stage || "未知")));
  const noResults = query.trim() ? "没有找到匹配条目，试试其他关键词。" : "这部分尚未形成有来源支持的可读条目。";

  return <div className="ak-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !sourceOpen) onClose(); }}>
    <div ref={root} role="dialog" aria-modal="true" aria-labelledby="ak-title" className="ak-panel" tabIndex={-1}>
      <header className="ak-header"><div className="ak-heading"><span className="ak-logo"><BookOpen size={23} /></span><div><span className="wb-eyebrow">知识阅读</span><h1 id="ak-title">{data?.title || "Anduril 知识"}</h1></div></div><div className="ak-header-actions"><button className="wb-icon-button" aria-label="刷新知识" disabled={busy} onClick={() => setRequestId((id) => id + 1)}><RefreshCw size={17} className={busy ? "animate-spin" : ""} /></button><button ref={close} className="wb-icon-button" aria-label="关闭知识阅读" onClick={onClose}><X size={21} /></button></div></header>
      <div className="ak-scroll">
        {busy && !data && <div className="ak-empty" role="status"><Loader2 size={23} className="animate-spin" /><p>正在读取已整理的知识…</p></div>}
        {error && <div className="ak-error" role="alert"><p>{error}</p><button className="wb-secondary" disabled={busy} onClick={() => setRequestId((id) => id + 1)}>重试读取</button></div>}
        {data && <>
          <section className="ak-intro"><div className="ak-intro-meta"><span className="ak-status">{data.status === "ready" ? "可读知识已整理" : "知识持续整理中"}</span><span>{knowledgeUpdatedLabel(data.updated_at)}</span></div><p className="ak-summary">{data.summary || "从已归档的公开资料阅读产品、技术与进展。"}</p><p className="ak-assurance">依据已归档原文整理；公开描述不等于独立验证。每项内容可查看对应原文依据。</p><div className="ak-counts">{[{ name: "产品条目", count: data.products.length }, { name: "技术机制", count: data.technologies.length }, { name: "技术路线", count: data.routes.length }, { name: "开源参考", count: data.reproduction_units.length }].map((item) => <div key={item.name}><strong>{item.count}</strong><span>{item.name}</span></div>)}</div></section>
          <div className="ak-navigation"><div className="ak-tabs" role="tablist" aria-label="知识视图">{TABS.map(({ id, name, icon: Icon }) => <button key={id} id={`ak-tab-${id}`} role="tab" aria-selected={tab === id} aria-controls={`ak-view-${id}`} className={tab === id ? "active" : ""} onClick={() => setTab(id)}><Icon size={15} />{name}</button>)}</div><label className="ak-search"><Search size={14} /><input aria-label="搜索知识条目" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、机制…" /></label></div>
          <section id={`ak-view-${tab}`} role="tabpanel" aria-labelledby={`ak-tab-${tab}`} className="ak-view">
            {tab === "products" && <><SectionHeading title="从产品进入技术" description="按产品家族浏览，点击条目阅读技术细节、机制与来源。" />{products.length === 0 ? <Empty>{noResults}</Empty> : <div className="ak-product-layout"><div className="ak-product-list">{families.map((family) => <section className="ak-family" key={family}><h3>{family}<span>{products.filter((product) => (product.family || "未分类产品") === family).length}</span></h3><div className="ak-product-grid">{products.filter((product) => (product.family || "未分类产品") === family).map((product) => <button key={product.id} aria-pressed={selected?.id === product.id} className={`ak-product-card ${selected?.id === product.id ? "selected" : ""}`} onClick={() => setSelectedId(product.id)}><span className="ak-product-category">{product.category || "类别待补充"}</span><strong>{product.name}<ArrowRight size={14} /></strong><p>{product.overview || "产品概述待补充。"}</p><span className="ak-card-stage">{maturityLabel(product.maturity?.stage)}</span></button>)}</div></section>)}</div>{selected && <ProductDetail key={selected.id} product={selected} data={data} sourceNotes={sources} />}</div>}</>}
            {tab === "routes" && <><SectionHeading title="技术如何解决问题" description="从目标问题、实现机制到取舍，阅读技术与产品之间的联系。" /><div className="ak-route-list">{routes.length === 0 ? <Empty>{noResults}</Empty> : routes.map((route, index) => <article className="ak-route-card" key={route.id}><span className="ak-route-no">{String(index + 1).padStart(2, "0")}</span><div><h3>{route.name}</h3><ReadField label="要解决的问题" text={route.problem} /><ReadField label="实现机制" text={route.mechanism} /><TextList title="取舍与约束" items={route.tradeoffs} /><TextList title="尚缺的信息" items={route.limitations} />{productLinks(route.product_ids)}{sources(route.source_ids)}</div></article>)}</div><SectionHeading title="技术机制" description="阅读具体机制，追溯其关联产品与证据。" /><div className="ak-technology-grid">{technologies.length === 0 ? <Empty>{noResults}</Empty> : technologies.map((technology) => <article className="ak-technology-card" key={technology.id}><h3>{technology.name}</h3><p>{technology.mechanism || "机制细节尚未整理。"}</p>{productLinks(technology.product_ids)}<TextList title="尚缺的信息" items={technology.limitations} />{sources(technology.source_ids)}</article>)}</div></>}
            {tab === "maturity" && <><SectionHeading title="产品进展与依据" description="按原文公开披露的阶段分组，阅读每项判断所依据的声明。" />{stages.length === 0 ? <Empty>{noResults}</Empty> : <div className="ak-maturity-grid">{stages.map((stage) => <section className="ak-maturity-column" key={stage}><h3>{maturityLabel(stage)}<span>{products.filter((product) => (product.maturity?.stage || "未知") === stage).length}</span></h3>{products.filter((product) => (product.maturity?.stage || "未知") === stage).map((product) => <article key={product.id}><button className="ak-product-link" onClick={() => openProduct(product.id)}>{product.name}<ArrowRight size={14} /></button><p>{product.maturity?.basis || "阶段依据尚未补充。"}</p>{sources(product.maturity?.source_ids ?? [])}</article>)}</section>)}</div>}</>}
            {tab === "reproduction" && <><SectionHeading title="可学习的开源单元" description="外部开源参考与本机复现结果分别记录；是否被 Anduril 采用以条目中的证据为准。" />{units.length === 0 ? <Empty>{noResults}</Empty> : <div className="ak-unit-list">{units.map((unit) => <article className="ak-unit-card" key={unit.id}><div className="ak-unit-head"><div><span className="ak-unit-kind">外部开源参考</span><h3>{unit.name}</h3></div><span className={`ak-reproduction-status ${unit.status === "passed" || unit.status === "reproduced" ? "passed" : ""}`}>{reproductionLabel(unit.status)}</span></div><p>{unit.description || "用途说明尚未整理。"}</p>{safeKnowledgeUrl(unit.repo_url) && <a className="ak-repo-link" href={safeKnowledgeUrl(unit.repo_url)!} target="_blank" rel="noreferrer">查看开源仓库<ExternalLink size={13} /></a>}<div className="ak-unit-meta"><span>许可证：{unit.license || "待确认"}</span><span>版本：{unit.commit || "尚未固定版本"}</span></div>{unit.related_technology_ids.length > 0 && <p className="ak-unit-related">关联技术：{data.technologies.filter((technology) => unit.related_technology_ids.includes(technology.id)).map((technology) => technology.name).join("、") || "待补充"}</p>}<div className="ak-unit-io"><TextList title="输入" items={unit.inputs} empty="输入要求待补充。" /><TextList title="输出" items={unit.outputs} empty="输出说明待补充。" /></div><TextList title="复现步骤" items={unit.steps} ordered empty="尚未整理可执行步骤。" /><TextList title="验收条件" items={unit.acceptance_criteria} empty="尚未定义验收条件。" /><TextList title="适用范围与缺口" items={unit.limitations} />{sources(unit.source_ids)}</article>)}</div>}</>}
          </section>
          {data.gaps.length > 0 && <section className="ak-gaps"><h2>继续补充的知识</h2><ul>{data.gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></section>}
        </>}
      </div>
    </div>
  </div>;
}

function ProductDetail({ product, data, sourceNotes }: {
  product: AndurilProduct;
  data: AndurilKnowledge;
  sourceNotes: (ids: string[]) => ReactNode;
}) {
  const technologies = data.technologies.filter((technology) => product.technology_ids.includes(technology.id));
  return <aside className="ak-detail" aria-label={`${product.name} 产品详情`}><div className="ak-detail-heading"><span>产品详情</span><span>{product.category || product.family}</span></div><h2>{product.name}</h2><p className="ak-detail-overview">{product.overview || "产品概述待补充。"}</p><section><h3>技术细节</h3>{product.technical_details.length === 0 ? <p className="ak-muted">已归档资料尚未形成可展示的技术细节。</p> : product.technical_details.map((detail, index) => <div className="ak-detail-field" key={index}><h4>{detail.label}</h4><p>{detail.text}</p>{sourceNotes(detail.source_ids)}</div>)}</section><section><h3>关联技术机制</h3>{technologies.length === 0 ? <p className="ak-muted">技术机制关联待补充。</p> : technologies.map((technology) => <div className="ak-detail-field" key={technology.id}><h4>{technology.name}</h4><p>{technology.mechanism || "机制说明待补充。"}</p>{sourceNotes(technology.source_ids)}</div>)}</section><section><h3>成熟度与依据</h3><span className="ak-card-stage">{maturityLabel(product.maturity?.stage)}</span><p>{product.maturity?.basis || "阶段依据尚未补充。"}</p>{sourceNotes(product.maturity?.source_ids ?? [])}</section><TextList title="尚缺的信息" items={product.limitations} /><section><h3>产品资料</h3>{sourceNotes(product.source_ids)}</section></aside>;
}

function SourceNotes({ ids, sources, onOpenLocalSource }: { ids: string[]; sources: AndurilSource[]; onOpenLocalSource: (ref: string) => void }) {
  const items = knowledgeSources(ids, sources);
  if (!items.length) return <p className="ak-source-empty">原文依据待补充。</p>;
  return <details className="ak-source-notes"><summary><FileText size={12} />查看原文依据 · {items.length}</summary><div>{items.map((source) => <article key={source.id}>{source.quote && <blockquote>{source.quote}</blockquote>}<div className="ak-source-link">{safeKnowledgeUrl(source.url) ? <a href={safeKnowledgeUrl(source.url)!} target="_blank" rel="noreferrer">{source.title || "查看来源"}<ExternalLink size={12} /></a> : <span>{source.title || "归档原文"}</span>}{source.ref_id?.startsWith("local://") && <button onClick={() => onOpenLocalSource(source.ref_id!)}>本地原文<ArrowRight size={12} /></button>}</div>{source.location && <p className="ak-source-location">{source.location}</p>}</article>)}</div></details>;
}

function SectionHeading({ title, description }: { title: string; description: string }) {
  return <div className="ak-section-heading"><h2>{title}</h2><p>{description}</p></div>;
}

function ReadField({ label, text }: { label: string; text: string }) {
  return <div className="ak-read-field"><h4>{label}</h4><p>{text || "内容待补充。"}</p></div>;
}

function TextList({ title, items, empty, ordered = false }: { title: string; items: string[]; empty?: string; ordered?: boolean }) {
  if (!items.length && !empty) return null;
  const List = ordered ? "ol" : "ul";
  return <section className="ak-text-list"><h4>{title}</h4>{items.length ? <List>{items.map((item, index) => <li key={index}>{item}</li>)}</List> : <p className="ak-muted">{empty}</p>}</section>;
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="ak-empty"><BookOpen size={22} /><p>{children}</p></div>;
}
