interface Props {
  mode?: "none" | "github" | "oidc";
}

export function LoginPage({ mode = "github" }: Props) {
  const oidc = mode === "oidc";
  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="logo">⬢</span>
          <h1>Andro-CD</h1>
        </div>
        <p className="muted">GitOps for AWS ECS</p>
        <a
          className="btn primary login-btn"
          href={oidc ? "/api/auth/oidc/login" : "/api/auth/login"}
        >
          {oidc ? "Sign in with SSO" : "Sign in with GitHub"}
        </a>
      </div>
    </div>
  );
}
