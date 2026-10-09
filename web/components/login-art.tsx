// The sign-in page's route map: the network's stations, pulsing, with aircraft flying the
// hub's routes. Each starts part-way along its route (a negative begin), so none waits at
// the map's corner before it sets off. Pure SVG and CSS, so it costs no script and respects reduced motion.

const STATIONS: Record<string, [number, number]> = {
  DEL: [168, 70],
  BOM: [92, 222],
  GOI: [104, 286],
  HYD: [188, 250],
  BLR: [176, 330],
  MAA: [222, 330],
  COK: [160, 398],
  CCU: [330, 168],
};

const ROUTES: [string, string, number][] = [
  ["BLR", "DEL", 9],
  ["BLR", "BOM", 7],
  ["BLR", "CCU", 11],
  ["BLR", "GOI", 6],
  ["DEL", "BOM", 8],
  ["BLR", "HYD", 6.5],
  ["MAA", "COK", 7.5],
];

function arc(from: [number, number], to: [number, number]) {
  const [x1, y1] = from;
  const [x2, y2] = to;
  const mx = (x1 + x2) / 2 + (y2 - y1) * 0.18;
  const my = (y1 + y2) / 2 - (x2 - x1) * 0.18;
  return `M${x1},${y1} Q${mx},${my} ${x2},${y2}`;
}

export function RouteMap() {
  return (
    <svg viewBox="0 0 400 460" className="h-full w-full" aria-hidden>
      <defs>
        <linearGradient id="route" x1="0" x2="1">
          <stop offset="0" stopColor="#7dd3fc" stopOpacity="0.15" />
          <stop offset="0.5" stopColor="#7dd3fc" stopOpacity="0.6" />
          <stop offset="1" stopColor="#7dd3fc" stopOpacity="0.15" />
        </linearGradient>
      </defs>
      {ROUTES.map(([a, b, seconds], i) => {
        const d = arc(STATIONS[a], STATIONS[b]);
        return (
          <g key={`${a}-${b}`}>
            <path
              d={d}
              fill="none"
              stroke="url(#route)"
              strokeWidth="1.4"
              strokeDasharray="4 6"
              className="route-dash"
            />
            <g className="motion-safe:block hidden">
              <path d="M-6,0 L5,-1.6 L7,0 L5,1.6 Z M-1,-1 L2,-6 L3.4,-6 L2,-1 Z M-1,1 L2,6 L3.4,6 L2,1 Z" fill="white">
                <animateMotion
                  dur={`${seconds}s`}
                  repeatCount="indefinite"
                  rotate="auto"
                  begin={`-${i * 0.9}s`}
                  path={d}
                />
              </path>
            </g>
          </g>
        );
      })}
      {Object.entries(STATIONS).map(([code, [x, y]]) => (
        <g key={code}>
          <circle
            cx={x}
            cy={y}
            r={code === "BLR" ? 9 : 6}
            className="station-pulse"
            fill="#38bdf8"
            fillOpacity="0.25"
          />
          <circle cx={x} cy={y} r={code === "BLR" ? 4.5 : 3} fill="white" />
          {/* The hub's label sits to its left, clear of MAA next to it. */}
          <text
            x={code === "BLR" ? x - 12 : x + 9}
            y={y + 4}
            textAnchor={code === "BLR" ? "end" : "start"}
            className="fill-sky-100 font-mono"
            fontSize="11"
          >
            {code}
            {code === "BLR" ? " · hub" : ""}
          </text>
        </g>
      ))}
    </svg>
  );
}
