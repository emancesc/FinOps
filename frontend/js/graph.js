// Rendering Cytoscape.js per la vista grafo
// Richiede: <script src="https://unpkg.com/cytoscape/dist/cytoscape.min.js"></script>

let cy = null;
let currentView = "architectural"; // "architectural" | "tagging"

function initGraph(containerId) {
  cy = cytoscape({
    container: document.getElementById(containerId),
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          "font-size": 11,
          "text-valign": "bottom",
          "text-margin-y": 4,
          "background-color": "#2563eb",
          color: "#222",
          width: 36,
          height: 36,
        },
      },
      { selector: "node[type='BusinessUnit']", style: { "background-color": "#7c3aed" } },
      { selector: "node[type='CostCenter']", style: { "background-color": "#db2777" } },
      { selector: "node[type='Environment']", style: { "background-color": "#059669" } },
      { selector: "node[type='Customer']", style: { "background-color": "#d97706" } },
      { selector: "node[type='Application']", style: { "background-color": "#0891b2" } },
      { selector: "node[type='Tenant']", style: { "background-color": "#1a2540" } },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": "#9ca3af",
          "target-arrow-color": "#9ca3af",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(label)",
          "font-size": 9,
          color: "#6b7280",
        },
      },
    ],
    layout: { name: "cose", animate: false },
  });
  window.cy = cy; // usato da graph.html per il dettaglio del nodo selezionato
}

function loadGraphData(elements) {
  if (!cy) return;
  cy.elements().remove();
  cy.add(elements);
  cy.layout({ name: "cose", animate: true, animationDuration: 500 }).run();
}

function convertNeo4jToElements(neo4jData) {
  // /graph/group restituisce una lista di nodi Resource (senza relazioni)
  if (Array.isArray(neo4jData)) neo4jData = { nodes: neo4jData, relationships: [] };
  const nodes = (neo4jData.nodes || []).map((n) => ({
    data: { ...n, id: n.id || n.arn, label: n.resource_type || n.label || n.name || n.arn, type: n.type },
  }));
  const edges = (neo4jData.edges || neo4jData.relationships || []).map((r, i) => ({
    data: { id: `e${i}`, source: r.source, target: r.target, label: r.type },
  }));
  return [...nodes, ...edges];
}

function toggleView(view) {
  currentView = view;
  document.querySelectorAll(".view-toggle button").forEach((b) => b.classList.remove("active"));
  document.getElementById(`btn-view-${view}`)?.classList.add("active");
}

window.graphModule = { initGraph, loadGraphData, convertNeo4jToElements, toggleView };
