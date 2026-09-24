"use client";

import { useEffect, useRef, useState } from "react";
import type { XhsLiveStatus } from "@/lib/types";

const POLL_INTERVAL_MS = 500;
const FETCH_TIMEOUT_MS = 4000;
const DECODE_TIMEOUT_MS = 3000;

interface XhsLiveViewProps {
  live: XhsLiveStatus | null;
}

/** 候选帧先解码确认能显示，再决定要不要换掉当前帧（审查意见 1.4）：鉴权
 * 重定向到登录页时 `/api/xhs/screen` 会回 200 + HTML，坏 JPEG 也可能
 * 200；两种情况 blob 都建得出 object URL，但拿去当 `<img src>` 用会立刻
 * 渲染出一张破图，还会把上一张已经显示成功的帧的 URL 提前 revoke 掉。
 * 优先用 `HTMLImageElement.decode()`（返回 Promise，解码失败会 reject），
 * 不支持的浏览器退化到 load/error 事件。跟外层 fetch 一样兜一个超时：
 * `tick()` 的 `finally`（排下一次轮询）要等这个 Promise 落地才会跑，
 * `decode()`/`onload`/`onerror` 三个都不触发（浏览器解码卡住这种边缘
 * 情况，前端 smoke 里用真实 JPEG 连续轮询时偶发复现过）就会让整条轮询
 * 永久停摆——超时按失败处理，保证轮询总能继续。 */
function canDecodeImage(url: string): Promise<boolean> {
  const attempt = new Promise<boolean>((resolve) => {
    const img = new Image();
    if (typeof img.decode === "function") {
      img.src = url;
      img.decode().then(() => resolve(true)).catch(() => resolve(false));
      return;
    }
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
    img.src = url;
  });
  const timeout = new Promise<boolean>((resolve) => {
    setTimeout(() => resolve(false), DECODE_TIMEOUT_MS);
  });
  return Promise.race([attempt, timeout]);
}

/** S4：小红书实时观看——聊天页顶部小条 + 点开的手机比例浮层。自己管
 * "浮层开没开"这个本地状态，`ChatArea` 只按 `channel === "xiaoji"` 挂载
 * 一次，不往 `useChat` 里塞展开/收起这种纯 UI 状态。首屏/断线重连的 REST
 * 初始化在 `app/page.tsx` 里做（那边本来就常驻持有 `connectionState`，
 * 不会因为这个组件按频道换挂载/卸载而重置"上次是不是在线"的判断——
 * 2026-09-22 reuse 审查指出：原来放在这个组件里的话，用户切一下频道
 * 这个组件就卸载重挂一次，"刚重连"的判断跟着被误重置，多打一次没必要的
 * REST 请求）。 */
export function XhsLiveView({ live }: XhsLiveViewProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  // 浮层开着且正在直播才 500ms 拉一帧；关层或已经"放下手机"立即停。用
  // fetch+blob 而不是直接改 <img src>：新一帧请求失败（404/网络抖动/鉴权
  // 重定向回 200 HTML/坏 JPEG）时不能让浏览器把 <img> 显示区替换成一张
  // 破图，必须保留上一张已显示成功的画面（设计文档 S4："新请求失败不盖掉
  // 最后成功图"）。候选帧先解码确认真的能显示，再在"仍未取消"的前提下
  // 换图；解码失败只释放候选 URL，不动当前已经显示的帧（审查意见 1.4）。
  // 下一次请求排在上一次真正落地之后才发出（`setTimeout` 链而不是
  // `setInterval`），弱网时不会堆出一串还没返回的重叠请求；每次请求带
  // 超时并用 `AbortController` 取消，避免服务端卡住时整条轮询被堵死。
  useEffect(() => {
    if (!isOpen || !live?.active) return undefined;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    const tick = async () => {
      controller = new AbortController();
      const timeoutId = setTimeout(() => controller?.abort(), FETCH_TIMEOUT_MS);
      try {
        const res = await fetch("/api/xhs/screen", {
          credentials: "include",
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok || cancelled) return;
        const blob = await res.blob();
        if (cancelled) return;
        const candidateUrl = URL.createObjectURL(blob);
        const decodable = await canDecodeImage(candidateUrl);
        if (cancelled || !decodable) {
          URL.revokeObjectURL(candidateUrl);
          return;
        }
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = candidateUrl;
        setFrameSrc(candidateUrl);
      } catch {
        // 静默：保留上一帧，不产生错误 UI（含超时被 abort 的情形）。
      } finally {
        clearTimeout(timeoutId);
        if (!cancelled) timer = setTimeout(tick, POLL_INTERVAL_MS);
      }
    };

    tick();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer) clearTimeout(timer);
    };
  }, [isOpen, live?.active]);

  // 卸载时兜底释放最后一个 object URL，避免长会话里累积内存。
  useEffect(() => () => {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
  }, []);

  useEffect(() => {
    if (!isOpen) return undefined;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsOpen(false);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isOpen]);

  return (
    <>
      {!isOpen && live?.active && (
        <button
          type="button"
          onClick={() => setIsOpen(true)}
          className="glass mx-3 mt-2 flex min-h-11 touch-manipulation items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm active:scale-[0.99]"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-warm-accent animate-gentle-pulse" />
          <span className="text-xs text-warm-text-secondary">小机在刷小红书 👀</span>
        </button>
      )}

      {isOpen && (
        // 点空白背景关闭；标题栏和画面本身各自 stopPropagation，不会误触关闭。
        <div
          className="fixed inset-0 z-[110] flex flex-col bg-black/80 px-4 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-[max(1.5rem,calc(1rem_+_var(--sat)))]"
          onClick={() => setIsOpen(false)}
        >
          <div className="flex items-center justify-between" onClick={(e) => e.stopPropagation()}>
            <span className="text-sm text-white/85">
              {live?.active ? "小机在刷小红书" : "他放下手机了"}
            </span>
            <button
              type="button"
              onClick={() => setIsOpen(false)}
              aria-label="关闭"
              className="flex h-11 w-11 touch-manipulation items-center justify-center rounded-full text-xl text-white/85 active:bg-white/15"
            >
              ×
            </button>
          </div>
          <div
            className="mx-auto mt-4 flex min-h-0 w-full max-w-sm flex-1 items-center justify-center"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="relative aspect-[5/7] max-h-full w-full overflow-hidden rounded-2xl bg-black shadow-2xl">
              {frameSrc ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={frameSrc} alt="小机正在看的画面" className="h-full w-full object-contain" />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-sm text-white/50">
                  正在连接画面…
                </div>
              )}
              {!live?.active && (
                <div className="absolute inset-x-0 bottom-0 bg-black/60 px-3 py-2 text-center text-xs text-white/85">
                  他放下手机了
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
