"use client";

import { useEffect, useRef, useState } from "react";
import type { Attachment } from "@/lib/types";
import { copyText } from "@/lib/clipboard";

function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** 小红书"分享链接"气泡：标题 + 域名 + 复制/打开。复制走共用的
 * `copyText`（跟 `MessageBubble.tsx::BubbleCopyButton` 同一份，已经处理
 * `navigator.clipboard` 被拒绝时退化到 `execCommand("copy")`）；只有它也
 * 失败的极端情况（连 execCommand 都不支持）才落到"选中文字，你自己复制"
 * 这层最后兜底。单个 copyState 枚举，不用两个独立布尔——否则一次失败
 * 落入兜底态后，后面再点一次成功了，"已复制"和兜底文字会同时挂着。
 *
 * 选中兜底文字的动作放进 `useEffect`（keyed on copyState），不在
 * `handleCopy` 的 catch 里直接摸 `textRef.current`：那个 `<span>` 只有
 * `copyState === "manual"` 时才渲染，`setCopyState("manual")` 排进的是
 * *下一次*渲染，在 catch 执行的这一刻它还没挂载，`textRef.current` 必是
 * null，选中动作在真正生效前就已经在原地空转（2026-09-22 code-review
 * 指出）。挪到 effect 里，等 DOM 提交完（span 真的挂上了）再选中。 */
export function LinkCard({ attachment }: { attachment: Attachment }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "manual">("idle");
  const textRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (copyState !== "manual") return;
    const el = textRef.current;
    if (!el) return;
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }, [copyState]);

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const text = attachment.text || attachment.url;
    try {
      await copyText(text);
      setCopyState("copied");
      window.setTimeout(() => setCopyState((s) => (s === "copied" ? "idle" : s)), 1500);
    } catch {
      setCopyState("manual");
    }
  };

  return (
    <div
      className="max-w-[280px] rounded-xl bg-black/[0.04] px-3 py-2.5 dark:bg-white/[0.06]"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="truncate text-[13px] font-medium text-warm-text">{attachment.name || "分享链接"}</div>
      <div className="mt-0.5 truncate text-[11px] text-warm-text-secondary/60">{hostnameOf(attachment.url)}</div>
      {copyState === "manual" && (
        <span ref={textRef} className="mt-1.5 block select-all break-all text-[11px] leading-relaxed text-warm-text-secondary/70">
          {attachment.text || attachment.url}
        </span>
      )}
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={handleCopy}
          className="flex min-h-11 flex-1 touch-manipulation items-center justify-center rounded-lg bg-warm-accent/12 text-[12px] font-medium text-warm-accent transition-colors active:bg-warm-accent/20"
        >
          {copyState === "copied" ? "已复制" : "复制"}
        </button>
        <a
          href={attachment.url}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="flex min-h-11 flex-1 touch-manipulation items-center justify-center rounded-lg bg-black/[0.04] text-[12px] font-medium text-warm-text transition-colors active:bg-black/[0.08] dark:bg-white/[0.06]"
        >
          打开
        </a>
      </div>
    </div>
  );
}
