// The Christmas theme: snow behind the cards. Off with "reduce motion" (unless switched on in the user menu), paused
// in a hidden tab. The choice of the menu is kept in this browser only. Kept cheap: soft dots need no HiDPI canvas
// (a quarter of the memory), and 30 frames a second are enough for them.
(function () {
  "use strict";

  const KEY = "santa-snow";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let canvas = null;
  let context = null;
  let flakes = [];
  let frame = null;
  let last = 0;
  let color = "";

  function stored() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (error) {
      return null;
    }
  }

  function wanted() {
    const value = stored();
    return value ? value === "on" : !reduced.matches;
  }

  function remember(on) {
    try {
      window.localStorage.setItem(KEY, on ? "on" : "off");
    } catch (error) {
      // private window or blocked storage: the choice lasts until the next page
    }
  }

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    color = getComputedStyle(canvas).color;
    const count = Math.min(80, Math.round(window.innerWidth * window.innerHeight / 14000));
    while (flakes.length < count) flakes.push(newFlake(true));
    flakes.length = count;
  }

  function newFlake(anywhere) {
    return {
      x: Math.random() * window.innerWidth,
      y: anywhere ? Math.random() * window.innerHeight : -5,
      radius: 1 + Math.random() * 2.2,
      speed: 18 + Math.random() * 30,
      sway: Math.random() * Math.PI * 2,
    };
  }

  function draw(time) {
    frame = window.requestAnimationFrame(draw);
    if (last && time - last < 30) return;
    const seconds = Math.min(0.08, (time - (last || time)) / 1000);
    last = time;
    context.clearRect(0, 0, window.innerWidth, window.innerHeight);
    context.fillStyle = color;
    context.beginPath();
    flakes.forEach((flake, index) => {
      flake.y += flake.speed * seconds;
      flake.sway += seconds;
      flake.x += Math.sin(flake.sway) * 12 * seconds;
      if (flake.y > window.innerHeight + 5) flakes[index] = newFlake(false);
      context.moveTo(flake.x + flake.radius, flake.y);
      context.arc(flake.x, flake.y, flake.radius, 0, Math.PI * 2);
    });
    context.fill();
  }

  function start() {
    if (canvas) return;
    canvas = document.createElement("canvas");
    canvas.className = "snow";
    canvas.setAttribute("aria-hidden", "true");
    document.body.prepend(canvas);
    context = canvas.getContext("2d");
    resize();
    last = 0;
    frame = window.requestAnimationFrame(draw);
  }

  function stop() {
    if (!canvas) return;
    window.cancelAnimationFrame(frame);
    canvas.remove();
    canvas = null;
    flakes = [];
  }

  function update() {
    // another page loaded in place (hx-boost) replaced the body: the same snow goes on in it, where it was
    if (canvas && !canvas.isConnected) document.body.prepend(canvas);
    const on = wanted() && document.documentElement.dataset.season === "christmas";
    if (on && !document.hidden) start();
    else stop();
    document.querySelectorAll("[data-snow]").forEach((button) => {
      button.setAttribute("aria-pressed", (button.dataset.snow === "on") === wanted() ? "true" : "false");
    });
  }

  // On / Off in the user menu
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-snow]");
    if (!button) return;
    remember(button.dataset.snow === "on");
    update();
  });
  window.addEventListener("resize", () => { if (canvas) resize(); });
  const recolor = () => { if (canvas) color = getComputedStyle(canvas).color; };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", recolor);
  new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  document.addEventListener("visibilitychange", update);
  reduced.addEventListener("change", update);
  document.addEventListener("DOMContentLoaded", update);
  document.addEventListener("santa:page", update);
})();

// The string of lights: swags that hang in a "U" from the bottom edge of the top bar, small Verlet ropes that swing
// when the pointer sweeps through them. The loop sleeps while they are at rest; with "reduce motion" they hang still.
// On / Off in the user menu, kept in this browser only.
(function () {
  "use strict";

  const LIGHTS = "santa-lights";
  const POINTS = 13; // per swag, even: a bulb on every 2nd inner point, symmetric
  const SLACK = 1.04; // rope length / span: the depth of the "U"
  const SWAG = 6; // rem between two anchors
  const STEP = 1 / 60;
  const GRAVITY = 1400; // px/s²
  const DAMPING = 0.985;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let canvas = null;
  let context = null;
  let ropes = [];
  let colors = null;
  let frame = null;
  let last = 0;
  let rest = 0;
  let backlog = 0;
  let pointer = null;
  let choice = null;
  let fontSize = 16;

  // read when the garland is built, not on every frame
  function rem() {
    return fontSize;
  }

  // the variables use light-dark(): only the computed color of an element resolves them
  function resolve(value) {
    canvas.style.color = value;
    const color = getComputedStyle(canvas).color;
    canvas.style.color = "";
    return color;
  }

  function readColors() {
    colors = {
      wire: resolve("var(--muted)"),
      cap: resolve("var(--border)"),
      bulbs: [resolve("var(--xmas-red)"), resolve("var(--accent)"), resolve("var(--xmas-gold)")],
    };
  }

  function step() {
    let motion = 0;
    ropes.forEach((rope) => {
      const points = rope.points;
      for (let index = 1; index < points.length - 1; index++) {
        const point = points[index];
        const vx = (point.x - point.px) * DAMPING;
        const vy = (point.y - point.py) * DAMPING;
        point.px = point.x;
        point.py = point.y;
        point.x += vx;
        point.y += vy + GRAVITY * STEP * STEP;
        motion = Math.max(motion, Math.abs(vx) + Math.abs(vy));
      }
      for (let round = 0; round < 8; round++) {
        for (let index = 0; index < points.length - 1; index++) {
          const a = points[index];
          const b = points[index + 1];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const distance = Math.hypot(dx, dy) || 1;
          const share = (distance - rope.length) / distance;
          // the anchors stay where they are
          const shareA = index === 0 ? 0 : index + 1 === points.length - 1 ? 1 : 0.5;
          a.x += dx * share * shareA;
          a.y += dy * share * shareA;
          b.x -= dx * share * (1 - shareA);
          b.y -= dy * share * (1 - shareA);
        }
      }
    });
    return motion;
  }

  function build() {
    fontSize = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = Math.max(1, Math.round(width / (SWAG * rem())));
    const span = width / count;
    // start close to the hanging shape (a parabola as deep as the rope allows), then let it settle
    const sag = Math.sqrt(3 * span * span * (SLACK - 1) / 8);
    ropes = [];
    for (let swag = 0; swag < count; swag++) {
      const points = [];
      for (let index = 0; index < POINTS; index++) {
        const t = index / (POINTS - 1);
        const x = (swag + t) * span;
        const y = 1 + 4 * sag * t * (1 - t);
        points.push({ x, y, px: x, py: y });
      }
      ropes.push({ points, length: span * SLACK / (POINTS - 1) });
    }
    for (let round = 0; round < 240; round++) step();
    ropes.forEach((rope) => rope.points.forEach((point) => { point.px = point.x; point.py = point.y; }));
  }

  function draw() {
    const scale = rem() / 16;
    context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    context.lineWidth = scale;
    context.lineJoin = "round";
    context.strokeStyle = colors.wire;
    context.beginPath();
    ropes.forEach((rope) => {
      const points = rope.points;
      context.moveTo(points[0].x, points[0].y);
      for (let index = 1; index < points.length - 1; index++) {
        const next = points[index + 1];
        context.quadraticCurveTo(points[index].x, points[index].y,
          (points[index].x + next.x) / 2, (points[index].y + next.y) / 2);
      }
      context.lineTo(points[points.length - 1].x, points[points.length - 1].y);
    });
    context.stroke();
    let bulb = 0;
    ropes.forEach((rope) => {
      const points = rope.points;
      for (let index = 2; index < points.length - 1; index += 2) {
        const point = points[index];
        // half the slope of the wire: the bulbs stick out of it, and gravity pulls them down a little
        const before = points[index - 1];
        const after = points[index + 1];
        const angle = Math.atan2(after.y - before.y, after.x - before.x) / 2;
        context.save();
        context.translate(point.x, point.y);
        context.rotate(angle);
        context.fillStyle = colors.cap;
        context.fillRect(-1.2 * scale, 0, 2.4 * scale, 2.2 * scale);
        context.fillStyle = colors.bulbs[bulb++ % colors.bulbs.length];
        context.beginPath();
        context.ellipse(0, 4.7 * scale, 1.9 * scale, 2.8 * scale, 0, 0, Math.PI * 2);
        context.fill();
        context.restore();
      }
    });
  }

  function tick(time) {
    const elapsed = Math.min(0.05, (time - (last || time)) / 1000);
    last = time;
    backlog += elapsed;
    let motion = 0;
    while (backlog >= STEP) {
      motion = Math.max(motion, step());
      backlog -= STEP;
    }
    draw();
    rest = motion < 0.03 ? rest + elapsed : 0;
    frame = rest > 1 ? null : window.requestAnimationFrame(tick);
  }

  function wake() {
    if (!canvas || frame || reduced.matches || document.hidden) return;
    rest = 0;
    last = 0;
    backlog = 0;
    frame = window.requestAnimationFrame(tick);
  }

  function sleep() {
    window.cancelAnimationFrame(frame);
    frame = null;
  }

  // a sweep of the pointer pushes the points near it along
  function nudge(event) {
    if (!canvas || reduced.matches) return;
    const box = canvas.getBoundingClientRect();
    const x = event.clientX - box.left;
    const y = event.clientY - box.top;
    const previous = pointer;
    pointer = { x, y };
    const reach = 2 * rem();
    if (!previous || y < -reach || y > box.height + reach) return;
    const limit = 0.6 * rem();
    const dx = Math.max(-limit, Math.min(limit, x - previous.x));
    const dy = Math.max(-limit, Math.min(limit, y - previous.y));
    let moved = false;
    ropes.forEach((rope) => {
      for (let index = 1; index < rope.points.length - 1; index++) {
        const point = rope.points[index];
        const distance = Math.hypot(point.x - x, point.y - y);
        if (distance > reach) continue;
        const strength = 0.35 * (1 - distance / reach);
        point.px -= dx * strength;
        point.py -= dy * strength;
        moved = true;
      }
    });
    if (moved) wake();
  }

  function refresh() {
    if (!canvas) return;
    sleep();
    build();
    draw();
  }

  function lightsWanted() {
    if (choice) return choice === "on";
    try {
      return window.localStorage.getItem(LIGHTS) !== "off";
    } catch (error) {
      return true;
    }
  }

  function mount() {
    const topbar = document.querySelector("[data-topbar]");
    if (!topbar || document.documentElement.dataset.season !== "christmas") return;
    const on = lightsWanted();
    document.querySelectorAll("[data-lights]").forEach((button) => {
      button.setAttribute("aria-pressed", (button.dataset.lights === "on") === on ? "true" : "false");
    });
    // switched off: neither the swags nor the straight string of the stylesheet
    topbar.dataset.garland = on ? "on" : "off";
    if (!on) {
      if (canvas) {
        sleep();
        canvas.remove();
        canvas = null;
        ropes = [];
      }
      return;
    }
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.className = "garland";
      canvas.setAttribute("aria-hidden", "true");
      context = canvas.getContext("2d");
    }
    // another page loaded in place (hx-boost) brought a new top bar
    if (canvas.parentNode !== topbar) topbar.append(canvas);
    readColors();
    refresh();
  }

  function recolor() {
    if (!canvas) return;
    readColors();
    draw();
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-lights]");
    if (!button) return;
    choice = button.dataset.lights;
    try {
      window.localStorage.setItem(LIGHTS, choice);
    } catch (error) {
      // private window or blocked storage: the choice lasts until the next page
    }
    mount();
  });
  document.addEventListener("pointermove", nudge, { passive: true });
  window.addEventListener("resize", refresh);
  document.addEventListener("visibilitychange", () => { if (document.hidden) sleep(); });
  reduced.addEventListener("change", refresh);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", recolor);
  new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  document.addEventListener("DOMContentLoaded", mount);
  document.addEventListener("santa:page", mount);
})();
