import { NavLink, Outlet } from 'react-router-dom'

export default function App() {
  return (
    <div className="app">
      <aside className="sidebar">
        <p className="brand">Consent Console</p>
        <p className="brand-sub">IVR capture &middot; DPDP</p>
        <nav className="nav">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/consents">Consents</NavLink>
          <NavLink to="/sessions">Calls</NavLink>
          <NavLink to="/purposes">Purposes &amp; notices</NavLink>
        </nav>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
