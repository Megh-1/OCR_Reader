import { useState } from 'react';
import TemplateDesigner from './TemplateDesigner';
import ReviewQueue from './ReviewQueue';
import ProcessDocument from './ProcessDocument';
import './App.css';

export default function App() {
  const [view, setView] = useState('designer'); // 'designer' | 'process' | 'review'

  return (
    <div className="app">
      <nav className="top-nav">
        <button className={view === 'designer' ? 'active' : ''} onClick={() => setView('designer')}>
          Template Designer
        </button>
        <button className={view === 'process' ? 'active' : ''} onClick={() => setView('process')}>
          Process Document
        </button>
        <button className={view === 'review' ? 'active' : ''} onClick={() => setView('review')}>
          Review Queue
        </button>
      </nav>
      {view === 'designer' && <TemplateDesigner />}
      {view === 'process' && <ProcessDocument />}
      {view === 'review' && <ReviewQueue />}
    </div>
  );
}
