import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getUserSession, setUserSession, userApi } from "./api";
import type { GatewayUser, ShopInfo } from "./types";

type AuthState = {
  me: GatewayUser | null | undefined;
  shop: ShopInfo | null;
  ready: boolean;
  setMe: (user: GatewayUser | null) => void;
  setShop: (shop: ShopInfo | null) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMeState] = useState<GatewayUser | null | undefined>(undefined);
  const [shop, setShop] = useState<ShopInfo | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const nextShop = await userApi.shop().catch(() => null);
    setShop(nextShop);
    if (!getUserSession()) {
      setMeState(null);
      return;
    }
    try {
      const user = await userApi.me();
      setMeState(user);
      if (user.shop) setShop(user.shop);
    } catch {
      setUserSession("");
      setMeState(null);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    refresh().finally(() => {
      if (!cancelled) setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  const setMe = useCallback((user: GatewayUser | null) => {
    setMeState(user);
    if (user?.shop) setShop(user.shop);
  }, []);

  const logout = useCallback(async () => {
    await userApi.logout().catch(() => undefined);
    setUserSession("");
    setMeState(null);
  }, []);

  const value = useMemo(
    () => ({ me, shop, ready, setMe, setShop, refresh, logout }),
    [me, shop, ready, setMe, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("AuthProvider missing");
  return ctx;
}
