import { useCallback, useEffect, useRef, useState } from "react";

import { KnowledgePage } from "./knowledge/KnowledgePage";
import { WorkbenchPage } from "./workbench/WorkbenchPage";

type View = "workbench" | "knowledge";

function currentView(): View {
  return window.location.pathname.startsWith("/knowledge") ? "knowledge" : "workbench";
}

export function App() {
  const [view, setView] = useState<View>(currentView);
  const [unsavedWorkbench, setUnsavedWorkbench] = useState(false);
  const [workbenchNavigationBlocked, setWorkbenchNavigationBlocked] = useState(false);
  const viewRef = useRef(view);
  const unsavedWorkbenchRef = useRef(unsavedWorkbench);
  const workbenchNavigationBlockedRef = useRef(workbenchNavigationBlocked);
  const workbenchPathRef = useRef(
    window.location.pathname.startsWith("/workbench") ? window.location.pathname : "/workbench",
  );

  viewRef.current = view;
  unsavedWorkbenchRef.current = unsavedWorkbench;
  workbenchNavigationBlockedRef.current = workbenchNavigationBlocked;

  const canLeaveWorkbench = useCallback(() => {
    if (workbenchNavigationBlockedRef.current) {
      window.alert("AI 回复仍在生成，请等待完成或失败后再离开工作台。");
      return false;
    }
    return !unsavedWorkbenchRef.current || window.confirm("当前 DDL 尚未提交，确定离开工作台？");
  }, []);

  useEffect(() => {
    const onPopState = () => {
      const next = currentView();
      if (
        next !== viewRef.current
        && viewRef.current === "workbench"
        && !canLeaveWorkbench()
      ) {
        window.history.pushState(null, "", workbenchPathRef.current);
        return;
      }
      setView(next);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [canLeaveWorkbench]);

  useEffect(() => {
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!unsavedWorkbench && !workbenchNavigationBlocked) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [unsavedWorkbench, workbenchNavigationBlocked]);

  const navigate = (next: View) => {
    if (
      next !== view
      && view === "workbench"
      && !canLeaveWorkbench()
    ) return;
    if (view === "workbench" && window.location.pathname.startsWith("/workbench")) {
      workbenchPathRef.current = window.location.pathname;
    }
    const path = next === "knowledge" ? "/knowledge" : workbenchPathRef.current;
    window.history.pushState(null, "", path);
    setView(next);
  };

  const handleUnsavedWorkbenchChange = (unsaved: boolean) => {
    if (window.location.pathname.startsWith("/workbench")) {
      workbenchPathRef.current = window.location.pathname;
    }
    setUnsavedWorkbench(unsaved);
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="app-header">
        <a className="brand" href="/workbench" onClick={(event) => { event.preventDefault(); navigate("workbench"); }}>
          <span className="brand-mark" aria-hidden="true">⌁</span>
          <span><strong>DATA AGENT</strong><small>Schema Loom</small></span>
        </a>
        <nav aria-label="主导航">
          <a href="/workbench" aria-current={view === "workbench" ? "page" : undefined} onClick={(event) => { event.preventDefault(); navigate("workbench"); }}>结构工作台</a>
          <a href="/knowledge" aria-current={view === "knowledge" ? "page" : undefined} onClick={(event) => { event.preventDefault(); navigate("knowledge"); }}>知识记忆</a>
        </nav>
      </header>
      <main id="main-content" tabIndex={-1}>{view === "knowledge" ? <KnowledgePage /> : <WorkbenchPage onUnsavedChange={handleUnsavedWorkbenchChange} onNavigationBlockChange={setWorkbenchNavigationBlocked} />}</main>
    </div>
  );
}
