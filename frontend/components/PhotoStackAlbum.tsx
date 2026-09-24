"use client";

import { useEffect, useRef } from "react";
import PhotoStack from "@/vendor/photostack/photo-stack.js";
import "@/vendor/photostack/photo-stack.css";

// 跟库默认的 142:190（约 3:4）比例保持一致，宽度往现有多图气泡的
// max-w-[280px] 容器里对齐（S2 需求：只改尺寸，别的参数用它默认值）。
const STACK_WIDTH = 200;
const STACK_HEIGHT = 268;

/** 小机/用户任一方发 ≥2 张图时用的堆叠卡（微信同款效果），点当前那张
 * 走 `onTap(index)` 交给调用方打开现有的 `AttachmentLightbox`——多图堆叠
 * 本身不做预览，只负责"探边 + 跟手翻页"。1 张图不走这个组件（见
 * `MessageBubble.tsx::AttachmentBlock`）。 */
export function PhotoStackAlbum({ images, onTap }: { images: string[]; onTap: (index: number) => void }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  // onTap 每次渲染可能是新的函数引用（内联箭头函数很常见）；用 ref 转一手，
  // 避免因为这个把整个 PhotoStack 实例重建一遍（拖拽状态、当前页码都会丢）。
  const onTapRef = useRef(onTap);
  onTapRef.current = onTap;

  useEffect(() => {
    const el = containerRef.current;
    if (!el || images.length === 0) return undefined;
    const stack = new PhotoStack(el, images, {
      width: STACK_WIDTH,
      height: STACK_HEIGHT,
      onTap: (index) => onTapRef.current(index),
    });
    return () => stack.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 图片内容变化才重建，函数引用变化不重建（见上）
  }, [images.join("\u0000")]);

  if (images.length === 0) return null;
  return <div ref={containerRef} className="pstack-container" style={{ width: STACK_WIDTH, height: STACK_HEIGHT }} />;
}
