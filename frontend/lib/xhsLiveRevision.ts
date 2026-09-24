/** S4 审查意见 1.5：WS 推送与 REST 首屏/断线重连初始化谁的 `xhs_live` 状态
 * 更新更"新"，用这个单调递增的版本号裁决，不比较 `Date.now()`——同一
 * 毫秒内 WS 和 REST 都可能发生，时间戳会打平手；REST 请求还可能乱序
 * 落地（旧请求比新请求晚回来），纯时间戳判断不出"这个响应是不是已经
 * 过时"。
 *
 * 规则：`bump()` 给 WS 事件用——事件一到就是最新，直接推进版本号并采纳；
 * `begin()` 给 REST 请求用——发起时占用一个新版本号；`isCurrent(token)`
 * 在响应落地时判断这个版本号是否还是"当前最新"（没被任何后来者——WS
 * 事件或更晚发起的 REST 请求——抢先推进过）。纯逻辑、不依赖 React 和
 * 浏览器 API，可以脱离组件直接单测（`scripts/verify-xhs-live-revision.mjs`）。
 */
export function createRevisionGate() {
  let revision = 0;
  return {
    bump(): void {
      revision += 1;
    },
    begin(): number {
      revision += 1;
      return revision;
    },
    isCurrent(token: number): boolean {
      return token === revision;
    },
  };
}

export type RevisionGate = ReturnType<typeof createRevisionGate>;
