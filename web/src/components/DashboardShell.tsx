import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { Avatar, Button, Divider, Drawer, Flex, Grid, Tooltip, Typography } from "antd";
import {
  ApiOutlined,
  LogoutOutlined,
  MenuOutlined,
  MoonOutlined,
  SearchOutlined,
  SunOutlined,
} from "@ant-design/icons";
import { useThemeMode } from "../theme";

export type DashboardNavItem = {
  key: string;
  icon: ReactNode;
  label: string;
};

function initials(value: string) {
  return value.trim().slice(0, 1).toUpperCase() || "U";
}

export function DashboardShell({
  brand,
  brandHint,
  nav,
  current,
  username,
  sectionLabel,
  onLogout,
  onSearch,
  searchHint,
  fill = false,
  children,
}: {
  brand: string;
  brandHint: string;
  nav: DashboardNavItem[];
  current: string;
  username: string;
  sectionLabel: string;
  onLogout: () => void;
  onSearch?: () => void;
  searchHint?: string;
  fill?: boolean;
  children: ReactNode;
}) {
  const screens = Grid.useBreakpoint();
  const mobile = !screens.lg;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { mode, toggle } = useThemeMode();

  const navigation = (
    <>
      <div className="shell-brand">
        <span className="shell-brand-mark" aria-hidden="true">
          <ApiOutlined />
        </span>
        <div className="shell-brand-copy">
          <Typography.Text className="shell-brand-name">{brand}</Typography.Text>
          <Typography.Text className="shell-brand-hint">{brandHint}</Typography.Text>
        </div>
      </div>

      <div className="shell-nav-label">工作区</div>
      <nav className="shell-nav" aria-label="主导航">
        {nav.map((item) => (
          <NavLink
            key={item.key}
            to={item.key}
            onClick={() => setDrawerOpen(false)}
            className={`shell-nav-item${current === item.key ? " is-active" : ""}`}
          >
            <span className="shell-nav-icon">{item.icon}</span>
            <span>{item.label}</span>
            {current === item.key ? <span className="shell-nav-indicator" /> : null}
          </NavLink>
        ))}
      </nav>

      <div className="shell-sidebar-footer">
        <Divider className="shell-divider" />
        <div className="shell-user-card">
          <Avatar size={36} className="shell-avatar">
            {initials(username)}
          </Avatar>
          <div className="shell-user-copy">
            <Typography.Text className="shell-user-name" ellipsis>
              {username}
            </Typography.Text>
            <Typography.Text className="shell-user-role">已登录</Typography.Text>
          </div>
          <Tooltip title="退出登录" placement="top">
            <Button
              type="text"
              className="shell-icon-button"
              icon={<LogoutOutlined />}
              onClick={onLogout}
              aria-label="退出登录"
            />
          </Tooltip>
        </div>
      </div>
    </>
  );

  return (
    <div className="app-shell">
      {!mobile && <aside className="dashboard-sidebar">{navigation}</aside>}

      <div className="shell-main">
        <header className="shell-topbar">
          <Flex align="center" gap={12} style={{ minWidth: 0 }}>
            {mobile && (
              <Button
                type="text"
                className="topbar-menu-button"
                icon={<MenuOutlined />}
                onClick={() => setDrawerOpen(true)}
                aria-label="打开导航"
              />
            )}
            {mobile && <span className="mobile-brand-mark"><ApiOutlined /></span>}
            <div className="topbar-title-wrap">
              <Typography.Text className="topbar-eyebrow">{brand}</Typography.Text>
              <Typography.Text className="topbar-title" ellipsis>
                {sectionLabel}
              </Typography.Text>
            </div>
          </Flex>

          <Flex align="center" gap={8}>
            {onSearch && (
              <Button className="topbar-search" icon={<SearchOutlined />} onClick={onSearch}>
                <span className="topbar-search-label">搜索</span>
                {searchHint ? <kbd>{searchHint}</kbd> : null}
              </Button>
            )}
            <Tooltip title={mode === "dark" ? "切换浅色模式" : "切换深色模式"}>
              <Button
                className="topbar-action"
                icon={mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
                onClick={toggle}
                aria-label="切换主题"
              />
            </Tooltip>
            {mobile && (
              <Avatar size={34} className="topbar-avatar">
                {initials(username)}
              </Avatar>
            )}
          </Flex>
        </header>

        <main className={fill ? "shell-content is-fill" : "shell-content"}>{children}</main>
      </div>

      <Drawer
        placement="left"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={286}
        closable={false}
        styles={{ body: { padding: 0, background: "#0b1717" } }}
      >
        <aside className="dashboard-sidebar is-drawer">{navigation}</aside>
      </Drawer>
    </div>
  );
}
