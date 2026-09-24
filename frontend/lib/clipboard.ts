/** 复制文本到剪贴板：优先 `navigator.clipboard`，被移动端浏览器的权限
 * 策略拒绝时退化到旧式 `document.execCommand("copy")`（隐藏 textarea +
 * select）。原来在 `MessageBubble.tsx`（`BubbleCopyButton`）里，2026-09-22
 * `/simplify` reuse 审查发现 `LinkCard.tsx` 正要第三次抄一份更弱的版本
 * （只选中文字、不真的复制）——按"复制第二次就是抽共用的时机"抽到这里，
 * `MessageBubble.tsx`/`LinkCard.tsx` 都改成调这个。`MarkdownContent.tsx`
 * 还有一份行为略有差异的旧实现（clipboard 被拒绝时不退化），跟本次改动
 * 无关的旧代码不顺手扩大范围去动它。 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 某些移动端浏览器暴露 clipboard API，但会因权限策略拒绝；继续走旧式兜底。
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.cssText = "position:fixed;opacity:0;pointer-events:none";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}
