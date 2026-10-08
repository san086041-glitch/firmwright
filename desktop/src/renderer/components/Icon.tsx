/** 线条图标（24×24 视框、1.75 描边）。不引入图标库，只放界面用到的这些。 */
import type { CSSProperties } from "react";

const P: Record<string, string> = {
  plus: "M12 5v14M5 12h14",
  chevron: "M9 6l6 6-6 6",
  chevronDown: "M6 9l6 6 6-6",
  x: "M18 6L6 18M6 6l12 12",
  check: "M5 12.5l4.5 4.5L19 7",
  alert: "M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  info: "M12 16v-4M12 8h.01M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  terminal: "M4 17l6-6-6-6M12 19h8",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  chip: "M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM9 9h6v6H9z",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  hammer: "M14 7l3-3 3 3-3 3M17 10l-9 9a2 2 0 0 1-3-3l9-9M11 4l5 5",
  flame: "M12 22c4 0 7-3 7-7 0-3-2-6-4-8 0 3-2 4-3 4 0-3-1-6-4-9 0 4-3 6-3 11 0 5 3 9 7 9z",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  bug: "M8 6a4 4 0 0 1 8 0M6 10h12v4a6 6 0 0 1-12 0zM3 13h3M18 13h3M4 7l3 3M20 7l-3 3M5 20l2-3M19 20l-2-3M12 10v10",
  hand: "M8 13V5.5a1.5 1.5 0 0 1 3 0V11M11 10V4.5a1.5 1.5 0 0 1 3 0V11M14 10.5V6a1.5 1.5 0 0 1 3 0v8a7 7 0 0 1-7 7h-.5a6.5 6.5 0 0 1-5.4-2.9L2.6 16a1.5 1.5 0 0 1 2.5-1.7L8 17",
  target: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
  send: "M5 12h14M13 6l6 6-6 6",
  stop: "M7 7h10v10H7z",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
  brain: "M9.5 3a3 3 0 0 0-3 3 3 3 0 0 0-2.5 4.6A3 3 0 0 0 5 16a3 3 0 0 0 4.5 3.2V3.5M14.5 3a3 3 0 0 1 3 3 3 3 0 0 1 2.5 4.6A3 3 0 0 1 19 16a3 3 0 0 1-4.5 3.2V3.5",
  bot: "M12 8V4M8 4h8M5 8h14a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9a1 1 0 0 1 1-1zM9 13v1M15 13v1",
  usb: "M12 3v14M9 6l3-3 3 3M8 12l-3 3v2M16 10l3 3v2M12 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  play: "M7 4v16l13-8z",
  power: "M12 3v9M6.4 6.4a8 8 0 1 0 11.2 0",
  sparkles: "M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  cpu: "M6 6h12v12H6zM9 9h6v6H9zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  dot: "M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2z",
  merge: "M6 3v18M18 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 9a9 9 0 0 0 9 9",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  download: "M12 3v12M7 10l5 5 5-5M5 21h14",
  pause: "M8 5v14M16 5v14",
  eraser: "M7 21h10M5.5 13.5l7-7a2 2 0 0 1 2.8 0l3.2 3.2a2 2 0 0 1 0 2.8L12 19H8z",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  key: "M15 7a4 4 0 1 1-3.5 6L5 19.5V22H2v-3l6.5-6.5A4 4 0 0 1 15 7zM16.5 8.5h.01",
  // 2026-10-06：USB-UART 桥（板上另一颗转换芯片）
  bridge: "M3 12h4M17 12h4M7 8h10v8H7zM10 8V6M14 8V6M10 18v-2M14 18v-2",
};

export type IconName = keyof typeof P;

export function Icon({ name, size = 16, className, style, title }:
  { name: IconName; size?: number; className?: string; style?: CSSProperties; title?: string }) {
  return (
    <svg className={`icon ${className ?? ""}`} width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" style={style}
         aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <path d={P[name]} />
    </svg>
  );
}

/** 产品标志：芯片轮廓 + 字母 F 的锻打线条 */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <rect x="4" y="4" width="24" height="24" rx="7" fill="var(--accent)" />
      <path d="M12 22V10h9M12 16h7" stroke="var(--on-accent)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M2 11h2M2 16h2M2 21h2M28 11h2M28 16h2M28 21h2" stroke="var(--accent)" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
