import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  hint?: string; // 选项下面的一行说明（模式的含义、板子被谁占用……）
  disabled?: boolean;
  badge?: string; // 右侧的小标签（"in use"、"vision"）
}

/** 自定义下拉框（2026-10-05：原生 <select> 的弹出列表是系统样式，深色主题下很突兀，也放不下说明文字）。
 *  弹层用 portal 挂到 body、按触发按钮的位置定位，不会被 overflow:hidden 的父元素裁掉；
 *  上下方向自动选（下面放不下就往上弹）。键盘：↑↓ 选择，Enter 确认，Esc 关闭。 */
export function Select<T extends string>({ value, options, onChange, title, variant = "bare", icon, placeholder, width,
                                           disabled, renderValue, className = "" }: {
  value: T;
  options: SelectOption<T>[];
  onChange: (v: T) => void;
  title?: string;
  variant?: "bare" | "field" | "pill";
  icon?: ReactNode;
  placeholder?: string;
  width?: number; // 弹层最小宽度
  disabled?: boolean;
  renderValue?: (o: SelectOption<T> | undefined) => ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; minWidth: number; maxHeight: number } | null>(null);
  const current = options.find((o) => o.value === value);

  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 8;
    const above = r.top - 8;
    const want = Math.min(360, options.length * 44 + 12);
    const up = below < want && above > below;
    const minWidth = Math.max(r.width, width ?? 0);
    const left = Math.min(r.left, window.innerWidth - minWidth - 8);
    setPos(up ? { left, bottom: window.innerHeight - r.top + 4, minWidth, maxHeight: Math.min(360, above) }
              : { left, top: r.bottom + 4, minWidth, maxHeight: Math.min(360, below) });
  }, [open, options.length, width]);

  useEffect(() => {
    if (!open) return;
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!btn.current?.contains(t) && !pop.current?.contains(t)) setOpen(false);
    };
    const onScroll = (e: Event) => { if (!pop.current?.contains(e.target as Node)) setOpen(false); };
    // 下拉框打开时 Esc 只关下拉框，不能传到页面上（新建会话页的 Esc = 取消整个页面）
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onEsc, true);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onScroll);
    document.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("keydown", onEsc, true);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onScroll);
      document.removeEventListener("scroll", onScroll, true);
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (o: SelectOption<T> | undefined) => {
    if (!o || o.disabled) return;
    setOpen(false);
    if (o.value !== value) onChange(o.value);
    btn.current?.focus();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    const step = (d: number) => {
      let i = active;
      for (let n = 0; n < options.length; n++) {
        i = (i + d + options.length) % options.length;
        if (!options[i].disabled) break;
      }
      setActive(i);
    };
    if (e.key === "ArrowDown") { e.preventDefault(); step(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); step(-1); }
    else if (e.key === "Enter") { e.preventDefault(); pick(options[active]); }
    else if (e.key === "Escape" || e.key === "Tab") setOpen(false);
  };

  return (
    <>
      <button ref={btn} type="button" className={`sel sel-${variant} ${open ? "open" : ""} ${className}`} title={title} disabled={disabled}
              onClick={(e) => { e.stopPropagation(); setOpen(!open); }} onKeyDown={onKey} aria-haspopup="listbox" aria-expanded={open}>
        {icon}
        <span className="sel-v">{renderValue ? renderValue(current) : current?.label ?? placeholder ?? ""}</span>
        <Icon name="chevron" size={12} className="sel-chev" />
      </button>
      {open && pos && createPortal(
        <div ref={pop} className="sel-pop" role="listbox" style={{ position: "fixed", left: pos.left, top: pos.top, bottom: pos.bottom,
                                                                    minWidth: pos.minWidth, maxHeight: pos.maxHeight }}
             onMouseDown={(e) => e.stopPropagation()}>
          {options.map((o, i) => (
            <div key={o.value} role="option" aria-selected={o.value === value} aria-disabled={o.disabled}
                 className={`sel-opt ${o.value === value ? "on" : ""} ${i === active ? "act" : ""} ${o.disabled ? "dis" : ""}`}
                 onMouseEnter={() => setActive(i)} onClick={() => pick(o)}>
              <span className="ck">{o.value === value && <Icon name="check" size={13} />}</span>
              <span className="tx">
                <span className="lb">{o.label}{o.badge && <span className="bd">{o.badge}</span>}</span>
                {o.hint && <span className="ht">{o.hint}</span>}
              </span>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
