import { NavLink, Outlet, useLocation } from 'react-router-dom'
import logo from './assets/dsg_white.svg'

const TITLES: [prefix: string, title: string][] = [
  ['/consents', 'Consents'],
  ['/sessions', 'Calls'],
  ['/purposes', 'Purposes & notices'],
]

function pageTitle(pathname: string): string {
  return TITLES.find(([prefix]) => pathname.startsWith(prefix))?.[1] ?? 'Overview'
}

export default function App() {
  const { pathname } = useLocation()
  return (
    <div className="app">
      <aside className="sidebar">
        <img className="brand-logo" src={logo} alt="datasafeguard" width={316} height={54} />
        <p className="brand-sub">ID-PRIVACY&reg; &middot; IVR</p>
        <p className="nav-label">Consent capture</p>
        <nav className="nav">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/consents">Consents</NavLink>
          <NavLink to="/sessions">Calls</NavLink>
          <NavLink to="/purposes">Purposes &amp; notices</NavLink>
        </nav>
        <div className="side-foot">IVR consent capture<br />DPDP &middot; Exotel &amp; Twilio</div>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <div className="crumb">Consent console / <strong>{pageTitle(pathname)}</strong></div>
          <div className="env-pill"><span className="live-dot" />Read-only operator view</div>
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
