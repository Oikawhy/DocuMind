/**
 * Operations page — document processing operations tracker.
 */

export function Operations() {
  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Operations</h1>
        <p>Track document processing pipelines and workflow status</p>
      </div>

      <div className="page-body">
        <div className="card-grid" style={{ marginBottom: 24 }}>
          <div className="stat-card">
            <div className="stat-label">Active Workflows</div>
            <div className="stat-value">—</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Completed Today</div>
            <div className="stat-value">—</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Failed</div>
            <div className="stat-value" style={{ color: "var(--danger)" }}>—</div>
          </div>
        </div>

        <div className="card">
          <table className="data-table">
            <thead>
              <tr>
                <th>Operation ID</th>
                <th>Document</th>
                <th>Stage</th>
                <th>Status</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td colSpan={5} style={{ textAlign: "center", padding: 48, color: "var(--text-secondary)" }}>
                  No active operations
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
