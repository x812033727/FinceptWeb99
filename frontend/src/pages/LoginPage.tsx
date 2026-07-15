import { useState, FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { login } from "@/lib/auth";

export default function LoginPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(email, password);
      navigate("/dashboard");
    } catch (err: any) {
      setError(err.response?.data?.detail ?? "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm bg-card border border-border rounded-lg p-8 space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-primary">Fincept Web Terminal</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Sign in to your invited account
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="login-email" className="block text-sm text-foreground mb-1">Email</label>
            <input
              id="login-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2 rounded-md bg-input border border-border text-foreground text-sm outline-none focus:ring-1 focus:ring-ring"
              placeholder="you@example.com"
            />
          </div>

          <div>
            <label htmlFor="login-password" className="block text-sm text-foreground mb-1">Password</label>
            <input
              id="login-password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2 rounded-md bg-input border border-border text-foreground text-sm outline-none focus:ring-1 focus:ring-ring"
              placeholder="••••••••"
            />
          </div>

          {error && <p className="text-negative text-sm">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 px-4 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
          >
            {loading ? "Please wait…" : "Sign in"}
          </button>
        </form>

        <div className="text-center text-sm space-y-2">
          <Link to="/forgot-password" className="text-primary hover:underline">Forgot password?</Link>
          <p className="text-muted-foreground">Accounts are available by administrator invitation only.</p>
        </div>
      </div>
    </div>
  );
}
