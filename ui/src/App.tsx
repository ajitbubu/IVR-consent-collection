import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import logo from './assets/dsg_white.svg'
import shield from './assets/shield.svg'

// Navigation and header follow the PMP shell (datasafeguard downstream page).
// Only the IVR Consent pages live in this app; the other PMP modules are shown
// for continuity and stay disabled until their URLs are wired in.

const ICONS: Record<string, string> = {
  UCM: 'M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z',
  PIA: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
  CDD: 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4',
  DSAR: 'M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4',
  CDR: 'M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1',
  DPM: 'M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z',
  'Compliance Audit': 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z',
  Organization: 'M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4',
  Settings: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.573-1.066zM15 12a3 3 0 11-6 0 3 3 0 016 0z',
}

const PMP_UCM = ['Dashboard', 'Summary', 'Configuration', 'DCB Console', 'Downstream System']

const IVR_PAGES: [to: string, label: string][] = [
  ['/', 'Overview'],
  ['/consents', 'Consents'],
  ['/sessions', 'Calls'],
  ['/purposes', 'Purposes & notices'],
]

const PMP_MODULES: [label: string, children: string[]][] = [
  ['PIA', ['Assessments', 'Templates', 'Reports']],
  ['CDD', ['Data Discovery', 'Classification', 'Data Map']],
  ['DSAR', ['Requests', 'Workflows', 'Portal Settings']],
  ['CDR', ['Records', 'Retention Policies', 'Disposal Log']],
  ['DPM', ['Policies', 'Notices', 'Consent Banners']],
  ['Compliance Audit', ['Audit Trails', 'Frameworks', 'Gap Analysis']],
  ['Organization', []],
  ['Settings', ['General', 'Users & Roles', 'Integrations', 'API Keys']],
]

function pageTitle(pathname: string): string {
  const match = IVR_PAGES.filter(([to]) => to !== '/').find(([to]) => pathname.startsWith(to))
  return `IVR Consent — ${match ? match[1] : 'Overview'}`
}

function Icon({ name }: { name: string }) {
  return (
    <span className="menu-icon">
      {ICONS[name] && <svg viewBox="0 0 24 24" aria-hidden="true"><path d={ICONS[name]} /></svg>}
    </span>
  )
}

function Module({ label, children, open, onToggle }: {
  label: string; children: string[]; open: boolean; onToggle: () => void
}) {
  if (children.length === 0) {
    return (
      <button className="sidebar-menu-btn" disabled title="Lives in PMP">
        <Icon name={label} /><span className="menu-label">{label}</span>
      </button>
    )
  }
  return (
    <>
      <button className="sidebar-menu-btn" aria-expanded={open} onClick={onToggle}>
        <Icon name={label} /><span className="menu-label">{label}</span>
        <span className={`menu-chevron${open ? ' open' : ''}`}>▾</span>
      </button>
      <div className={`sidebar-submenu${open ? ' open' : ''}`}>
        {children.map((c) => (
          <button key={c} className="sidebar-menu-btn" disabled title="Lives in PMP">
            <span className="menu-icon" /><span className="menu-label">{c}</span>
          </button>
        ))}
      </div>
    </>
  )
}

export default function App() {
  const { pathname } = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [open, setOpen] = useState<Record<string, boolean>>({ UCM: true })
  const toggle = (k: string) => setOpen((o) => ({ ...o, [k]: !o[k] }))

  function toggleSidebar() {
    if (window.matchMedia('(max-width: 768px)').matches) setMobileOpen((v) => !v)
    else setCollapsed((v) => !v)
  }

  return (
    <div className={`app-layout${collapsed ? ' sidebar-collapsed' : ''}`}>
      <nav className={`sidebar${collapsed ? ' collapsed' : ''}${mobileOpen ? ' mobile-open' : ''}`} aria-label="Main">
        <div className="sidebar-logo">
          <img className="sidebar-logo-img" src={logo} alt="datasafeguard" width={316} height={54} />
          <span className="sidebar-logo-collapsed"><img src={shield} alt="datasafeguard" /></span>
        </div>
        <div className="sidebar-nav">
          <div className="sidebar-section">
            <button className="sidebar-menu-btn active" aria-expanded={!!open.UCM} onClick={() => toggle('UCM')}>
              <Icon name="UCM" /><span className="menu-label">UCM</span>
              <span className={`menu-chevron${open.UCM ? ' open' : ''}`}>▾</span>
            </button>
            <div className={`sidebar-submenu${open.UCM ? ' open' : ''}`}>
              {PMP_UCM.map((c) => (
                <button key={c} className="sidebar-menu-btn" disabled title="Lives in PMP">
                  <span className="menu-icon" /><span className="menu-label">{c}</span>
                </button>
              ))}
              <div className="sidebar-group-label">IVR Consent</div>
              {IVR_PAGES.map(([to, label]) => (
                <NavLink key={to} to={to} end={to === '/'} className="sidebar-menu-btn"
                         onClick={() => setMobileOpen(false)}>
                  <span className="menu-icon" /><span className="menu-label">{label}</span>
                </NavLink>
              ))}
            </div>
          </div>
          <div className="sidebar-section">
            {PMP_MODULES.map(([label, children]) => (
              <Module key={label} label={label} children={children}
                      open={!!open[label]} onToggle={() => toggle(label)} />
            ))}
          </div>
        </div>
        <div className="sidebar-footer">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" />
            <path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z" />
          </svg>
          <span>Version 10<br />Powered by CCE&reg;</span>
        </div>
      </nav>
      <div className={`sidebar-overlay${mobileOpen ? ' show' : ''}`} onClick={() => setMobileOpen(false)} />

      <div className="main-wrapper">
        <header className="topbar">
          <button className="topbar-toggle" onClick={toggleSidebar} title="Toggle sidebar" aria-label="Toggle sidebar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                 strokeLinecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
          </button>
          <div className="topbar-title">{pageTitle(pathname)}</div>
          <div className="topbar-actions">
            <button className="topbar-bell" title="Notifications" aria-label="Notifications">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 01-3.46 0" /></svg>
            </button>
            <div className="topbar-avatar" title="Read-only operator">OP</div>
          </div>
        </header>
        <main className="main">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
