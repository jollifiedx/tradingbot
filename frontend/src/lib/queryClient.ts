import { QueryClient } from "@tanstack/react-query";
import { AuthError } from "../api/client";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Retrying a 401/403 is pointless (the token isn't going to become
      // valid on its own) and just delays showing the login/not-authorized
      // screen. Other failures (network blips, 503 fail-closed) get a
      // couple of retries.
      retry: (failureCount, error) => {
        if (error instanceof AuthError) return false;
        return failureCount < 2;
      },
    },
  },
});
