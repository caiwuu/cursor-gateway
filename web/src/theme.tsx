import { createContext, useContext, useMemo, useState, useEffect, type ReactNode } from "react";
import { theme, type ThemeConfig } from "antd";

const KEY = "cursor-gateway-theme";

type Mode = "light" | "dark";

const ThemeModeContext = createContext<{
  mode: Mode;
  toggle: () => void;
}>({ mode: "light", toggle: () => undefined });

export function useThemeMode() {
  return useContext(ThemeModeContext);
}

export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(() => {
    const saved = localStorage.getItem(KEY);
    return saved === "dark" ? "dark" : "light";
  });

  useEffect(() => {
    if (mode === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  }, [mode]);

  const value = useMemo(
    () => ({
      mode,
      toggle: () => {
        setMode((cur) => {
          const next = cur === "dark" ? "light" : "dark";
          localStorage.setItem(KEY, next);
          return next;
        });
      },
    }),
    [mode],
  );
  return <ThemeModeContext.Provider value={value}>{children}</ThemeModeContext.Provider>;
}

/** 一套适合数据密集型后台的柔和高对比主题。 */
export function buildAntdTheme(mode: Mode): ThemeConfig {
  const dark = mode === "dark";
  return {
    algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: "#0f8f83",
      colorInfo: "#168da2",
      colorSuccess: "#16866f",
      colorWarning: "#d97706",
      colorError: "#dc2626",
      colorLink: "#0b7f75",
      borderRadius: 10,
      borderRadiusLG: 16,
      controlHeight: 36,
      fontSize: 14,
      lineWidth: 1,
      colorBgLayout: dark ? "#08100f" : "#f3f7f6",
      colorBgContainer: dark ? "#111b1a" : "#ffffff",
      colorBorder: dark ? "#263331" : "#dce6e3",
      colorSplit: dark ? "#263331" : "#e4ece9",
      fontFamily:
        "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
      fontFamilyCode:
        "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Monaco, Consolas, monospace",
    },
    components: {
      Layout: {
        headerHeight: 68,
        headerBg: dark ? "#111b1a" : "#ffffff",
        headerPadding: "0 20px",
        bodyBg: dark ? "#08100f" : "#f3f7f6",
        siderBg: "#0b1717",
      },
      Menu: {
        itemBorderRadius: 6,
        itemMarginInline: 4,
        horizontalItemBorderRadius: 6,
      },
      Card: {
        borderRadiusLG: 16,
        paddingLG: 22,
      },
      Button: {
        borderRadius: 10,
        controlHeight: 36,
        primaryShadow: "none",
      },
      Input: {
        borderRadius: 10,
        controlHeight: 36,
      },
      Select: {
        borderRadius: 10,
        controlHeight: 36,
      },
      Table: {
        headerBorderRadius: 0,
        headerBg: dark ? "#15201f" : "#f7faf9",
        cellPaddingBlock: 12,
        cellPaddingInline: 16,
      },
      Tabs: {
        titleFontSize: 14,
      },
      Modal: {
        borderRadiusLG: 18,
      },
    },
  };
}
