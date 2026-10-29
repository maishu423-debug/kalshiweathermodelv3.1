"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import PaperTrading, { type Recommendation } from "./PaperTrading";


// ── Types ────────────────────────────────────────────────────────────────────

type Market = {
  ticker: string;
  label: string;
  title: string;
  status: string;
  yes_bid_cents: number | null;
  yes_ask_cents: number | null;
  no_bid_cents: number | null;
  no_ask_cents: number | null;
  last_price_cents: number | null;
  chance_percent: number | null;
  volume: number | null;
  open_interest: number | null;
  close_time: string | null;
  rules_primary: string | null;
  rules_secondary: string | null;
};

type ApiPayload = {
  series: {
    title: string;
    category: string;
    important_info: { title: string | null; message: string | null };
    settlement_sources: Array<{ name: string; url: string }>;
  };
  event: { title: string; subtitle: string; category: string };
  markets: Market[];
  selected_market: Market | null;
  orderbook: {
    yes: Array<{ price_cents: number | null; count: number | null }>;
    no:  Array<{ price_cents: number | null; count: number | null }>;
  };
};

type ModelResult = {
  error?:      string;
  T0?:         number;
  mean?:       number;
  std?:        number;
  mode?:       number;
  mode_prob?:  number;
  p10?:        number;
  p90?:        number;
  dist?:       Array<{ temp: number; prob: number }>;
  fetched_at?: string;
  fit_source?: string;
};

type MarketMomentum = {
  ticker:        string;
  label:         string;
  floor:         number | null;
  cap:           number | null;
  yes_ask_cents: number | null;
  yes_bid_cents: number | null;
  no_ask_cents:  number | null;
  no_bid_cents:  number | null;
  volume:        number | null;
  yes_velocity:  number;
  no_velocity:   number;
  is_dead:       boolean;
  model_prob:    number | null;
  rank:          number;
};

type ForecastHistoryRow = {
  timestamp:         string;
  current_temp_f:    number | null;
  model_forecast_f:  number | null;
};

type AlgorithmState = {
  forecast: {
    models:      Record<string, ModelResult>;
    ensemble:    { current_temp_f?: number; mean?: number; mode?: number; mode_prob?: number; p10?: number; p90?: number; n_models?: number; dist?: Array<{ temp: number; prob: number }> };
    last_run_at: string | null;
    running:     boolean;
    loop?:       { waiting_for_hfmetar?: { reason: string; since: string } | null } | null;
  };
  temp:           { current_f: number | null; daily_high_f: number | null };
  analysis:       MarketMomentum[];
  recommendation: Recommendation;
  time_to_cutoff: { hours: number; display: string };
  models_ready:   boolean;
};

// ── Formatters ───────────────────────────────────────────────────────────────

function todayEventTicker() {
  const d = new Date();
  const yy  = String(d.getFullYear()).slice(-2);
  const mon = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][d.getMonth()];
  const dd  = String(d.getDate()).padStart(2, "0");
  return `KXHIGHMIA-${yy}${mon}${dd}`;
}
const DEFAULT_MARKET = `${todayEventTicker()}-B94.5`;

function cents(value: number | null | undefined, fallback = "-") {
  if (value === null || value === undefined) return fallback;
  return `${value}¢`;
}

function percent(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  if (value < 1) return "<1%";
  return `${value}%`;
}

function pct(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function modelLabel(key: string) {
  const map: Record<string, string> = {
    accuweather_1h: "AccuWeather +1h",
    accuweather_2h: "AccuWeather +2h",
    accuweather_3h: "AccuWeather +3h",
    var_1h:         "VAR(1) +1h",
    var_2h:         "VAR(1) +2h",
    var_3h:         "VAR(1) +3h",
  };
  return map[key] ?? key;
}

// ── Probability Distribution Chart ───────────────────────────────────────────

// Lowest temperature a Kalshi bucket pays out on, e.g. "87° to 88°" is floor 87,
// "89° or above" is floor 88 (t > floor). The "or below" bucket has no lower end.
function kalshiBucketStart(b: MarketMomentum): number | null {
  if (b.floor == null) return null;
  return b.cap == null ? b.floor + 1 : b.floor;
}

function ProbDistRows({
  dist,
  ensemble,
  brackets,
}: {
  dist: Array<{ temp: number; prob: number }>;
  ensemble: { mean?: number; mode?: number; p10?: number; p90?: number };
  brackets: MarketMomentum[];
}) {
  if (!dist || dist.length === 0) return null;

  const total = dist.reduce((sum, d) => sum + d.prob, 0) || 1;
  const probAt = new Map(dist.map((d) => [d.temp, d.prob / total]));
  const lo = Math.min(...dist.map((d) => d.temp));
  const hi = Math.max(...dist.map((d) => d.temp));

  // Fixed 2°F buckets that start on the SAME parity as Kalshi's: when Kalshi's buckets start on an
  // even number (82–83, 84–85, ...) ours do too, and when they start on an odd number
  // (81–82, 83–84, ...) ours are odd. Without Kalshi data yet, fall back to even starts.
  const kalshiStart = brackets.map(kalshiBucketStart).find((v): v is number => v != null);
  const ourParity = kalshiStart == null ? 0 : ((kalshiStart % 2) + 2) % 2;
  const rows: Array<{ from: number; to: number; prob: number; atLeast: number; atMost: number }> = [];
  let cumulative = 0;
  for (let from = lo - ((((lo - ourParity) % 2) + 2) % 2); from <= hi; from += 2) {
    const prob = (probAt.get(from) ?? 0) + (probAt.get(from + 1) ?? 0);
    rows.push({ from, to: from + 1, prob, atLeast: 1 - cumulative, atMost: cumulative + prob });
    cumulative += prob;
  }

  const visible = rows.filter((r) => r.prob >= 0.001);
  const maxProb = Math.max(...visible.map((r) => r.prob), 0.0001);
  const tier = (p: number) => (p >= 0.6 ? "high" : p >= 0.3 ? "mid" : "low");

  return (
    <div className="dist-rows-wrap">
      <h3 className="algo-section-title">
        Probability Distribution
        <span className="algo-hint-inline"> — ensemble daily high forecast</span>
      </h3>

      <table className="dist-rows-table">
        <thead>
          <tr>
            <th>Range</th>
            <th>Probability</th>
            <th></th>
            <th>P(≥ low)</th>
            <th>P(≤ high)</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((r) => (
            <tr key={r.from} className={`dist-row dist-row--${tier(r.prob)}`}>
              <td className="dist-row-label">P({r.from}–{r.to}°F)</td>
              <td className="dist-row-pct">{(r.prob * 100).toFixed(1)}%</td>
              <td className="dist-row-bar">
                <span style={{ width: `${(r.prob / maxProb) * 100}%` }} />
              </td>
              <td>{(r.atLeast * 100).toFixed(1)}%</td>
              <td>{(r.atMost * 100).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="dist-legend">
        <span className="dist-stat">mode <strong>{ensemble.mode}°F</strong></span>
        <span className="dist-stat">mean <strong>{ensemble.mean?.toFixed(1)}°F</strong></span>
        <span className="dist-stat">80% range <strong>{ensemble.p10}–{ensemble.p90}°F</strong></span>
      </div>
    </div>
  );
}

function MiniModelDistChart({
  dist,
  mode,
  mean,
}: {
  dist?: Array<{ temp: number; prob: number }>;
  mode?: number;
  mean?: number;
}) {
  if (!dist || dist.length === 0) {
    return <div className="model-dist-empty">No distribution</div>;
  }

  const W = 300, H = 82;
  const ml = 24, mr = 10, mt = 8, mb = 20;
  const cw = W - ml - mr;
  const ch = H - mt - mb;
  const maxProb = Math.max(...dist.map((d) => d.prob), 0.0001);
  const gap = cw / dist.length;
  const barW = Math.max(2, gap * 0.68);
  const firstTemp = dist[0].temp;
  const lastTemp = dist[dist.length - 1].temp;
  const meanX = mean != null ? ml + (mean - firstTemp) * gap + gap / 2 : null;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="model-dist-svg" aria-hidden="true">
      <line x1={ml} y1={mt + ch} x2={W - mr} y2={mt + ch} stroke="#dedbd3" strokeWidth={1} />
      {dist.map((d, i) => {
        const x = ml + i * gap + (gap - barW) / 2;
        const h = Math.max(1, (d.prob / maxProb) * ch);
        const y = mt + ch - h;
        const isMode = d.temp === mode;
        return (
          <rect
            key={`${d.temp}-${i}`}
            x={x}
            y={y}
            width={barW}
            height={h}
            fill={isMode ? "#13936a" : "#8dd3be"}
            opacity={isMode ? 1 : 0.78}
            rx={1.6}
          />
        );
      })}
      {meanX != null && meanX >= ml && meanX <= W - mr && (
        <line x1={meanX} y1={mt} x2={meanX} y2={mt + ch} stroke="#756f66" strokeDasharray="3 2" strokeWidth={1.2} />
      )}
      <text x={ml} y={H - 5} textAnchor="middle" fontSize={9} fill="#89857d">{firstTemp}°</text>
      <text x={W - mr} y={H - 5} textAnchor="middle" fontSize={9} fill="#89857d">{lastTemp}°</text>
      <text x={ml - 4} y={mt + 5} textAnchor="end" fontSize={9} fill="#b8b3aa">{(maxProb * 100).toFixed(0)}%</text>
    </svg>
  );
}

// ── Component ────────────────────────────────────────────────────────────────

export default function Home() {
  const [selectedTicker, setSelectedTicker]     = useState(DEFAULT_MARKET);
  const [payload, setPayload]                   = useState<ApiPayload | null>(null);
  const [error, setError]                       = useState<string | null>(null);
  const [isLoading, setIsLoading]               = useState(true);
  const [lastUpdated, setLastUpdated]           = useState<Date | null>(null);
  const [streamStatus, setStreamStatus] = useState<string>("Connecting…");
  const streamLiveRef = useRef(false);

  // Algorithm state
  const [algoState, setAlgoState]               = useState<AlgorithmState | null>(null);
  const [algoError, setAlgoError]               = useState<string | null>(null);
  const [algoMessage, setAlgoMessage]           = useState<string | null>(null);
  const [isRefreshingForecast, setIsRefreshingForecast] = useState(false);
  const [isRefreshingTemp, setIsRefreshingTemp]         = useState(false);
  const [isDistSidebarOpen, setIsDistSidebarOpen] = useState(false);
  const [forecastHistory, setForecastHistory]   = useState<ForecastHistoryRow[]>([]);
  const lastHistoryForecastRunAtRef = useRef<string | null>(null);

  // Model enable/disable switches
  const ALL_MODELS = ["accuweather_1h", "accuweather_2h", "accuweather_3h", "var_1h", "var_2h", "var_3h"] as const;
  type ModelKey = typeof ALL_MODELS[number];
  const [enabledModels, setEnabledModels]       = useState<Record<ModelKey, boolean>>({
    accuweather_1h: true, accuweather_2h: true, accuweather_3h: true,
    var_1h: true,         var_2h: true,         var_3h: true,
  });
  const enabledModelsRef = useRef(enabledModels);
  enabledModelsRef.current = enabledModels;

  // ── Init: read market ticker from URL ──────────────────────────────────
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get("market_ticker");
    if (t) setSelectedTicker(t);
  }, []);

  // ── Market data polling (2 s) ──────────────────────────────────────────
  useEffect(() => {
    let ignore = false;
    async function load() {
      try {
        setError(null);
        const res = await fetch(
          `/api/kalshi/miami-temperature?market_ticker=${encodeURIComponent(selectedTicker)}`,
          { cache: "no-store" }
        );
        if (!res.ok) {
          let detail = "";
          try {
            const data = await res.json();
            detail = typeof data.error === "string" ? `: ${data.error}` : "";
          } catch {
            detail = "";
          }
          throw new Error(`API returned ${res.status}${detail}`);
        }
        const data = (await res.json()) as ApiPayload;
        if (!ignore) { setPayload(data); setIsLoading(false); setLastUpdated(new Date()); }
      } catch (err) {
        if (!ignore) { setError(err instanceof Error ? err.message : "Unable to load market data"); setIsLoading(false); }
      }
    }
    load();
    const timer = window.setInterval(load, 2000);
    return () => { ignore = true; window.clearInterval(timer); };
  }, [selectedTicker]);

  // ── WebSocket → SSE stream ────────────────────────────────────────────
  useEffect(() => {
    const controller = new AbortController();
    const markLive = () => {
      streamLiveRef.current = true;
      setStreamStatus("Live WebSocket");
    };

    const handleTicker = (update: {
        ticker: string;
        yes_bid_cents: number | null; yes_ask_cents: number | null;
        last_price_cents: number | null;
        volume: number | null; open_interest: number | null;
        yes_bid_size: number | null; yes_ask_size: number | null;
      }) => {
      markLive();
      setPayload((cur) => {
        if (!cur) return cur;
        const markets = cur.markets.map((m) => {
          if (m.ticker !== update.ticker) return m;
          const yesBid = update.yes_bid_cents    ?? m.yes_bid_cents;
          const yesAsk = update.yes_ask_cents    ?? m.yes_ask_cents;
          const last   = update.last_price_cents ?? m.last_price_cents;
          return {
            ...m,
            yes_bid_cents:    yesBid,
            yes_ask_cents:    yesAsk,
            no_ask_cents:     yesBid === null ? m.no_ask_cents : 100 - yesBid,
            no_bid_cents:     yesAsk === null ? m.no_bid_cents : 100 - yesAsk,
            last_price_cents: last,
            chance_percent:   last ?? m.chance_percent,
            volume:           update.volume        ?? m.volume,
            open_interest:    update.open_interest ?? m.open_interest,
          };
        });
        const selected_market =
          markets.find((m) => m.ticker === cur.selected_market?.ticker) ??
          cur.selected_market;
        return { ...cur, markets, selected_market };
      });
      setLastUpdated(new Date());
    };

    const handleSseFrame = (frame: string) => {
      const lines = frame.split("\n");
      const eventName = lines
        .find((line) => line.startsWith("event:"))
        ?.slice("event:".length)
        .trim() || "message";
      const dataText = lines
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice("data:".length).trimStart())
        .join("\n");
      if (!dataText) return;

      const data = JSON.parse(dataText);
      if (eventName === "status") {
        data.connected ? markLive() : setStreamStatus(data.message);
      } else if (eventName === "ticker") {
        handleTicker(data);
      } else if (["orderbook_snapshot", "orderbook_delta", "trade"].includes(eventName)) {
        markLive();
      } else if (eventName === "auth_missing") {
        setStreamStatus(`Polling (no WS auth): ${data.message}`);
        controller.abort();
      } else if (eventName === "stream_error") {
        setStreamStatus(`WS error: ${data.message}`);
        controller.abort();
      } else if (eventName === "kalshi_error") {
        setStreamStatus(`Kalshi WS error: ${data.message || JSON.stringify(data)}`);
      }
    };

    async function readStream() {
      try {
        const res = await fetch("/api/kalshi/miami-temperature/stream", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`Stream returned ${res.status}`);
        if (!streamLiveRef.current) setStreamStatus("SSE connected; waiting for Kalshi WS");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            if (frame.trim() && !frame.startsWith(":")) handleSseFrame(frame);
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setStreamStatus(`WS disconnected — polling every 2s (${err instanceof Error ? err.message : "stream error"})`);
        }
      }
    }

    readStream();
    return () => controller.abort();
  }, []);

  // ── Algorithm state polling (5 s) ─────────────────────────────────────
  const loadAlgoState = useCallback(async () => {
    try {
      const active = Object.entries(enabledModelsRef.current)
        .filter(([, on]) => on).map(([k]) => k).join(",");
      const res = await fetch(
        `/api/kalshi/algorithm/state${active ? `?enabled_models=${encodeURIComponent(active)}` : ""}`,
        { cache: "no-store" }
      );
      if (!res.ok) {
        let detail = "";
        try {
          const data = await res.json();
          detail = typeof data.error === "string" ? `: ${data.error}` : "";
        } catch {
          detail = "";
        }
        throw new Error(`Algorithm API ${res.status}${detail}`);
      }
      setAlgoState((await res.json()) as AlgorithmState);
      setAlgoError(null);
    } catch (err) {
      setAlgoError(err instanceof Error ? err.message : "Algorithm unavailable.");
    }
  }, []);

  useEffect(() => {
    loadAlgoState();
    const timer = window.setInterval(loadAlgoState, 5_000);
    return () => window.clearInterval(timer);
  }, [loadAlgoState]);

  // ── Forecast history — reload when a model cycle completes ──
  const loadForecastHistory = useCallback(async () => {
    try {
      const res = await fetch("/api/kalshi/algorithm/forecast-history?limit=100", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      setForecastHistory((data.history ?? []) as ForecastHistoryRow[]);
    } catch {
      // silent — history panel just stays stale until the next successful poll
    }
  }, []);

  useEffect(() => {
    loadForecastHistory();
  }, [loadForecastHistory]);

  useEffect(() => {
    const lastRunAt = algoState?.forecast?.last_run_at ?? null;
    if (!lastRunAt || lastHistoryForecastRunAtRef.current === lastRunAt) return;
    lastHistoryForecastRunAtRef.current = lastRunAt;
    loadForecastHistory();
  }, [algoState?.forecast?.last_run_at, loadForecastHistory]);

  // ── Handlers ──────────────────────────────────────────────────────────
  function selectMarket(ticker: string) {
    setSelectedTicker(ticker);
    const url = new URL(window.location.href);
    url.searchParams.set("market_ticker", ticker);
    window.history.replaceState({}, "", url);
  }

  async function refreshTemp() {
    setIsRefreshingTemp(true);
    setAlgoMessage(null);
    try {
      const res = await fetch("/api/kalshi/algorithm/refresh-temp", { method: "POST", cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Temperature refresh failed (${res.status})`);
      await loadAlgoState();
      const observed = data.observed_at ? ` (${new Date(data.observed_at).toLocaleTimeString()})` : "";
      setAlgoMessage(`Temperature refreshed${observed}: now ${data.current_f}°F, high ${data.daily_high_f}°F.`);
    } catch (err) {
      setAlgoError(err instanceof Error ? err.message : "Temperature refresh failed.");
    } finally {
      setIsRefreshingTemp(false);
    }
  }

  async function refreshForecast() {
    setIsRefreshingForecast(true);
    setAlgoMessage("Forecast refresh started — all 6 models are running (~2–3 min). Page will update automatically.");
    try {
      await fetch("/api/kalshi/algorithm/refresh", { method: "POST" });
    } finally {
      setIsRefreshingForecast(false);
    }
    // Poll more frequently right after a refresh so the user sees progress
    let polls = 0;
    const fastPoll = window.setInterval(async () => {
      await loadAlgoState();
      polls++;
      if (polls >= 24) window.clearInterval(fastPoll); // stop after 2 min
    }, 5_000);
  }

  // ── Render ─────────────────────────────────────────────────────────────
  if (isLoading) return <main className="shell"><div className="status">Loading Miami market data...</div></main>;
  if (error || !payload) return <main className="shell"><div className="status error">Could not load data: {error}</div></main>;

  const timeLeft    = algoState?.time_to_cutoff;
  const ensemble    = algoState?.forecast?.ensemble;
  const modelsReady = algoState?.models_ready ?? false;
  const tempNow     = algoState?.temp?.current_f;
  const tempHigh    = algoState?.temp?.daily_high_f;

  return (
    <main className="shell">
      {/* ── Top bar ── */}
      <header className="topbar">
        <div className="brand">Kalshi</div>
        <nav>
          <span>Markets</span>
          <span>Live</span>
          <span>Research</span>
        </nav>
        <button>Sign up</button>
      </header>

      {/* ── Market header ── */}
      <section className="market-header">
        <div className="weather-badge">
          <span>MIA</span>
          <strong>☀</strong>
        </div>
        <div>
          <p className="eyebrow">{payload.event.category} · Daily Temperature</p>
          <h1>{payload.event.title}</h1>
          <p className="live-status">
            {streamStatus === "Live WebSocket" ? "Live WebSocket stream" : "Live refresh every 2s"}
            {lastUpdated ? ` / Updated ${lastUpdated.toLocaleTimeString()}` : ""}
          </p>
          <p className="stream-status">{streamStatus}</p>
        </div>
      </section>

      {/* ── Algorithm panel + forecast history ── */}
      <div className="algo-layout">
      <section className="algo-panel">
        {/* Header row */}
        <div className="algo-header">
          <div className="algo-title-row">
            <h2 className="algo-title">Algorithm</h2>
            {timeLeft && (
              <span className={`algo-timer ${timeLeft.hours < 1 ? "algo-timer--urgent" : ""}`}>
                {timeLeft.display}
              </span>
            )}
            {tempNow !== null && tempNow !== undefined && (
              <span className="algo-temp">Current Temp: {tempNow.toFixed(1)}°F</span>
            )}
            {tempHigh !== null && tempHigh !== undefined && (
              <span className="algo-temp algo-temp--high">Recorded High: {tempHigh}°F</span>
            )}
            <button
              className="algo-btn algo-btn--temp-refresh"
              onClick={refreshTemp}
              disabled={isRefreshingTemp}
              title="Fetch latest temperature from NWS ASOS"
            >
              {isRefreshingTemp ? "…" : "⟳"}
            </button>
            {modelsReady && ensemble?.mean !== undefined && (
              <span className="algo-direction algo-dir--flat">
                Model Forecast: ~{ensemble.mean.toFixed(1)}°F
              </span>
            )}
          </div>

          <div className="algo-controls">
            <button
              className="algo-btn algo-btn--refresh"
              onClick={refreshForecast}
              disabled={isRefreshingForecast || (algoState?.forecast?.running ?? false)}
            >
              {(algoState?.forecast?.running || isRefreshingForecast) ? "Running models…" : "Refresh Forecast"}
            </button>

            <button
              className="algo-btn algo-btn--dist"
              onClick={() => setIsDistSidebarOpen(true)}
              disabled={!algoState?.forecast?.models}
            >
              Distributions
            </button>
          </div>
        </div>

        {algoMessage && <p className="algo-message">{algoMessage}</p>}
        {algoError   && <p className="algo-error">{algoError}</p>}

        {/* Models row */}
        <div className="algo-body">
          {/* Model forecast cards */}
          <div className="algo-models">
            <h3 className="algo-section-title">Forecast Models</h3>
            {!modelsReady && !algoState?.forecast?.running && (
              <p className="algo-hint">No forecast yet — click <strong>Refresh Forecast</strong> to run all 6 models.</p>
            )}
            {algoState?.forecast?.loop?.waiting_for_hfmetar && (
              <p className="algo-running">
                ⏳ Waiting for HFMETAR since {new Date(algoState.forecast.loop.waiting_for_hfmetar.since + "Z").toLocaleTimeString()} — {algoState.forecast.loop.waiting_for_hfmetar.reason.replace("weather_unavailable: ", "")}.
                {" "}No other source is used; retrying every 30 s and the models run as soon as it returns.
              </p>
            )}
            {algoState?.forecast?.running && (
              <p className="algo-running">⏳ Models running… (~2–3 min). Page polls every 5 s.</p>
            )}
            <div className="model-grid">
              {ALL_MODELS.map((key) => {
                const m = algoState?.forecast?.models?.[key];
                const isOn = enabledModels[key];
                return (
                  <div key={key} className={`model-card ${m?.error ? "model-card--error" : ""} ${!isOn ? "model-card--disabled" : ""}`}>
                    <div className="model-name">
                      <label className="model-switch">
                        <input
                          type="checkbox"
                          checked={isOn}
                          onChange={(e) => {
                            setEnabledModels((prev) => ({ ...prev, [key]: e.target.checked }));
                          }}
                        />
                        {modelLabel(key)}
                      </label>
                    </div>
                    {!m ? (
                      <div className="model-error">No data yet</div>
                    ) : m.error ? (
                      <div className="model-error">{m.error.slice(0, 800)}</div>
                    ) : (
                      <>
                        <div className="model-main">
                          {m.mode != null ? `${m.mode}°F` : "—"}
                          <span className="model-sub"> mode</span>
                        </div>
                        <div className="model-meta">
                          <span>peak {m.mode_prob != null ? `${(m.mode_prob * 100).toFixed(1)}%` : "—"}</span>
                          <span>mean {m.mean?.toFixed(1)}°F</span>
                        </div>
                        <div className="model-ci">
                          <span className="ci-low">{m.p10?.toFixed(1)}</span>
                          <span className="ci-bar">──────</span>
                          <span className="ci-high">{m.p90?.toFixed(1)}</span>
                          <span className="ci-label">10–90th</span>
                        </div>
                      </>
                    )}
                  </div>
                );
              })}
              {ensemble && ensemble.n_models! > 0 && (
                <div className="model-card model-card--ensemble">
                  <div className="model-name">Daily High Forecast ({ensemble.n_models} models)</div>
                  <div className="model-main">
                    {ensemble.mode != null ? `${ensemble.mode}°F` : "—"}
                    <span className="model-sub"> mode</span>
                  </div>
                  <div className="model-meta">
                    <span>peak {ensemble.mode_prob != null ? `${(ensemble.mode_prob * 100).toFixed(1)}%` : "—"}</span>
                    <span>mean {ensemble.mean?.toFixed(1)}°F</span>
                  </div>
                  <div className="model-ci">
                    <span className="ci-low">{ensemble.p10}</span>
                    <span className="ci-bar">──────</span>
                    <span className="ci-high">{ensemble.p90}</span>
                    <span className="ci-label">10–90th</span>
                  </div>
                </div>
              )}
            </div>
            {algoState?.forecast?.last_run_at && (
              <p className="algo-hint">
                Last run: {new Date(algoState.forecast.last_run_at).toLocaleTimeString()}
              </p>
            )}
          </div>
        </div>

        {/* Probability distribution */}
        {ensemble?.dist && ensemble.dist.length > 0 && (
          <ProbDistRows dist={ensemble.dist} ensemble={ensemble} brackets={algoState?.analysis ?? []} />
        )}

        {/* Momentum table */}
        {algoState?.analysis && algoState.analysis.length > 0 && (
          <div className="ev-table-wrap">
            <h3 className="algo-section-title">
              Bracket Analysis
              <span className="algo-hint-inline"> — model probability of settlement in each bracket</span>
            </h3>
            <table className="ev-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Bracket</th>
                  <th>YES ask</th>
                  <th>YES bid</th>
                  <th>NO ask</th>
                  <th>YES ¢/min</th>
                  <th>NO ¢/min</th>
                  <th>Model Prob</th>
                </tr>
              </thead>
              <tbody>
                {algoState.analysis.map((row) => (
                  <tr
                    key={row.ticker}
                    className={`ev-row ${row.ticker === selectedTicker ? "ev-row--selected" : ""} ${row.is_dead ? "ev-row--dead" : ""}`}
                    onClick={() => selectMarket(row.ticker)}
                  >
                    <td>{row.is_dead ? "✕" : row.rank}</td>
                    <td>{row.label || row.ticker.split("-").pop()}</td>
                    <td>{cents(row.yes_ask_cents)}</td>
                    <td>{cents(row.yes_bid_cents)}</td>
                    <td>{cents(row.no_ask_cents)}</td>
                    <td className={row.yes_velocity > 0.2 ? "vel-rising" : row.yes_velocity < -0.2 ? "vel-falling" : ""}>
                      {row.yes_velocity > 0 ? `▲ ${row.yes_velocity.toFixed(2)}` : row.yes_velocity < 0 ? `▼ ${Math.abs(row.yes_velocity).toFixed(2)}` : "—"}
                    </td>
                    <td className={row.no_velocity > 0.2 ? "vel-rising" : row.no_velocity < -0.2 ? "vel-falling" : ""}>
                      {row.no_velocity > 0 ? `▲ ${row.no_velocity.toFixed(2)}` : row.no_velocity < -0.2 ? `▼ ${Math.abs(row.no_velocity).toFixed(2)}` : "—"}
                    </td>
                    <td className={!row.is_dead && row.model_prob != null && row.model_prob >= 0.15 ? "dir-ok" : ""}>
                      {row.is_dead ? "dead" : row.model_prob != null ? `${(row.model_prob * 100).toFixed(1)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Forecast history: current temp vs. model forecast every 5 min ── */}
      <aside className="forecast-history-panel">
        <h3 className="algo-section-title">
          Forecast History
          <span className="algo-hint-inline"> — new row every 5 min</span>
        </h3>
        {forecastHistory.length === 0 ? (
          <p className="algo-hint">No history yet — the first row lands after the next automatic forecast cycle.</p>
        ) : (
          <div className="forecast-history-scroll">
            <table className="forecast-history-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Current</th>
                  <th>Forecast</th>
                </tr>
              </thead>
              <tbody>
                {forecastHistory.map((row) => (
                  <tr key={row.timestamp}>
                    <td>{new Date(row.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td>
                    <td>{row.current_temp_f != null ? `${row.current_temp_f.toFixed(1)}°F` : "—"}</td>
                    <td>{row.model_forecast_f != null ? `${row.model_forecast_f.toFixed(1)}°F` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </aside>
      </div>

      {/* ── Paper trading bot + trade history (Google Sheet) ── */}
      <PaperTrading books={payload.markets} recommendation={algoState?.recommendation} />

      {/* ── Market list ── */}
      <section className="layout-grid">
        <div>
          <div className="market-table">
            <div className="table-head">
              <span></span>
              <span>Chance</span>
              <span></span>
            </div>
            {payload.markets.map((market) => {
              const momentum = algoState?.analysis?.find((a) => a.ticker === market.ticker);
              const hasProb  = momentum && momentum.model_prob != null && momentum.model_prob >= 0.15;
              return (
                <button
                  key={market.ticker}
                  className={`market-row ${market.ticker === selectedTicker ? "selected" : ""} ${momentum?.is_dead ? "market-row--dead" : ""}`}
                  onClick={() => selectMarket(market.ticker)}
                >
                  <span className="row-label">{market.label}</span>
                  <span className="chance">{percent(market.chance_percent)}</span>
                  <span className="price-actions">
                    <span className="yes">Yes {cents(market.yes_ask_cents)}</span>
                    <span className="no">No {cents(market.no_ask_cents)}</span>
                    {hasProb && (
                      <span className="ev-inline dir-ok">
                        {(momentum!.model_prob! * 100).toFixed(1)}%
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="info-banner">
            <strong>{payload.series.important_info.title || "Important information:"}</strong>
            <span>{payload.series.important_info.message}</span>
          </div>
        </div>
      </section>

      {isDistSidebarOpen && (
        <div className="dist-sidebar-layer" role="presentation">
          <button
            className="dist-sidebar-backdrop"
            aria-label="Close model distributions"
            onClick={() => setIsDistSidebarOpen(false)}
          />
          <aside className="dist-sidebar" aria-label="Model probability distributions">
            <div className="dist-sidebar-head">
              <div>
                <p className="eyebrow">Forecast Models</p>
                <h2>Probability Distributions</h2>
              </div>
              <button
                className="dist-sidebar-close"
                onClick={() => setIsDistSidebarOpen(false)}
                aria-label="Close model distributions"
              >
                ×
              </button>
            </div>

            <div className="model-dist-list">
              {ALL_MODELS.map((key) => {
                const m = algoState?.forecast?.models?.[key];
                return (
                  <section
                    key={key}
                    className={`model-dist-panel ${m?.error ? "model-dist-panel--error" : ""} ${!enabledModels[key] ? "model-dist-panel--disabled" : ""}`}
                  >
                    <div className="model-dist-panel-head">
                      <div>
                        <h3>{modelLabel(key)}</h3>
                        {!m ? (
                          <span>No data yet</span>
                        ) : m.error ? (
                          <span>Error</span>
                        ) : (
                          <span>
                            mode {m.mode}°F · peak {m.mode_prob != null ? `${(m.mode_prob * 100).toFixed(1)}%` : "—"} · mean {m.mean?.toFixed(1)}°F
                          </span>
                        )}
                      </div>
                      <strong>{m && !m.error && m.mode != null ? `${m.mode}°` : "—"}</strong>
                    </div>
                    {m?.error ? (
                      <p className="model-dist-error">{m.error.slice(0, 220)}</p>
                    ) : (
                      <MiniModelDistChart dist={m?.dist} mode={m?.mode} mean={m?.mean} />
                    )}
                  </section>
                );
              })}
            </div>
          </aside>
        </div>
      )}
    </main>
  );
}
