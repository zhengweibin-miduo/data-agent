import { type FormEvent, useEffect, useState } from "react";

import { formatApiError } from "../api/client";
import { deleteMemory, getMemory, getMemoryHistory, searchMemories, updateMemory } from "../api/dataAgent";
import type { MemoryDetail, MemoryHistoryPage, MemorySearchResponse } from "../api/types";

export function KnowledgePage() {
  const params = new URLSearchParams(window.location.search);
  const [source, setSource] = useState(params.get("source") ?? "commerce_prod");
  const [query, setQuery] = useState(params.get("query") ?? "");
  const [results, setResults] = useState<MemorySearchResponse | null>(null);
  const [selected, setSelected] = useState<MemoryDetail | null>(null);
  const [history, setHistory] = useState<MemoryHistoryPage | null>(null);
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadingMemoryUid, setLoadingMemoryUid] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [reprocessNotice, setReprocessNotice] = useState("");
  const scoreFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });

  const openMemory = async (uid: string) => {
    setBusy(true); setLoadingMemoryUid(uid); setError("");
    try {
      const [memory, historyPage] = await Promise.all([getMemory(uid), getMemoryHistory(uid)]);
      setSelected(memory); setHistory(historyPage); setContent(JSON.stringify(memory.content, null, 2)); setEditing(false);
      const url = new URL(window.location.href); url.searchParams.set("memory", uid); window.history.replaceState(null, "", url);
    } catch (cause) { setError(formatApiError(cause, "知识详情加载失败")); }
    finally { setBusy(false); setLoadingMemoryUid(null); }
  };

  useEffect(() => {
    const uid = params.get("memory");
    if (uid) void openMemory(uid);
  // URL parameters are intentionally read only on page entry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSearch = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await searchMemories(source.trim(), query.trim());
      setResults(response);
      const url = new URL(window.location.href); url.searchParams.set("source", source.trim()); url.searchParams.set("query", query.trim()); window.history.replaceState(null, "", url);
    } catch (cause) { setError(formatApiError(cause, "知识搜索失败")); }
    finally { setBusy(false); }
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selected) return;
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(content) as Record<string, unknown>; }
    catch { setError("结构化内容不是有效 JSON。"); return; }
    setBusy(true); setError("");
    try {
      const result = await updateMemory(selected.uid, parsed, selected.record_version);
      setReprocessNotice(result.requires_reprocess
        ? "修正已保存，但不会改写当前 Meta 快照。请返回结构工作台，重新载入并提交 DDL 以应用新知识。"
        : "");
      await openMemory(selected.uid);
    }
    catch (cause) { setError(formatApiError(cause, "修正未保存，若版本已变化请重新打开详情")); setBusy(false); }
  };

  const remove = async () => {
    if (busy || !selected || !window.confirm(`软删除权威知识“${selected.memory_key}”（${selected.uid}）？它将不再参与后续召回。`)) return;
    setBusy(true); setError("");
    try {
      await deleteMemory(selected.uid, selected.record_version);
      setSelected(null); setHistory(null); setResults((current) => current ? { ...current, items: current.items.filter((item) => item.memory.uid !== selected.uid) } : current);
    } catch (cause) { setError(formatApiError(cause, "软删除失败")); }
    finally { setBusy(false); }
  };

  return (
    <div className="knowledge-page">
      <aside className="knowledge-search">
        <div className="panel-kicker">SOURCE / QUERY</div><h1>知识记忆</h1>
        <p>搜索已确认的业务口径，检查版本后再修正。</p>
        <form onSubmit={(event) => void handleSearch(event)}>
          <label htmlFor="memory-source">数据源</label><input id="memory-source" name="source" value={source} onChange={(event) => setSource(event.target.value)} maxLength={128} pattern="[\\w.-]+" autoComplete="off" spellCheck={false} required />
          <label htmlFor="memory-query">查询</label><textarea id="memory-query" name="query" rows={4} value={query} onChange={(event) => setQuery(event.target.value)} maxLength={2000} autoComplete="off" required />
          <button className="primary-action" type="submit" disabled={busy}>{busy ? "正在查询…" : "搜索知识 →"}</button>
        </form>
        {results?.degraded_targets.length ? <p className="warning">部分索引降级：{results.degraded_targets.join("、")}</p> : null}
        {error && <div className="error-summary" role="alert">{error}</div>}
        {reprocessNotice && <div className="warning" role="status" aria-live="polite">{reprocessNotice}</div>}
      </aside>

      <section className="memory-results" aria-labelledby="result-title">
        <header><div className="panel-kicker">SEARCH RESULTS</div><h2 id="result-title">权威记录</h2></header>
        {!results && <p className="empty-state">输入 source 和业务关键词开始搜索。</p>}
        {results?.items.length === 0 && <p className="empty-state">没有找到匹配知识。尝试更具体的指标、表或字段名。</p>}
        <ul>{results?.items.map((hit) => <li key={hit.memory.uid}><button type="button" aria-pressed={selected?.uid === hit.memory.uid} aria-busy={loadingMemoryUid === hit.memory.uid} disabled={busy} onClick={() => void openMemory(hit.memory.uid)}><span>{hit.memory.category} · v{hit.memory.record_version} · {scoreFormatter.format(hit.score)}</span><strong>{hit.memory.memory_text || hit.memory.memory_key}</strong><small>{hit.signals.join(" · ")}</small></button></li>)}</ul>
      </section>

      <section className="memory-detail" aria-labelledby="detail-title">
        <header><div className="panel-kicker">AUTHORITY / HISTORY</div><h2 id="detail-title">记录详情</h2></header>
        {!selected && <p className="empty-state">选择一条记录查看结构化内容和变更历史。</p>}
        {selected && <>
          <div className="detail-heading"><div><h3>{selected.memory_key}</h3><p>{selected.category} · v{selected.record_version} · {selected.status}</p></div><div><button className="quiet-action" type="button" disabled={busy} onClick={() => setEditing((value) => !value)}>{editing ? "取消修正" : "修正内容"}</button><button className="danger-action" type="button" disabled={busy} onClick={() => void remove()}>软删除</button></div></div>
          <p>{selected.memory_text}</p>
          {editing ? <form onSubmit={(event) => void save(event)}><label htmlFor="memory-content">结构化内容（JSON）</label><textarea id="memory-content" name="memory_content" className="json-editor" rows={12} value={content} onChange={(event) => setContent(event.target.value)} autoComplete="off" spellCheck={false} /><button className="primary-action" type="submit" disabled={busy}>保存修正</button></form> : <pre>{JSON.stringify(selected.content, null, 2)}</pre>}
          <h3>版本历史</h3><ol className="history-list">{history?.items.map((item, index) => <li key={`${item.created_at}-${index}`}><strong>{item.event_type}</strong><span>{item.actor_type}</span><time>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</time></li>)}</ol>
        </>}
      </section>
    </div>
  );
}
