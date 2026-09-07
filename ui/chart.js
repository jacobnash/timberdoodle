// No build step, no framework - same spirit as every other file in ui/.
// Extracted out of app.js (originally built for point-history charts on
// the equipment-tree page) so derivations.js can reuse it for test-case
// input-series charts without duplicating it.
// pad defaults to 30 (app.js's original 640x220 point-history chart); a
// smaller canvas (e.g. derivations.js's 220x70 test-case sparklines) needs
// a smaller pad passed explicitly or the plot area all but disappears.
function drawLineChart(canvas, history, pad = 30) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width - pad * 2;
  const h = canvas.height - pad * 2;

  const values = history.map((r) => r.value);
  const times = history.map((r) => r.ts);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const spanV = maxV - minV || 1;
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const spanT = maxT - minT || 1;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "currentColor";
  ctx.globalAlpha = 0.3;
  ctx.strokeRect(pad, pad, w, h);
  ctx.globalAlpha = 1;

  ctx.fillStyle = "currentColor";
  ctx.font = "11px sans-serif";
  ctx.fillText(maxV.toFixed(2), 2, pad + 4);
  ctx.fillText(minV.toFixed(2), 2, pad + h);

  // Reads the real --accent token instead of a second hardcoded hex, so a
  // theme change to style.css's token doesn't leave this one line stale.
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2b6cb0";
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  history.forEach((row, i) => {
    const x = pad + ((row.ts - minT) / spanT) * w;
    const y = pad + h - ((row.value - minV) / spanV) * h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}
