/**
 * Admin page — administration stub screens.
 */

export function Admin() {
  const sections = [
    { title: "Labels & Types", description: "Manage document labels and declared types", icon: "🏷️" },
    { title: "Role Mappings", description: "Map IdP groups to roles and label access", icon: "👥" },
    { title: "Chunk Profiles", description: "Configure chunking algorithms and parameters", icon: "📊" },
    { title: "Templates", description: "Manage extraction templates and proposals", icon: "📋" },
    { title: "Model Routes", description: "Configure LLM routes and BYOK consent", icon: "🤖" },
    { title: "Legal Holds", description: "Impose and release legal holds on documents", icon: "⚖️" },
    { title: "Dead Letters", description: "View and replay failed outbox events", icon: "📬" },
    { title: "Audit Log", description: "Browse hash-chained audit events", icon: "🔒" },
  ];

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Administration</h1>
        <p>Platform configuration and compliance management</p>
      </div>

      <div className="page-body">
        <div className="card-grid">
          {sections.map((section) => (
            <div key={section.title} className="card" style={{ cursor: "pointer", transition: "all var(--transition-fast)" }}>
              <div style={{ fontSize: "1.5rem", marginBottom: 12 }}>{section.icon}</div>
              <h3 style={{ fontSize: "0.9375rem", fontWeight: 600, marginBottom: 4 }}>{section.title}</h3>
              <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>{section.description}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
