import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "./store/authStore";
import Login from "./pages/Login";
import Register from "./pages/Register";
import ForgotPassword from "./pages/ForgotPassword";
import Downloads from "./pages/Downloads";
import Layout from "./components/Layout";
import ModelConfigGate from "./components/ModelConfigGate";
import { MODULES, type AppModule } from "./modules";

function PrivateRoute({ children, allowAdmin = false }: { children: React.ReactNode; allowAdmin?: boolean }) {
  const { isAuthenticated, user, initialized } = useAuthStore();
  if (!initialized) return null;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  // 管理员不使用求职/个人系统功能，统一引导到管理后台；交流中心全员可参与，故放行
  if (user?.role === "admin" && !allowAdmin) return <Navigate to="/admin" replace />;
  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, user, initialized } = useAuthStore();
  if (!initialized) return null;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (user?.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** 根据模块定义渲染页面 + 守卫 + 模型配置门禁。 */
function renderModule(mod: AppModule) {
  const Component = mod.component;
  const inner = (
    <Layout>
      {mod.requireModel ? (
        <ModelConfigGate>
          <Component />
        </ModelConfigGate>
      ) : (
        <Component />
      )}
    </Layout>
  );
  // 交流中心对管理员也开放（管理员同样参与社区分享）
  const allowAdmin = mod.key === "community";
  return mod.section === "admin" ? <AdminRoute>{inner}</AdminRoute> : <PrivateRoute allowAdmin={allowAdmin}>{inner}</PrivateRoute>;
}

function App() {
  const { initialize } = useAuthStore();

  useEffect(() => {
    initialize();
  }, [initialize]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
        {/* 客户端下载页：公开访问，未登录也能拿安装包 */}
        <Route path="/download" element={<Downloads />} />
        {MODULES.map((mod) => (
          <Route key={mod.key} path={mod.path} element={renderModule(mod)} />
        ))}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
