import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import App from './App'
import Dashboard from './pages/Dashboard'
import Consents from './pages/Consents'
import ConsentDetail from './pages/ConsentDetail'
import Purposes from './pages/Purposes'
import Receipt from './pages/Receipt'
import Sessions from './pages/Sessions'
import './styles.css'

const router = createBrowserRouter(
  [
    {
      path: '/',
      element: <App />,
      children: [
        { index: true, element: <Dashboard /> },
        { path: 'consents', element: <Consents /> },
        { path: 'consents/:id', element: <ConsentDetail /> },
        { path: 'purposes', element: <Purposes /> },
        { path: 'sessions', element: <Sessions /> },
      ],
    },
    // Outside the console chrome: a receipt is meant to be printed or saved.
    { path: '/consents/:id/receipt', element: <Receipt /> },
  ],
  { basename: '/console' },
)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
)
