import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Flex, Spin, theme } from "antd";
import {
  AppstoreOutlined,
  DashboardOutlined,
  FundOutlined,
  KeyOutlined,
  MessageOutlined,
  SettingOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import Home from "./pages/Home";
import Overview from "./pages/Overview";
import Nodes from "./pages/Nodes";
import NodeEditor from "./pages/NodeEditor";
import Playground from "./pages/Playground";
import Tokens from "./pages/Tokens";
import Usage from "./pages/Usage";
import Settings from "./pages/Settings";
import Users from "./pages/Users";
import ConsoleApp from "./pages/console/ConsoleApp";
import Login from "./pages/Login";
import { CommandPalette } from "./components/CommandPalette";
import { useAuth } from "./auth";
import { productNameOf } from "./types";
import { DashboardShell } from "./components/DashboardShell";

const NAV = [
  { key: "/overview", icon: <DashboardOutlined />, label: "概览" },
  { key: "/nodes", icon: <AppstoreOutlined />, label: "账号" },
  { key: "/tokens", icon: <KeyOutlined />, label: "令牌" },
  { key: "/users", icon: <TeamOutlined />, label: "用户与计价" },
  { key: "/usage", icon: <FundOutlined />, label: "用量" },
  { key: "/playground", icon: <MessageOutlined />, label: "调试" },
  { key: "/settings", icon: <SettingOutlined />, label: "设置" },
];

function selectedKey(pathname: string) {
  if (pathname.startsWith("/nodes")) return "/nodes";
  if (pathname.startsWith("/tokens")) return "/tokens";
  if (pathname.startsWith("/users")) return "/users";
  if (pathname.startsWith("/usage")) return "/usage";
  if (pathname.startsWith("/playground")) return "/playground";
  if (pathname.startsWith("/settings")) return "/settings";
  return "/overview";
}

function isMac() {
  return /mac|iphone|ipad/i.test(navigator.userAgent);
}

function isAuthPath(pathname: string) {
  return pathname === "/login" || pathname === "/register" || pathname === "/setup";
}

export default function App() {
  const loc = useLocation();
  const nav = useNavigate();
  const { token } = theme.useToken();
  const [cmd, setCmd] = useState(false);
  const { me, shop, ready, logout: authLogout } = useAuth();
  const brand = productNameOf(shop);
  const current = selectedKey(loc.pathname);
  const fill = loc.pathname.startsWith("/playground");
  const kbd = isMac() ? "⌘K" : "Ctrl K";
  const sectionLabel = NAV.find((item) => item.key === current)?.label || "管理台";

  useEffect(() => {
    if (me?.role !== "admin") return;
    if (loc.pathname === "/" || loc.pathname.startsWith("/console") || isAuthPath(loc.pathname)) return;
    document.title = `${sectionLabel} · ${brand}`;
  }, [me, loc.pathname, sectionLabel, brand]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmd(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function logout() {
    await authLogout();
    nav("/login", { replace: true });
  }

  if (loc.pathname === "/console/login" || loc.pathname === "/console/register") {
    return <Navigate to="/login" replace />;
  }

  if (!ready || me === undefined) {
    return (
      <Flex align="center" justify="center" style={{ minHeight: "100vh", background: token.colorBgLayout }}>
        <Spin />
      </Flex>
    );
  }

  if (loc.pathname === "/") {
    return <Home />;
  }

  if (me?.role === "admin") {
    if (isAuthPath(loc.pathname)) return <Navigate to="/overview" replace />;
  } else if (shop?.need_setup) {
    if (loc.pathname !== "/setup") return <Navigate to="/setup" replace />;
    return <Login mode="setup" />;
  } else if (isAuthPath(loc.pathname)) {
    if (me?.role === "user") return <Navigate to="/console" replace />;
    if (loc.pathname === "/register") return <Login mode="register" />;
    if (loc.pathname === "/setup") return <Navigate to="/login" replace />;
    return <Login mode="login" />;
  } else if (!me) {
    return <Navigate to="/login" replace />;
  }

  if (me.role !== "admin") {
    if (!loc.pathname.startsWith("/console")) return <Navigate to="/console" replace />;
    return <ConsoleApp />;
  }

  if (loc.pathname.startsWith("/console")) {
    return <ConsoleApp />;
  }

  return (
    <DashboardShell
      brand={brand}
      brandHint="ADMIN CONSOLE"
      nav={NAV}
      current={current}
      username={me.username}
      sectionLabel={sectionLabel}
      onLogout={logout}
      onSearch={() => setCmd(true)}
      searchHint={kbd}
      fill={fill}
    >
      {fill ? (
          <div style={{ height: "100%" }}>
            <Routes>
              <Route path="/playground" element={<Playground />} />
            </Routes>
          </div>
        ) : (
          <div className="page-container">
            <Routes>
              <Route path="/" element={<Navigate to="/overview" replace />} />
              <Route path="/overview" element={<Overview />} />
              <Route path="/nodes" element={<Nodes />} />
              <Route path="/nodes/new" element={<NodeEditor />} />
              <Route path="/nodes/:id" element={<NodeEditor />} />
              <Route path="/tokens" element={<Tokens />} />
              <Route path="/users" element={<Users />} />
              <Route path="/usage" element={<Usage />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </div>
        )}
      <CommandPalette open={cmd} onOpenChange={setCmd} />
    </DashboardShell>
  );
}
