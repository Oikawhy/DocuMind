/**
 * Chat page — conversational RAG interface with sessions, citations, and feedback.
 */

import { useState, useRef, useEffect, useMemo, useCallback } from "react";
import { Link } from "react-router-dom";
import { buildApiClient, ApiError } from "../api";
import { useToast } from "../components/Toast";
import { Spinner } from "../components/Spinner";
import type { Citation, SessionSummary } from "../types";

interface DisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  confidence: number | null;
  route: string | null;
  feedback: number | null; // +1, -1, or null
}

function formatSessionDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export function Chat() {
  const { addToast } = useToast();
  const api = useMemo(() => buildApiClient(), []);

  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [chatDisabled, setChatDisabled] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Load sessions list
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const page = await api.listSessions();
      setSessions((page.items ?? []) as unknown as SessionSummary[]);
    } catch {
      // silently fail — sessions panel is optional
    } finally {
      setSessionsLoading(false);
    }
  }, [api]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Load a session's history
  async function loadSession(id: string) {
    setSessionId(id);
    setMessages([]);
    setLoading(true);
    try {
      const detail = await api.getSession(id);
      const msgs: DisplayMessage[] = detail.messages
        .filter((m) => m.role !== "system")
        .map((m) => ({
          id: m.id,
          role: m.role as "user" | "assistant",
          content: m.content,
          citations: [],
          confidence: null,
          route: null,
          feedback: null,
        }));
      setMessages(msgs);
    } catch (err) {
      if (err instanceof ApiError) {
        addToast(`Failed to load session: ${err.message}`, "error");
      }
    } finally {
      setLoading(false);
    }
  }

  function startNewChat() {
    setSessionId(null);
    setMessages([]);
    setChatDisabled(false);
  }

  async function deleteSession(id: string) {
    try {
      await api.deleteSession(id);
      addToast("Session deleted", "success");
      if (sessionId === id) startNewChat();
      loadSessions();
    } catch (err) {
      if (err instanceof ApiError) {
        addToast(`Delete failed: ${err.message}`, "error");
      }
    }
  }

  async function handleSend() {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: DisplayMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      citations: [],
      confidence: null,
      route: null,
      feedback: null,
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const res = await api.chat(text, sessionId ?? undefined);
      if (!sessionId) {
        setSessionId(res.session_id);
        loadSessions(); // refresh list with new session
      }
      const assistantMsg: DisplayMessage = {
        id: res.message_id,
        role: "assistant",
        content: res.answer,
        citations: res.citations,
        confidence: res.confidence,
        route: res.route,
        feedback: null,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      if (err instanceof ApiError && err.code === "CHAT_DISABLED") {
        setChatDisabled(true);
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content:
              "Chat is currently disabled. An administrator must enable it by setting DOCUMIND_CHAT_ENABLED=true.",
            citations: [],
            confidence: null,
            route: null,
            feedback: null,
          },
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content:
              err instanceof ApiError
                ? `Error: ${err.message}`
                : "Network error. Please try again.",
            citations: [],
            confidence: null,
            route: null,
            feedback: null,
          },
        ]);
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleFeedback(messageId: string, score: number) {
    try {
      await api.submitFeedback(messageId, score);
      setMessages((prev) =>
        prev.map((m) => (m.id === messageId ? { ...m, feedback: score } : m)),
      );
      addToast(score > 0 ? "Thanks for the feedback! 👍" : "Thanks for the feedback", "info");
    } catch {
      addToast("Failed to submit feedback", "error");
    }
  }

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Chat</h1>
        <p>Ask questions about your documents using AI-powered retrieval</p>
      </div>

      <div className="page-body">
        <div className="chat-layout">
          {/* Sessions sidebar */}
          <div className="sessions-panel">
            <div className="sessions-panel-header">
              <h3>Sessions</h3>
              <button
                className="btn btn-primary"
                style={{ padding: "4px 10px", fontSize: "0.75rem" }}
                onClick={startNewChat}
              >
                + New
              </button>
            </div>
            <div className="sessions-list">
              {sessionsLoading ? (
                <div style={{ textAlign: "center", padding: 16 }}>
                  <Spinner size={16} />
                </div>
              ) : sessions.length === 0 ? (
                <p
                  style={{
                    textAlign: "center",
                    padding: 16,
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                  }}
                >
                  No sessions yet
                </p>
              ) : (
                sessions.map((s) => (
                  <div
                    key={s.id}
                    className={`session-item ${sessionId === s.id ? "active" : ""}`}
                    onClick={() => loadSession(s.id)}
                  >
                    <div>
                      <div style={{ fontSize: "0.75rem" }}>
                        {formatSessionDate(s.created_at)}
                      </div>
                      <div style={{ fontSize: "0.6875rem", opacity: 0.7 }}>
                        {s.message_count} msgs
                      </div>
                    </div>
                    <button
                      className="session-delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteSession(s.id);
                      }}
                    >
                      ✕
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Chat main area */}
          <div className="chat-main">
            <div className="chat-messages" style={{ padding: "24px 24px 0" }}>
              {messages.length === 0 && (
                <div className="empty-state">
                  <div className="empty-icon">💬</div>
                  <h3>Start a conversation</h3>
                  <p>Ask a question about your documents and get cited answers.</p>
                </div>
              )}
              {messages.map((msg) => (
                <div key={msg.id} className={`chat-message ${msg.role} fade-in`}>
                  <div>{msg.content}</div>

                  {/* Citations */}
                  {msg.citations.length > 0 && (
                    <div className="citations-row">
                      {msg.citations.map((c, i) => (
                        <Link
                          key={c.chunk_id}
                          to={`/documents/${c.document_id}`}
                          className="citation-chip"
                        >
                          📄 [{i + 1}] v{c.version_number}
                          {c.excerpt ? `: ${c.excerpt.slice(0, 40)}…` : ""}
                        </Link>
                      ))}
                    </div>
                  )}

                  {/* Confidence + route */}
                  {msg.role === "assistant" && (msg.confidence !== null || msg.route) && (
                    <div
                      style={{
                        marginTop: 6,
                        fontSize: "0.6875rem",
                        color: "var(--text-muted)",
                      }}
                    >
                      {msg.confidence !== null && (
                        <span>Confidence: {(msg.confidence * 100).toFixed(0)}%</span>
                      )}
                      {msg.route && <span style={{ marginLeft: 8 }}>Route: {msg.route}</span>}
                    </div>
                  )}

                  {/* Feedback */}
                  {msg.role === "assistant" && (
                    <div className="feedback-row">
                      <button
                        className={`feedback-btn ${msg.feedback === 1 ? "active" : ""}`}
                        onClick={() => handleFeedback(msg.id, 1)}
                        title="Helpful"
                      >
                        👍
                      </button>
                      <button
                        className={`feedback-btn ${msg.feedback === -1 ? "active" : ""}`}
                        onClick={() => handleFeedback(msg.id, -1)}
                        title="Not helpful"
                      >
                        👎
                      </button>
                    </div>
                  )}
                </div>
              ))}
              {loading && (
                <div className="chat-message assistant fade-in">
                  <Spinner size={16} />
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            <div className="chat-input-area" style={{ padding: "16px 24px" }}>
              <div className="chat-input-wrapper">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={
                    chatDisabled
                      ? "Chat is disabled by administrator"
                      : "Ask about your documents…"
                  }
                  rows={1}
                  disabled={chatDisabled}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSend();
                    }
                  }}
                />
                <button
                  className="btn btn-primary"
                  onClick={handleSend}
                  disabled={loading || !input.trim() || chatDisabled}
                >
                  Send
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
