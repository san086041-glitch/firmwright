/** 界面语言切换（2026-10-10）：设置页"外观"和首次启动向导的标题栏各放一个 */
import { getLang, setLang, t, useLang, type Lang } from "../i18n";

const LANGS: [Lang, string][] = [["en", "English"], ["zh", "中文"]];

export function LangSwitch({ compact = false }: { compact?: boolean }) {
  const lang = useLang();
  return (
    <div className={`theme-seg ${compact ? "compact" : ""}`} role="radiogroup" aria-label={t("Language")}>
      {LANGS.map(([value, label]) => (
        <button key={value} role="radio" aria-checked={lang === value} className={lang === value ? "on" : ""}
                onClick={() => setLang(value)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** Espressif 文档链接：中文界面跳中文版（渲染时调用，切换语言后组件会重新渲染） */
export function docUrl(url: string): string {
  return getLang() === "zh" ? url.replace("/en/", "/zh_CN/") : url;
}
