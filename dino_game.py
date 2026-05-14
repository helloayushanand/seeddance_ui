DINO_HTML = """
<style>
  body { margin: 0; background: #1e1e1e; display: flex; flex-direction: column; align-items: center; font-family: monospace; }
  canvas { background: #1e1e1e; border-bottom: 2px solid #555; display: block; }
  #info { color: #aaa; font-size: 13px; margin-top: 8px; }
  #score-box { color: #fff; font-size: 15px; margin-top: 4px; }
</style>
<canvas id="c" width="700" height="160"></canvas>
<div id="score-box">SCORE: <span id="sc">0</span> &nbsp;&nbsp; HI: <span id="hi">0</span></div>
<div id="info">Press SPACE or TAP to jump &nbsp;|&nbsp; Duck with ↓</div>

<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');

const W = canvas.width, H = canvas.height;
const GROUND = H - 30;

let state = 'idle'; // idle | running | dead
let score = 0, hiScore = 0, frame = 0, speed = 5, spawnTimer = 0;
let obstacles = [], clouds = [];

// Dino
const dino = {
  x: 80, y: GROUND, w: 44, h: 48,
  vy: 0, jumping: false, ducking: false,
  legFrame: 0,
};

function resetGame() {
  dino.y = GROUND; dino.vy = 0; dino.jumping = false; dino.ducking = false;
  obstacles = []; clouds = [];
  score = 0; frame = 0; speed = 5; spawnTimer = 0;
  state = 'running';
}

function jump() {
  if (state === 'idle' || state === 'dead') { resetGame(); return; }
  if (!dino.jumping && !dino.ducking) {
    dino.vy = -14;
    dino.jumping = true;
  }
}

function duck(on) {
  if (!dino.jumping) dino.ducking = on;
}

// Input
document.addEventListener('keydown', e => {
  if (e.code === 'Space' || e.code === 'ArrowUp') { e.preventDefault(); jump(); }
  if (e.code === 'ArrowDown') { e.preventDefault(); duck(true); }
});
document.addEventListener('keyup', e => {
  if (e.code === 'ArrowDown') duck(false);
});
canvas.addEventListener('click', () => jump());

// Spawn
function spawnObstacle() {
  const types = ['cactusS', 'cactusL', 'cactusW', 'bird'];
  const birdOk = score > 300;
  const pool = birdOk ? types : types.slice(0, 3);
  const type = pool[Math.floor(Math.random() * pool.length)];
  if (type === 'bird') {
    const birdY = [GROUND - 20, GROUND - 50, GROUND - 80][Math.floor(Math.random()*3)];
    obstacles.push({ type:'bird', x: W+20, y: birdY, w:46, h:30, flap:0 });
  } else {
    const configs = {
      cactusS: { w:20, h:40 }, cactusL: { w:24, h:55 }, cactusW: { w:52, h:42 }
    };
    const c = configs[type];
    obstacles.push({ type, x: W+20, y: GROUND + 48 - c.h, w: c.w, h: c.h });
  }
}

function spawnCloud() {
  clouds.push({ x: W+20, y: 20 + Math.random()*40, w:80, h:28 });
}

// Draw helpers
function drawDino() {
  const x = dino.x, h = dino.ducking ? 30 : dino.h;
  const y = dino.ducking ? GROUND + 48 - 30 : dino.y;
  const w = dino.ducking ? 58 : dino.w;
  ctx.fillStyle = '#eee';

  if (state === 'dead') {
    // eyes X
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = '#1e1e1e';
    ctx.font = 'bold 14px monospace';
    ctx.fillText('X X', x+6, y+16);
    return;
  }

  ctx.fillRect(x, y, w, h);
  // eye
  ctx.fillStyle = '#1e1e1e';
  ctx.fillRect(x + w - 10, y + 8, 7, 7);
  // mouth
  ctx.fillRect(x + w - 6, y + 20, 4, 2);

  if (!dino.jumping && !dino.ducking) {
    // legs animate
    const l = dino.legFrame < 10;
    ctx.fillStyle = '#eee';
    ctx.fillRect(x+8,  GROUND + 48 - 14, 10, 14);  // back leg
    ctx.fillRect(x+24, GROUND + 48 - (l ? 14 : 8), 10, l ? 14 : 8); // front leg
  }
}

function drawCactus(o) {
  ctx.fillStyle = '#5a9e5a';
  ctx.fillRect(o.x, o.y, o.w, o.h);
  // arms
  const aw = Math.floor(o.w * 0.35);
  ctx.fillRect(o.x - aw, o.y + Math.floor(o.h*0.25), aw, Math.floor(o.h*0.3));
  ctx.fillRect(o.x + o.w, o.y + Math.floor(o.h*0.35), aw, Math.floor(o.h*0.25));
}

function drawBird(o) {
  ctx.fillStyle = '#c0a060';
  ctx.fillRect(o.x, o.y, o.w, o.h);
  // wings flap
  const wingY = o.flap < 15 ? o.y - 12 : o.y + 10;
  ctx.fillRect(o.x + 8, wingY, o.w - 16, 10);
  // beak
  ctx.fillStyle = '#e08020';
  ctx.fillRect(o.x + o.w, o.y + 10, 10, 6);
  o.flap = (o.flap + 1) % 30;
}

function drawCloud(c) {
  ctx.fillStyle = '#3a3a3a';
  ctx.fillRect(c.x, c.y, c.w, c.h);
  ctx.fillRect(c.x+10, c.y-12, c.w-20, 14);
}

function drawGround() {
  ctx.fillStyle = '#555';
  ctx.fillRect(0, GROUND + 48, W, 2);
  // pebbles
  for (let i = 0; i < 10; i++) {
    ctx.fillRect(((i * 73 + frame) % W), GROUND + 52, 4, 2);
  }
}

function collides(a, b) {
  const pad = 6;
  return a.x + pad < b.x + b.w - pad &&
         a.x + (a.ducking ? 58 : a.w) - pad > b.x + pad &&
         (a.ducking ? GROUND+48-30 : a.y) + pad < b.y + b.h - pad &&
         (a.ducking ? GROUND+48-30 : a.y) + (a.ducking ? 30 : a.h) - pad > b.y + pad;
}

function update() {
  if (state !== 'running') return;
  frame++;
  score++;
  speed = 5 + Math.floor(score / 500) * 0.8;

  // Dino physics
  if (dino.jumping) {
    dino.vy += 0.8;
    dino.y += dino.vy;
    if (dino.y >= GROUND) { dino.y = GROUND; dino.vy = 0; dino.jumping = false; }
  }
  dino.legFrame = (dino.legFrame + 1) % 20;

  // Obstacles
  spawnTimer--;
  if (spawnTimer <= 0) {
    spawnObstacle();
    spawnTimer = 60 + Math.floor(Math.random() * 80) - Math.floor(score/1000)*5;
    spawnTimer = Math.max(spawnTimer, 35);
  }
  obstacles.forEach(o => o.x -= speed);
  obstacles = obstacles.filter(o => o.x + o.w > -10);

  // Clouds
  if (frame % 90 === 0) spawnCloud();
  clouds.forEach(c => c.x -= 1.5);
  clouds = clouds.filter(c => c.x + c.w > -10);

  // Collision
  for (const o of obstacles) {
    if (collides(dino, o)) {
      state = 'dead';
      if (score > hiScore) hiScore = score;
    }
  }
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  drawGround();
  clouds.forEach(drawCloud);
  obstacles.forEach(o => o.type === 'bird' ? drawBird(o) : drawCactus(o));
  drawDino();

  if (state === 'idle') {
    ctx.fillStyle = '#aaa';
    ctx.font = '16px monospace';
    ctx.fillText('Press SPACE or TAP to start', W/2 - 140, H/2 - 10);
  }
  if (state === 'dead') {
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 18px monospace';
    ctx.fillText('GAME OVER', W/2 - 70, H/2 - 10);
    ctx.fillStyle = '#aaa';
    ctx.font = '13px monospace';
    ctx.fillText('Press SPACE or TAP to restart', W/2 - 130, H/2 + 15);
  }

  document.getElementById('sc').textContent = String(score).padStart(5,'0');
  document.getElementById('hi').textContent = String(hiScore).padStart(5,'0');
}

function loop() {
  update();
  draw();
  requestAnimationFrame(loop);
}
loop();
</script>
"""

def render(height: int = 220) -> None:
    import streamlit.components.v1 as components
    components.html(DINO_HTML, height=height)
