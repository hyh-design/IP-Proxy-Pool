const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, String(value));
  }
  return element;
}

function number(value, rate) {
  if (value === null || value === undefined) return "—";
  return rate ? `${(value * 100).toFixed(1)}%` : Number(value).toLocaleString("zh-CN");
}

export function renderLineChart(svg, points, { resolution, labels, reducedMotion }) {
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 900 460");
  svg.setAttribute("data-resolution", resolution);
  svg.dataset.reducedMotion = reducedMotion ? "true" : "false";
  if (!points.length) {
    const empty = svgElement("text", {
      x: 450,
      y: 220,
      "text-anchor": "middle",
      class: "chart-label",
    });
    empty.textContent = "当前范围暂无历史快照";
    svg.append(empty);
    return;
  }

  const width = 900;
  const left = 66;
  const right = 22;
  const top = 24;
  const bandHeight = 126;
  const gap = 14;
  const times = points.map((point) => new Date(point.observed_at).getTime());
  const minimumTime = Math.min(...times);
  const maximumTime = Math.max(...times);
  const timeSpan = Math.max(1, maximumTime - minimumTime);
  const step = resolution === "1h" ? 3_600_000 : 300_000;
  const x = (time) => left + ((time - minimumTime) / timeSpan) * (width - left - right);

  labels.forEach((definition, seriesIndex) => {
    const bandTop = top + seriesIndex * (bandHeight + gap);
    const values = points
      .map((point) => point[definition.key])
      .filter((value) => value !== null && value !== undefined)
      .map(Number);
    const maximum = definition.rate ? 1 : Math.max(1, ...values);
    const y = (value) => bandTop + bandHeight - (Number(value) / maximum) * bandHeight;

    const baseline = svgElement("line", {
      x1: left,
      x2: width - right,
      y1: bandTop + bandHeight,
      y2: bandTop + bandHeight,
      class: "chart-grid",
    });
    svg.append(baseline);
    const label = svgElement("text", { x: 0, y: bandTop + 13, class: "chart-label" });
    label.textContent = definition.label;
    svg.append(label);
    const maxLabel = svgElement("text", { x: left - 8, y: bandTop + 4, "text-anchor": "end", class: "chart-label" });
    maxLabel.textContent = number(maximum, definition.rate);
    svg.append(maxLabel);
    const zeroLabel = svgElement("text", { x: left - 8, y: bandTop + bandHeight, "text-anchor": "end", class: "chart-label" });
    zeroLabel.textContent = definition.rate ? "0%" : "0";
    svg.append(zeroLabel);

    let segment = [];
    const flush = () => {
      if (!segment.length) return;
      const path = svgElement("path", {
        d: segment.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`).join(" "),
        class: "chart-line",
        stroke: definition.color,
      });
      svg.append(path);
      segment = [];
    };

    points.forEach((point, index) => {
      const value = point[definition.key];
      if (value === null || value === undefined) {
        flush();
        return;
      }
      if (index > 0 && times[index] - times[index - 1] > step * 1.5) {
        flush();
        svg.append(svgElement("line", {
          x1: x(times[index - 1]) + 5,
          x2: x(times[index]) - 5,
          y1: bandTop + bandHeight / 2,
          y2: bandTop + bandHeight / 2,
          stroke: "#a86f16",
          "stroke-dasharray": "4 6",
          "data-gap": "true",
        }));
      }
      const px = x(times[index]);
      const py = y(value);
      segment.push([px, py]);
      const pointMarker = svgElement("circle", {
        cx: px,
        cy: py,
        r: 4,
        fill: definition.color,
        class: "chart-point",
        tabindex: 0,
        role: "img",
        "aria-label": `${definition.label} ${number(value, definition.rate)}，${new Date(times[index]).toLocaleString("zh-CN")}`,
      });
      const title = svgElement("title");
      title.textContent = `${definition.label}: ${number(value, definition.rate)} · ${new Date(times[index]).toLocaleString("zh-CN")}`;
      pointMarker.append(title);
      svg.append(pointMarker);
    });
    flush();
  });
}

export function renderDistribution(container, buckets, { title, labels, colors }) {
  container.replaceChildren();
  const heading = document.createElement("p");
  heading.className = "distribution-title";
  heading.textContent = title;
  container.append(heading);

  const entries = Object.entries(labels).map(([key, label]) => ({
    key,
    label,
    value: Number(buckets[key] || 0),
  }));
  const total = entries.reduce((sum, item) => sum + item.value, 0);
  const bar = document.createElement("div");
  bar.className = "distribution-bar";
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `${title}，合计 ${total}`);
  for (const item of entries) {
    const segment = document.createElement("span");
    segment.style.width = `${total ? (item.value / total) * 100 : 0}%`;
    segment.style.backgroundColor = colors[item.key];
    segment.title = `${item.label}: ${item.value}`;
    bar.append(segment);
  }
  container.append(bar);

  const legend = document.createElement("div");
  legend.className = "distribution-legend";
  for (const item of entries) {
    const row = document.createElement("div");
    row.className = "legend-item";
    const name = document.createElement("span");
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.backgroundColor = colors[item.key];
    name.append(swatch, document.createTextNode(item.label));
    const value = document.createElement("span");
    value.textContent = item.value.toLocaleString("zh-CN");
    row.append(name, value);
    legend.append(row);
  }
  container.append(legend);
}
