import { useAuth } from "./auth/AuthProvider";
import { LoginScreen } from "./components/LoginScreen";
import { Dashboard } from "./components/Dashboard";
import { ConfirmDialogHost } from "./components/ConfirmDialogHost";

function App() {
  const { session, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-neutral-500">
        Loading…
      </div>
    );
  }

  return (
    <>
      {session ? <Dashboard /> : <LoginScreen />}
      <ConfirmDialogHost />
    </>
  );
}

export default App;
