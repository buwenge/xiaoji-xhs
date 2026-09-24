"use client";

import { useCallback, useRef, useState } from "react";
import { createRevisionGate } from "@/lib/xhsLiveRevision";
import type { XhsLiveStatus } from "@/lib/types";

/** S4：小红书实时观看的状态与 REST 初始化，从 `useChat.ts` 搬出（审查意见
 * 1.5：施工令明确 case 只委托，不在大文件里堆状态）。WS 推送与 REST
 * 首屏/断线重连初始化谁的数据更新，用 `xhsLiveRevision` 的单调版本号
 * 裁决（见该文件注释），不比较 `Date.now()`。 */
export function useXhsLive() {
  const [xhsLive, setXhsLive] = useState<XhsLiveStatus | null>(null);
  const gateRef = useRef(createRevisionGate());

  // WS 收到 `xhs_live` 事件时调用：事件一到就是最新状态，直接采纳。
  const applyXhsLiveEvent = useCallback((event: { active: boolean; since: string | null }) => {
    gateRef.current.bump();
    // WS 推送不带 last_used_at（daemon 的 LiveStatusJob 只广播
    // active/since），UI 目前也没有地方读它——不假装"保留"一个从没被
    // WS 更新过的字段。
    setXhsLive({ active: event.active, since: event.since, last_used_at: null });
  }, []);

  // 首屏 / 断线重连用（`app/page.tsx` 在 `connectionState` 变回 "online"
  // 时调用一次）。响应落地时如果版本号已经被后来者（WS 事件或更晚发起的
  // 另一次 REST）推进过，就丢弃这次响应，不拿旧数据覆盖新状态。
  const refreshXhsLive = useCallback(async () => {
    const token = gateRef.current.begin();
    try {
      const res = await fetch("/api/xhs/live", { credentials: "include", cache: "no-store" });
      if (!res.ok) return;
      const data: XhsLiveStatus = await res.json();
      if (!gateRef.current.isCurrent(token)) return;
      setXhsLive(data);
    } catch {
      // 网络失败静默：小条/浮层不出现即可，不产生错误 UI（设计文档 S4）。
    }
  }, []);

  return { xhsLive, applyXhsLiveEvent, refreshXhsLive };
}
