import { t } from "../i18n";
import { useStore } from "../store";
import { Icon } from "./Icon";

/** 窄窗口时打开会话栏 / 设备栏的按钮；宽窗口时只有设备栏的显示 / 隐藏 */
export function PanelToggles({ inline = false }: { inline?: boolean }) {
  const devicesAvailable = useStore((s) => s.devicesAvailable);
  const devicesOpen = useStore((s) => s.devicesOpen);
  const boards = useStore((s) => s.boards);
  const crashed = Object.values(boards).some((b) => b.state === "crashed");
  const btns = (
    <>
      <button className="btn ghost sm icon-only only-narrow-sidebar" title={t("Sessions")} onClick={() => useStore.setState({ sidebarOpen: true })}>
        <Icon name="list" size={15} />
      </button>
      {devicesAvailable && (
        <button className={`btn ghost sm icon-only ${devicesOpen ? "on" : ""}`} title={devicesOpen ? t("Hide devices") : t("Show devices")}
                onClick={() => useStore.setState({ devicesOpen: !devicesOpen })} style={crashed ? { color: "var(--red)" } : undefined}>
          <Icon name="chip" size={15} />
        </button>
      )}
    </>
  );
  return inline ? btns : <div className="panel-toggles">{btns}</div>;
}
