import { useState } from 'react';
import TemplateDesigner from './TemplateDesigner';
import ReviewQueue from './ReviewQueue';
import './App.css';

export default function App() {
  const [view, setView] = useState('designer'); // 'designer' | 'review'

  return (
    <div className="app">
      <nav className="top-nav">
        <button className={view === 'designer' ? 'active' : ''} onClick={() => setView('designer')}>
          Template Designer
        </button>
        <button className={view === 'review' ? 'active' : ''} onClick={() => setView('review')}>
          Review Queue
        </button>
      </nav>
      {view === 'designer' ? <TemplateDesigner /> : <ReviewQueue />}
    </div>
  );
}
