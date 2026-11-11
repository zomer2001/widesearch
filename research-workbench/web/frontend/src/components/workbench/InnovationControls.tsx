"use client";

import { ChevronDown, LockKeyhole, Sparkles } from "lucide-react";
import { RESEARCH_MODULES } from "@/lib/innovations";
import type { ResearchModuleId } from "@/lib/types";

export default function InnovationControls({ modules, locked, running, onChange, onNew }: {
  modules: ResearchModuleId[]; locked: boolean; running: boolean;
  onChange: (modules: ResearchModuleId[]) => void; onNew: () => void;
}) {
  return <details className="wb-module-controls">
    <summary><span><Sparkles size={14} />可选研究增强 · 试验功能</span><small>{modules.length ? `已选 ${modules.length} 项` : locked ? "本次未启用" : "默认关闭"}</small><ChevronDown size={13} /></summary>
    <p className="wb-module-note">已接入，完整多步流程仍在验证；默认关闭。</p>
    <fieldset disabled={locked || running}>
      {RESEARCH_MODULES.map((module) => <label key={module.id} className="wb-module-option">
        <span><strong>{module.label}</strong><small>{module.description}</small></span>
        <input type="checkbox" role="switch" aria-label={module.label} checked={modules.includes(module.id)} onChange={(event) => onChange(event.target.checked ? [...modules, module.id] : modules.filter((id) => id !== module.id))} />
      </label>)}
    </fieldset>
    {locked ? <p className="wb-module-lock"><LockKeyhole size={12} /><span>本次研究的组合已固定，续问将沿用。<button type="button" onClick={onNew} disabled={running}>新建任务后可修改</button></span></p> : <p className="wb-module-note">仅对这次新建研究生效，运行记录会区分已启用与实际触发。</p>}
  </details>;
}
