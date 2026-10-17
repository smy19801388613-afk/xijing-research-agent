import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './styles.css';
import './design-system.css';
import './research-evidence.css';
import './brand.css';
import './memo-report.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
