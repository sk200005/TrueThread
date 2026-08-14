import React, { useState, useRef, useEffect } from 'react';
import { chatWithQuery } from '../api/client';
import './Chatbot.css';

export default function Chatbot({ jobId }) {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isLoading]);

  const handleSend = async (e) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);

    try {
      const { response } = await chatWithQuery(jobId, userMessage);
      setMessages(prev => [...prev, { role: 'ai', content: response }]);
    } catch (err) {
      setMessages(prev => [...prev, { role: 'ai', content: 'Error: ' + err.message, isError: true }]);
    } finally {
      setIsLoading(false);
    }
  };

  if (!isOpen) {
    return (
      <button className="chatbot-toggle btn btn-primary animate-fade-in" onClick={() => setIsOpen(true)}>
        <span className="chatbot-toggle-icon">💬</span> Ask Question
      </button>
    );
  }

  return (
    <div className="chatbot-panel glass-card animate-slide-in">
      <div className="chatbot-header">
        <h3>Research Assistant</h3>
        <button className="btn btn-ghost btn-sm chatbot-close" onClick={() => setIsOpen(false)}>×</button>
      </div>
      
      <div className="chatbot-messages">
        {messages.length === 0 && (
          <div className="chatbot-empty">
            Ask me anything about the sources in this report!
          </div>
        )}
        {messages.map((msg, idx) => (
          <div key={idx} className={`chatbot-message ${msg.role === 'user' ? 'message-user' : 'message-ai'} ${msg.isError ? 'message-error' : ''}`}>
            <div className="message-bubble">{msg.content}</div>
          </div>
        ))}
        {isLoading && (
          <div className="chatbot-message message-ai">
            <div className="message-bubble loading-bubble">
              <span className="dot"></span><span className="dot"></span><span className="dot"></span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <form className="chatbot-input-area" onSubmit={handleSend}>
        <input 
          type="text" 
          className="input" 
          placeholder="Type your question..." 
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={isLoading}
        />
        <button type="submit" className="btn btn-primary" disabled={isLoading || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
