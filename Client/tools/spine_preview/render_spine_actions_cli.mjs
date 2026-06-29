import { createServer } from "node:http";
import { readFile, writeFile, mkdir, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const value = argv[i];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) args[key] = true;
    else {
      args[key] = next;
      i++;
    }
  }
  return args;
}

function requirePath(args, name) {
  const value = args[name];
  if (!value) throw new Error(`missing --${name}`);
  return path.resolve(String(value));
}

function normalizeSlashes(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "");
}

function contentType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".html") return "text/html; charset=utf-8";
  if (ext === ".js") return "text/javascript; charset=utf-8";
  if (ext === ".json") return "application/json; charset=utf-8";
  if (ext === ".atlas" || ext === ".txt") return "text/plain; charset=utf-8";
  if (ext === ".png") return "image/png";
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".webp") return "image/webp";
  return "application/octet-stream";
}

function sanitizeName(value, fallback) {
  const cleaned = String(value || "")
    .replace(/[<>:"/\\|?*\x00-\x1f]+/g, "_")
    .replace(/\s+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || fallback;
}

function makeAssetResolver(files) {
  const roots = [
    path.dirname(files.jsonPath),
    path.dirname(files.atlasPath),
    ...files.imagePaths.map((p) => path.dirname(p)),
  ];
  const byName = new Map();
  for (const imagePath of files.imagePaths) byName.set(path.basename(imagePath).toLowerCase(), imagePath);
  return (requestPath) => {
    const rel = normalizeSlashes(decodeURIComponent(requestPath));
    for (const root of roots) {
      const candidate = path.resolve(root, rel);
      if (existsSync(candidate)) return candidate;
    }
    return byName.get(path.basename(rel).toLowerCase()) || "";
  };
}

function createLocalServer(files, runtimePath) {
  const resolveAsset = makeAssetResolver(files);
  const rendererPath = path.join(__dirname, "renderer.html");
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      let filePath = "";
      if (url.pathname === "/renderer.html") filePath = rendererPath;
      else if (url.pathname === "/vendor/spine-webgl-3.8.js") filePath = runtimePath;
      else if (url.pathname === "/skeleton.json") filePath = files.jsonPath;
      else if (url.pathname === "/skeleton.atlas") filePath = files.atlasPath;
      else filePath = resolveAsset(url.pathname.slice(1));

      if (!filePath) {
        res.writeHead(404);
        res.end("not found");
        return;
      }

      const data = await readFile(filePath);
      res.writeHead(200, {
        "content-type": contentType(filePath),
        "cache-control": "no-store",
        "access-control-allow-origin": "*",
      });
      res.end(data);
    } catch (error) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(String(error && error.stack || error));
    }
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({ server, baseUrl: `http://127.0.0.1:${address.port}` });
    });
  });
}

function maxTime(value) {
  let max = 0;
  function walk(node, key = "") {
    if (typeof node === "number" && key === "time" && Number.isFinite(node)) {
      max = Math.max(max, node);
      return;
    }
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    if (node && typeof node === "object") {
      for (const [childKey, childValue] of Object.entries(node)) walk(childValue, childKey);
    }
  }
  walk(value);
  return max || 1;
}

async function readAnimations(jsonPath) {
  const data = JSON.parse(await readFile(jsonPath, "utf-8"));
  const animations = data.animations || {};
  return Object.entries(animations).map(([name, body]) => ({
    name,
    duration: maxTime(body),
  }));
}

async function renderFrame({ page, baseUrl, outPath, width, height, animationName, time, frameCount, timeoutMs }) {
  const url = `${baseUrl}/renderer.html?width=${width}&height=${height}&frames=${frameCount}&animation=${encodeURIComponent(animationName)}&time=${encodeURIComponent(String(time))}`;
  await page.goto(url, { waitUntil: "load", timeout: timeoutMs });
  await page.waitForFunction(() => document.body.getAttribute("data-ready") === "1", null, { timeout: timeoutMs });
  await page.screenshot({ path: outPath, fullPage: false });
  const info = await stat(outPath);
  if (!info.size) throw new Error(`empty screenshot: ${outPath}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const files = {
    jsonPath: requirePath(args, "json"),
    atlasPath: requirePath(args, "atlas"),
    imagePaths: String(args.images || "").split(";").filter(Boolean).map((p) => path.resolve(p)),
  };
  const outDir = requirePath(args, "out");
  const runtimePath = path.resolve(args.runtime || path.join(__dirname, "vendor", "spine-webgl-3.8.js"));
  const chromePath = path.resolve(args.chrome || "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe");
  const width = Number(args.width || 768);
  const height = Number(args.height || 768);
  const frameCount = Number(args.frames || 5);
  const timeoutMs = Number(args.timeout || 30000);

  await mkdir(outDir, { recursive: true });
  const animations = await readAnimations(files.jsonPath);
  const { server, baseUrl } = await createLocalServer(files, runtimePath);
  const rendered = [];
  const { chromium } = await import("playwright-core");
  const browser = await chromium.launch({
    executablePath: chromePath,
    headless: true,
    args: [
      "--use-gl=swiftshader-webgl",
      "--enable-webgl",
      "--ignore-gpu-blocklist",
      "--disable-gpu-sandbox",
      "--disable-features=SkiaGraphite,DawnGraphite,UseDawn",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-sync",
      "--hide-scrollbars",
      "--mute-audio",
      "--no-first-run",
      "--no-default-browser-check",
      "--enable-unsafe-swiftshader",
    ],
  });
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  try {
    for (let index = 0; index < animations.length; index++) {
      const animation = animations[index];
      const safeName = sanitizeName(animation.name, `animation_${index + 1}`);
      const frameDir = path.join(outDir, safeName);
      await mkdir(frameDir, { recursive: true });
      const framePaths = [];
      const samples = [];
      for (let frame = 0; frame < frameCount; frame++) {
        samples.push(frameCount === 1 ? animation.duration / 2 : (animation.duration * (frame + 1)) / (frameCount + 1));
      }
      for (let frame = 0; frame < samples.length; frame++) {
        const framePath = path.join(frameDir, `frame_${String(frame).padStart(2, "0")}.png`);
        await renderFrame({
          page,
          baseUrl,
          outPath: framePath,
          width,
          height,
          animationName: animation.name,
          time: samples[frame],
          frameCount,
          timeoutMs,
        });
        framePaths.push(framePath);
      }
      rendered.push({
        name: animation.name,
        duration: animation.duration,
        sample_times: samples,
        frame_paths: framePaths,
      });
      console.error(`[spine] ${animation.name}: ${framePaths.length} frames`);
    }
  } finally {
    await browser.close();
    server.close();
  }

  const manifest = {
    skeleton: files.jsonPath,
    atlas: files.atlasPath,
    images: files.imagePaths,
    runtime: runtimePath,
    renderer: "spine-ts-3.8-webgl-chrome-screenshot",
    animation_count: rendered.length,
    animations: rendered,
  };
  const manifestPath = path.join(outDir, "manifest.json");
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2), "utf-8");
  console.log(JSON.stringify({ manifest_path: manifestPath, ...manifest }, null, 2));
}

main().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
