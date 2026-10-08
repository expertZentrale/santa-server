// The Christmas theme: snow behind the cards. Off with "reduce motion" (unless switched on in the user menu), paused
// in a hidden tab. The choice of the menu is kept in this browser only.
(function () {
  "use strict";

  const KEY = "santa-snow";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let canvas = null;
  let context = null;
  let flakes = [];
  let frame = null;
  let last = 0;

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
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(window.innerWidth * ratio);
    canvas.height = Math.round(window.innerHeight * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = Math.min(140, Math.round(window.innerWidth * window.innerHeight / 9000));
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
    const seconds = Math.min(0.05, (time - (last || time)) / 1000);
    last = time;
    context.clearRect(0, 0, window.innerWidth, window.innerHeight);
    context.fillStyle = getComputedStyle(canvas).color;
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
    frame = window.requestAnimationFrame(draw);
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
  document.addEventListener("visibilitychange", update);
  reduced.addEventListener("change", update);
  document.addEventListener("DOMContentLoaded", update);
  document.addEventListener("santa:page", update);
})();
