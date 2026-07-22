export function LoginPage() {
  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="logo">⬢</span>
          <h1>Andro-CD</h1>
        </div>
        <p className="muted">GitOps for AWS ECS</p>
        <a className="btn primary login-btn" href="/api/auth/login">
          Sign in with GitHub
        </a>
      </div>
    </div>
  );
}
