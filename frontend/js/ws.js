// Gestione WebSocket per aggiornamenti real-time della dashboard job
const ORCHESTRATOR_WS = "ws://localhost:8000";

class JobSocket {
  constructor(jobId, onMessage) {
    this._url = `${ORCHESTRATOR_WS}/jobs/${jobId}/ws`;
    this._onMessage = onMessage;
    this._ws = null;
  }

  connect() {
    this._ws = new WebSocket(this._url);
    this._ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this._onMessage(data);
      } catch (_) {}
    };
    this._ws.onclose = () => setTimeout(() => this.connect(), 3000);
    this._ws.onerror = (e) => console.warn("WS error", e);
  }

  close() {
    if (this._ws) this._ws.close();
  }
}

window.JobSocket = JobSocket;
