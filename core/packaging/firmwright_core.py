"""发布版核心的入口（PyInstaller，W8）：和开发期的 `python -m firmwright.acp` 完全一样。"""

from firmwright.acp.server import main

if __name__ == "__main__":
    main()
