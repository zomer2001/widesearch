"use client";

import { useRef, useState } from "react";
import { Check, CheckCircle2, Loader2, PlugZap, Settings2, X } from "lucide-react";
import useDialogFocus from "@/hooks/useDialogFocus";
import { ENGINE_IDS, ENGINE_LABELS, crawlPage, probeSearch, probeEngine, putWorkbenchSettings, readableError, type CrawlPreview, type SearchPreview, type EngineId, type WorkbenchSettings as Settings } from "@/lib/workbench";

export default function WorkbenchSettings({ settings, engine, onClose, onSaved }: {
  settings: Settings; engine: EngineId; onClose: () => void; onSaved: (settings: Settings) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<EngineId | "search">(engine);
  const [deepseek, setDeepseek] = useState({ model: settings.deepseek.model, api_base: settings.deepseek.api_base, api_key: "" });
  const [openai, setOpenai] = useState({ model: settings.openai?.model ?? "gpt-4o", api_base: settings.openai?.api_base ?? "https://api.openai.com/v1", api_key: "" });
  const [ollama, setOllama] = useState({ ...settings.ollama });
  const [search, setSearch] = useState({ provider: settings.search.provider, web_search_model: settings.search.web_search_model ?? "gpt-4o", browser_backend: settings.search.browser_backend, serper_api_key: "", tavily_api_key: "" });
  const [allowedDomains, setAllowedDomains] = useState((settings.search.web_search_allowed_domains ?? []).join(", "));
  const [busy, setBusy] = useState<"save" | "probe" | "crawl" | "search" | null>(null);
  const [searchQuery, setSearchQuery] = useState("Python official website");
  const [searchResult, setSearchResult] = useState<SearchPreview | null>(null);
  const [crawlUrl, setCrawlUrl] = useState("");
  const [crawlResult, setCrawlResult] = useState<CrawlPreview | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [saved, setSaved] = useState(false);
  const openaiSearch = search.provider === "openai_web_search";
  const openaiSearchKeyConfigured = settings.search.openai_key_configured ?? settings.openai?.api_key_configured ?? false;
  const searchDomains = () => [...new Set(allowedDomains.split(/[,，\n]+/).map((value) => value.trim().toLowerCase()).filter(Boolean))];
  useDialogFocus({ containerRef: root, initialFocusRef: close, onClose });

  const trySearch = async () => {
    setBusy("search"); setMessage(null); setSearchResult(null);
    try {
      const key = search.provider === "tavily" ? search.tavily_api_key : search.provider === "serper" ? search.serper_api_key : "";
      const result = await probeSearch(search.provider, key, searchQuery.trim(), openaiSearch ? search.web_search_model.trim() : undefined, openaiSearch ? searchDomains() : undefined);
      setSearchResult(result);
      setMessage({ ok: result.ok, text: result.message });
    } catch (error) { setMessage({ ok: false, text: readableError(error) }); }
    finally { setBusy(null); }
  };

  const tryCrawl = async () => {
    setBusy("crawl"); setMessage(null); setCrawlResult(null);
    try {
      const result = await crawlPage(crawlUrl.trim(), search.browser_backend);
      setCrawlResult(result);
      if (!result.ok) setMessage({ ok: false, text: result.error || `网页未返回可读正文（${result.status}）` });
    } catch (error) { setMessage({ ok: false, text: readableError(error) }); }
    finally { setBusy(null); }
  };

  const save = async (probe = false) => {
    setBusy(probe ? "probe" : "save"); setMessage(null); setSaved(false);
    try {
      const next = await putWorkbenchSettings({ engine, deepseek, openai, ollama, search: { ...search, web_search_allowed_domains: searchDomains() } });
      onSaved(next); setDeepseek((value) => ({ ...value, api_key: "" }));
      setOpenai((value) => ({ ...value, api_key: "" }));
      setSearch((value) => ({ ...value, serper_api_key: "", tavily_api_key: "" }));
      if (probe && tab !== "search") {
        const result = await probeEngine(tab);
        setMessage({ ok: result.ok, text: result.message });
      } else { setSaved(true); setMessage({ ok: true, text: "配置已保存。下一次研究将使用新的设置。" }); }
    } catch (error) { setMessage({ ok: false, text: readableError(error) }); }
    finally { setBusy(null); }
  };

  return <div className="wb-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div ref={root} role="dialog" aria-modal="true" aria-labelledby="wb-settings-title" className="wb-settings">
      <header className="wb-modal-head"><div><span className="wb-eyebrow">连接与配置</span><h2 id="wb-settings-title"><Settings2 size={19} /> 工作台设置</h2></div><button ref={close} className="wb-icon-button" aria-label="关闭设置" onClick={onClose}><X size={20} /></button></header>
      <div className="wb-setting-tabs" role="tablist" aria-label="配置分类">
        {([...ENGINE_IDS, "search"] as const).map((id) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => { setTab(id); setMessage(null); }} disabled={!!busy} className={tab === id ? "active" : ""}>{id === "search" ? "联网检索" : ENGINE_LABELS[id]}</button>)}
      </div>
      <div className="wb-settings-body">
        {tab === "deepseek" && <>
          <p className="wb-help">使用 DeepSeek 云端模型完成规划、阅读与写作。密钥只保存在本机后端。</p>
          <Field label="API 密钥" type="password" autoComplete="new-password" value={deepseek.api_key} placeholder={settings.deepseek.api_key_configured ? "已配置 · 留空保留原密钥" : "输入 DeepSeek API Key"} onChange={(value) => setDeepseek({ ...deepseek, api_key: value })} />
          <Field label="模型名称" value={deepseek.model} onChange={(value) => setDeepseek({ ...deepseek, model: value })} />
          <Field label="服务地址" value={deepseek.api_base} onChange={(value) => setDeepseek({ ...deepseek, api_base: value })} />
        </>}
        {tab === "openai" && <>
          <p className="wb-help">使用 OpenAI 云端模型完成规划、阅读与写作，默认使用 GPT-4o。密钥只保存在本机后端。</p>
          <Field label="API 密钥" type="password" autoComplete="new-password" value={openai.api_key} placeholder={settings.openai?.api_key_configured ? "已配置 · 留空保留原密钥" : "输入 OpenAI API Key"} onChange={(value) => setOpenai({ ...openai, api_key: value })} />
          <Field label="模型名称" value={openai.model} onChange={(value) => setOpenai({ ...openai, model: value })} />
          <Field label="服务地址" value={openai.api_base} onChange={(value) => setOpenai({ ...openai, api_base: value })} />
        </>}
        {tab === "ollama" && <>
          <p className="wb-help">连接本机 Ollama。请先启动服务并准备支持工具调用的模型。</p>
          <Field label="服务地址" value={ollama.api_base} onChange={(value) => setOllama({ ...ollama, api_base: value })} />
          <label className="wb-field"><span>模型名称</span><input list="wb-ollama-models" value={ollama.model} placeholder="选择或输入本机模型" onChange={(event) => setOllama({ ...ollama, model: event.target.value })} /><datalist id="wb-ollama-models">{settings.ollama_models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}</datalist></label>
          <p className="wb-help">{settings.ollama_models.length ? `已发现 ${settings.ollama_models.length} 个本机模型。` : "尚未发现模型，可输入已安装的模型名称后测试连接。"}</p>
        </>}
        {tab === "cli" && <>
          <div className="wb-cli-card"><span className="wb-cli-glyph">&gt;_</span><div><strong>CLI · 6-luna</strong><p>{settings.cli.model}</p></div><span className={`wb-status ${settings.cli.available ? "ready" : ""}`}>{settings.cli.available ? "已发现 CLI" : "尚未就绪"}</span></div>
          <p className="wb-help">通过本机命令行服务调用模型，使用已有登录。点击“测试连接”确认模型是否可用。</p>
          <p className="wb-setting-note">{settings.engines.find((item) => item.id === "cli")?.message}</p>
        </>}
        {tab === "search" && <>
          <p className="wb-help">搜索服务负责发现网页链接，网页读取方式负责实际读取正文。搜索模型与用于规划、阅读、写作的分析模型分别设置。</p>
          <div className="wb-field"><span>搜索服务</span><div className="wb-search-service-options" role="group" aria-label="搜索服务">
            {([{ id: "openai_web_search", label: "OpenAI · web_search", note: "复用 OpenAI 配置" }, { id: "ddgs", label: "免密钥 · DDGS", note: "无需密钥" }, { id: "serper", label: "Serper", note: "需要密钥" }, { id: "tavily", label: "Tavily", note: "需要密钥" }] as const).map((item) =>
              <button key={item.id} type="button" className={item.id === "openai_web_search" ? "wb-search-service-openai" : undefined} disabled={!!busy} aria-pressed={search.provider === item.id} onClick={() => { setSearch({ ...search, provider: item.id }); setSearchResult(null); setMessage(null); }}>
                <strong>{item.label}</strong><small>{item.note}</small>
              </button>)}
          </div></div>
          {openaiSearch ? <div className="wb-openai-search-settings">
            <Field label="联网搜索模型" value={search.web_search_model} placeholder="gpt-4o" onChange={(value) => setSearch({ ...search, web_search_model: value })} />
            <Field label="限定网站（可选）" value={allowedDomains} placeholder="anduril.com, github.com" onChange={setAllowedDomains} />
            <p className="wb-help">填写域名，多个网站用逗号分隔；留空搜索全网。</p>
            <p className="wb-help">此模型调用 OpenAI 联网搜索获取来源链接；网页正文会另行读取，搜索回答不作为原文保存。分析模型仍使用各引擎页中的设置。</p>
            <p className="wb-help">{openaiSearchKeyConfigured ? "已配置 OpenAI 密钥；搜索复用已保存的密钥与服务地址。" : "尚未配置 OpenAI 密钥，请先到 OpenAI API 页填写并保存。"}</p>
            <button type="button" className="wb-secondary" disabled={!!busy} onClick={() => { setTab("openai"); setMessage(null); }}>前往 OpenAI 配置</button>
            <p className="wb-setting-note">测试会调用 OpenAI 的联网搜索服务，使用已保存的密钥。</p>
          </div> : search.provider === "ddgs" ? <p className="wb-help">通过公共搜索引擎检索网页，不需要 API 密钥。可先测试搜索，再保存配置。</p> : search.provider === "tavily" ? <Field label="Tavily API 密钥" type="password" autoComplete="new-password" value={search.tavily_api_key} placeholder={settings.search.tavily_key_configured ? "已配置 · 留空保留原密钥" : "输入 Tavily API Key"} onChange={(value) => setSearch({ ...search, tavily_api_key: value })} /> : <Field label="Serper API 密钥" type="password" autoComplete="new-password" value={search.serper_api_key} placeholder={settings.search.serper_key_configured ? "已配置 · 留空保留原密钥" : "输入 Serper API Key"} onChange={(value) => setSearch({ ...search, serper_api_key: value })} />}
          <Field label="测试关键词" value={searchQuery} onChange={setSearchQuery} />
          <button className="wb-secondary" disabled={!!busy || !searchQuery.trim() || (openaiSearch && (!openaiSearchKeyConfigured || !search.web_search_model.trim()))} onClick={() => void trySearch()}>{busy === "search" ? <Loader2 size={15} className="animate-spin" /> : <PlugZap size={15} />}{openaiSearch ? "测试 OpenAI 联网搜索" : "测试搜索"}</button>
          {searchResult?.ok && <section aria-label="搜索测试结果">
            <p className="wb-help">{searchResult.message} · {searchResult.elapsed_s} 秒</p>
            <ul>{searchResult.results.map((result) => <li key={result.url}><a href={result.url} target="_blank" rel="noreferrer">{result.title || result.url}</a></li>)}</ul>
          </section>}
          <label className="wb-field"><span>网页读取方式</span><select value={search.browser_backend} onChange={(event) => { setSearch({ ...search, browser_backend: event.target.value }); setCrawlResult(null); }}>
            <option value="auto">自动 · 正文提取后按需渲染</option>
            <option value="crawl4ai">浏览器抓取 · Crawl4AI</option>
            <option value="trafilatura">正文提取 · Trafilatura</option>
            <option value="aiohttp">轻量 · 直接读取网页</option>
            <option value="jina">外部服务 · Jina Reader</option>
          </select></label>
          <p className="wb-help">自动模式先在本机提取正文，文本较短或读取失败时再用浏览器渲染。前四种方式不需要抓取服务密钥。</p>
          <Field label="试抓网页" type="url" value={crawlUrl} placeholder="粘贴公开网页网址，例如 https://example.com" onChange={setCrawlUrl} />
          <button className="wb-secondary" disabled={!!busy || !crawlUrl.trim()} onClick={() => void tryCrawl()}>{busy === "crawl" ? <Loader2 size={15} className="animate-spin" /> : <PlugZap size={15} />}读取并预览</button>
          <p className="wb-help">试抓仅使用当前选择，不会保存设置或调用模型；填写网址即可使用，无需搜索密钥。</p>
          {crawlResult?.ok && <section className="wb-crawl-preview" aria-label="网页抓取结果">
            <strong>{crawlResult.title || "网页读取成功"}</strong>
            <p className="wb-help">实际方式：{crawlResult.metadata.fetch_backend} · {crawlResult.characters} 字符 · {crawlResult.elapsed_s} 秒{crawlResult.metadata.cache_hit ? " · 本机缓存" : ""}</p>
            <p className="wb-help">{crawlResult.metadata.fetched_at && `抓取时间：${new Date(crawlResult.metadata.fetched_at).toLocaleString()}`}</p>
            {crawlResult.metadata.fallback_reason && <p className="wb-help">{crawlResult.metadata.fallback_reason}{crawlResult.metadata.browser_error ? `；${crawlResult.metadata.browser_error}` : ""}</p>}
            <a href={crawlResult.url} target="_blank" rel="noreferrer">查看原网页</a>
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 220, overflow: "auto", fontSize: 12, padding: 12, background: "#f3f5f1", borderRadius: 8 }}>{crawlResult.preview}</pre>
          </section>}
        </>}
        {message && <div role={message.ok ? "status" : "alert"} className={`wb-message ${message.ok ? "success" : "error"}`}>{message.ok && <CheckCircle2 size={16} />}<span>{message.text}</span></div>}
      </div>
      <footer className="wb-settings-footer"><span className="wb-help">密钥留空不会覆盖已有配置</span><div>{tab !== "search" && <button className="wb-secondary" disabled={!!busy} onClick={() => void save(true)}>{busy === "probe" ? <Loader2 size={15} className="animate-spin" /> : <PlugZap size={15} />}保存并测试</button>}<button className="wb-primary" disabled={!!busy} onClick={() => void save()}>{busy === "save" ? <Loader2 size={15} className="animate-spin" /> : saved ? <Check size={15} /> : null}保存配置</button></div></footer>
    </div>
  </div>;
}

function Field({ label, value, onChange, ...props }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string; autoComplete?: string }) {
  return <label className="wb-field"><span>{label}</span><input {...props} value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}
