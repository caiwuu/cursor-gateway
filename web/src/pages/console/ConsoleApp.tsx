import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import {
  AppstoreOutlined,
  DashboardOutlined,
  FundOutlined,
  GiftOutlined,
  KeyOutlined,
  MessageOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useAuth } from "../../auth";
import { productNameOf } from "../../types";
import { DashboardShell } from "../../components/DashboardShell";
import Home from "./Home";
import Keys from "./Keys";
import Redeem from "./Redeem";
import Usage from "./Usage";
import Models from "./Models";
import Play from "./Play";
import Profile from "./Profile";

const NAV = [
  { key: "/console", icon: <DashboardOutlined />, label: "概览" },
  { key: "/console/keys", icon: <KeyOutlined />, label: "令牌" },
  { key: "/console/redeem", icon: <GiftOutlined />, label: "兑换" },
  { key: "/console/usage", icon: <FundOutlined />, label: "用量" },
  { key: "/console/models", icon: <AppstoreOutlined />, label: "模型" },
  { key: "/console/play", icon: <MessageOutlined />, label: "调试" },
  { key: "/console/profile", icon: <UserOutlined />, label: "账户" },
];

function selectedKey(pathname: string) {
  const hit = [...NAV]
    .sort((a, b) => b.key.length - a.key.length)
    .find((x) => pathname === x.key || (x.key !== "/console" && pathname.startsWith(x.key)));
  return hit?.key || "/console";
}

export default function ConsoleApp() {
  const loc = useLocation();
  const nav = useNavigate();
  const { me, shop, logout: authLogout } = useAuth();
  const brand = productNameOf(shop);
  const current = selectedKey(loc.pathname);
  const sectionLabel = NAV.find((item) => item.key === current)?.label || "用户台";

  useEffect(() => {
    document.title = `${sectionLabel} · ${brand}`;
  }, [sectionLabel, brand]);

  async function logout() {
    await authLogout();
    nav("/login", { replace: true });
  }

  return (
    <DashboardShell
      brand={brand}
      brandHint="USER CONSOLE"
      nav={NAV}
      current={current}
      username={me?.username || ""}
      sectionLabel={sectionLabel}
      onLogout={logout}
    >
      <div className="page-container">
        <Routes>
          <Route path="/console" element={<Home />} />
          <Route path="/console/keys" element={<Keys />} />
          <Route path="/console/redeem" element={<Redeem />} />
          <Route path="/console/usage" element={<Usage />} />
          <Route path="/console/models" element={<Models />} />
          <Route path="/console/play" element={<Play />} />
          <Route path="/console/profile" element={<Profile />} />
          <Route path="/console/*" element={<Navigate to="/console" replace />} />
        </Routes>
      </div>
    </DashboardShell>
  );
}
