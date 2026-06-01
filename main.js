import { ipcMain as w, app as d, session as Se, BrowserWindow as M, nativeImage as Ee, Tray as Ae, Menu as ae } from "electron";
import { fileURLToPath as Ie } from "node:url";
import { spawn as B, spawnSync as _e } from "node:child_process";
import p from "node:fs";
import le from "node:http";
import Q from "node:https";
import l from "node:path";
const ce = l.dirname(Ie(import.meta.url)), _ = "test";
function ue() {
  return { ...process.env, OPENPA_UPGRADE_CHANNEL: _ };
}
function se() {
  return !d.isPackaged || _ !== "production";
}
const z = {
  agentUrl: "",
  deploymentType: "",
  autoUpdate: !0,
  channel: _
};
function pe() {
  return l.join(d.getPath("userData"), "openpa-config.json");
}
function Te() {
  let n;
  try {
    const e = p.readFileSync(pe(), "utf8"), t = JSON.parse(e);
    n = { ...z, ...t };
  } catch {
    n = { ...z };
  }
  return n.channel = _, n;
}
function Ue(n) {
  const e = pe();
  p.mkdirSync(l.dirname(e), { recursive: !0 });
  const t = `${e}.tmp`;
  p.writeFileSync(t, JSON.stringify(n, null, 2), "utf8"), p.renameSync(t, e);
}
let h = { ...z };
function j(n) {
  return h = { ...h, ...n }, Ue(h), h;
}
const D = {
  tokens: {},
  loggedInProfiles: [],
  activeProfileId: "",
  reasoningEnabled: {}
};
function de() {
  return l.join(d.getPath("userData"), "openpa-auth.json");
}
function Oe() {
  try {
    const n = p.readFileSync(de(), "utf8"), e = JSON.parse(n);
    return {
      tokens: { ...D.tokens, ...e.tokens ?? {} },
      loggedInProfiles: Array.isArray(e.loggedInProfiles) ? e.loggedInProfiles.filter((t) => typeof t == "string") : [],
      activeProfileId: typeof e.activeProfileId == "string" ? e.activeProfileId : "",
      reasoningEnabled: { ...D.reasoningEnabled, ...e.reasoningEnabled ?? {} }
    };
  } catch {
    return { ...D, tokens: {}, loggedInProfiles: [], reasoningEnabled: {} };
  }
}
function X(n) {
  const e = de();
  p.mkdirSync(l.dirname(e), { recursive: !0 });
  const t = `${e}.tmp`;
  p.writeFileSync(t, JSON.stringify(n, null, 2), "utf8"), p.renameSync(t, e);
}
let g = {
  ...D,
  tokens: {},
  loggedInProfiles: [],
  reasoningEnabled: {}
};
function Ce(n) {
  return g = {
    tokens: n.tokens ? { ...g.tokens, ...n.tokens } : g.tokens,
    loggedInProfiles: n.loggedInProfiles ?? g.loggedInProfiles,
    activeProfileId: n.activeProfileId ?? g.activeProfileId,
    reasoningEnabled: n.reasoningEnabled ? { ...g.reasoningEnabled, ...n.reasoningEnabled } : g.reasoningEnabled
  }, X(g), g;
}
function $e(n) {
  const e = { ...g.tokens };
  return delete e[n], g = {
    ...g,
    tokens: e,
    loggedInProfiles: g.loggedInProfiles.filter((t) => t !== n)
  }, X(g), g;
}
function G() {
  const n = d.getPath("home");
  return p.existsSync(l.join(n, ".openpa", ".env")) || p.existsSync(l.join(n, ".openpa", "docker", ".env"));
}
function Le() {
  const n = G();
  !n && h.agentUrl ? j({ agentUrl: "", deploymentType: "" }) : n && !h.agentUrl && j({ agentUrl: "http://localhost:1112", deploymentType: "local" });
}
process.env.APP_ROOT = l.join(ce, "..");
const J = process.env.VITE_DEV_SERVER_URL, on = l.join(process.env.APP_ROOT, "dist-electron"), fe = l.join(process.env.APP_ROOT, "dist");
process.env.VITE_PUBLIC = J ? l.join(process.env.APP_ROOT, "public") : fe;
const O = /* @__PURE__ */ new Set(), F = /* @__PURE__ */ new WeakMap();
let E = null, N = null, V = null, H = null;
function I(n) {
  return H === null ? !1 : H.has(n);
}
w.handle("get-app-version", () => d.getVersion());
w.on("openpa:get-config-sync", (n) => {
  n.returnValue = h;
});
w.handle("openpa:get-config", () => h);
w.handle("openpa:set-config", (n, e) => {
  const t = j(e);
  return e.agentUrl !== void 0 && ne(), t;
});
w.on("openpa:auth:snapshot-sync", (n) => {
  n.returnValue = g;
});
w.handle("openpa:auth:get", () => g);
w.handle("openpa:auth:set", (n, e) => Ce(e));
w.handle("openpa:auth:remove-token", (n, e) => typeof e != "string" || !e ? g : $e(e));
const ie = process.env.OPENPA_INSTALLER_BASE ?? "https://raw.githubusercontent.com/openpa/openpa/main/install";
let A = null, v = null, C = null;
function xe() {
  return l.join(d.getPath("home"), ".openpa", "install.pid");
}
function Re() {
  const n = d.getPath("home"), e = l.join(n, ".openpa", "bin");
  if (process.platform === "win32") {
    const t = l.join(e, "openpa.cmd");
    try {
      const o = p.readFileSync(t, "utf8").match(/^"([^"]+)"\s*%\*/m);
      if (o && o[1]) return o[1];
    } catch {
    }
    return l.join(n, ".openpa", "venv", "Scripts", "openpa.exe");
  }
  return l.join(e, "openpa");
}
function he() {
  const n = Re(), e = l.dirname(n);
  return process.platform === "win32" ? l.join(e, "python.exe") : l.join(l.dirname(e), "bin", "python");
}
function ge() {
  return "http://127.0.0.1:1112/health";
}
function Y() {
  return new Promise((n) => {
    const e = le.get(ge(), { timeout: 1500 }, (t) => {
      t.resume(), n((t.statusCode ?? 0) < 400);
    });
    e.on("error", () => n(!1)), e.on("timeout", () => {
      e.destroy(), n(!1);
    });
  });
}
async function K(n = 3e4) {
  const e = Date.now();
  for (; Date.now() - e < n; ) {
    if (await Y()) return !0;
    await new Promise((t) => setTimeout(t, 500));
  }
  return !1;
}
function me() {
  const n = "http://127.0.0.1:1515", e = h.agentUrl;
  if (!e) return n;
  try {
    const t = new URL(e);
    return t.port = "1515", t.origin;
  } catch {
    return n;
  }
}
async function De(n, e) {
  if (J) {
    n.loadURL(J + e);
    return;
  }
  if (await Y()) {
    n.loadURL(`${me()}/${e}`);
    return;
  }
  n.loadFile(l.join(fe, "index.html"), { hash: e.slice(1) });
}
function ye() {
  for (const n of M.getAllWindows()) {
    if (n.isDestroyed()) continue;
    const e = n.webContents.getURL();
    if (!e.startsWith("file://")) continue;
    const t = e.indexOf("#"), r = t >= 0 ? e.slice(t) : "#/";
    n.loadURL(`${me()}/${r}`);
  }
}
async function Z() {
  if (await Y())
    return { ok: !0 };
  if (v && v.exitCode === null)
    return await K() ? { ok: !0 } : { ok: !1, error: "backend did not become healthy" };
  const n = he();
  if (!p.existsSync(n))
    return { ok: !1, error: `venv python missing at ${n} — installer didn't finish?` };
  const e = l.join(d.getPath("home"), ".openpa"), t = l.join(e, "server.log"), r = l.join(e, "server.err.log");
  let o, i;
  try {
    p.mkdirSync(e, { recursive: !0 }), o = p.openSync(t, "a"), i = p.openSync(r, "a");
  } catch (f) {
    return { ok: !1, error: `could not open server log files: ${String(f)}` };
  }
  let s;
  try {
    s = B(n, ["-m", "app.cli.main", "serve"], {
      stdio: ["ignore", o, i],
      windowsHide: !0,
      env: ue()
    });
  } catch (f) {
    try {
      o && p.closeSync(o);
    } catch {
    }
    try {
      i && p.closeSync(i);
    } catch {
    }
    return { ok: !1, error: String(f) };
  }
  v = s;
  try {
    p.writeFileSync(xe(), String(s.pid ?? ""));
  } catch {
  }
  return s.on("exit", () => {
    v === s && (v = null);
    try {
      o && p.closeSync(o);
    } catch {
    }
    try {
      i && p.closeSync(i);
    } catch {
    }
  }), await K() ? { ok: !0 } : { ok: !1, error: `backend at ${ge()} did not respond after 30s` };
}
w.handle("openpa:server:start", async () => Z());
w.handle("openpa:backend-upgrade:apply", async (n) => We(n.sender));
w.handle("openpa:installer:detect", async () => we());
w.handle("openpa:installer:list-versions", async () => He());
w.handle("openpa:installer:run", async (n, e) => {
  if (A)
    throw new Error("An install is already running.");
  return Be(n.sender, e);
});
w.handle("openpa:installer:cancel", () => {
  if (!A) return !1;
  try {
    A.kill("SIGTERM");
  } catch {
  }
  return !0;
});
async function we() {
  const n = process.platform, e = {
    os: n === "linux" ? "linux" : n === "darwin" ? "macos" : n === "win32" ? "windows" : "unknown",
    arch: process.arch,
    hasDocker: !1,
    hasPython: !1,
    pythonVersion: "",
    channel: _
  };
  if (await q("docker", ["--version"]).catch(() => null)) {
    const o = await q("docker", ["info"]).catch(() => null);
    e.hasDocker = o !== null;
  }
  const r = n === "win32" ? [["py", ["-3.13", "-c", 'import sys;print("%d.%d" % sys.version_info[:2])']]] : [
    ["python3.13", ["-c", 'import sys;print("%d.%d" % sys.version_info[:2])']],
    ["python3", ["-c", 'import sys;print("%d.%d" % sys.version_info[:2])']]
  ];
  for (const [o, i] of r) {
    const s = await q(o, i).catch(() => null);
    if (s && /^3\.(1[3-9]|[2-9]\d)$/.test(s.trim())) {
      e.hasPython = !0, e.pythonVersion = s.trim();
      break;
    }
  }
  return e;
}
const Ne = process.env.OPENPA_UPGRADE_REPO ?? "openpa/openpa", Fe = `https://api.github.com/repos/${Ne}/releases?per_page=100`, Ve = /^v(\d+)\.(\d+)\.(\d+)-test(\d+)$/;
function Me(n, e) {
  const t = (i) => {
    const s = /^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$/.exec(i);
    if (!s) return [0, 0, 0, 0, 0];
    const a = s[4] === void 0 ? 1 : 0, f = s[4] === void 0 ? 0 : parseInt(s[4], 10);
    return [parseInt(s[1], 10), parseInt(s[2], 10), parseInt(s[3], 10), a, f];
  }, r = t(n), o = t(e);
  for (let i = 0; i < r.length; i++)
    if (r[i] !== o[i]) return r[i] - o[i];
  return 0;
}
function je(n, e = 1e4) {
  return new Promise((t, r) => {
    const o = Q.get(
      n,
      {
        headers: {
          Accept: "application/vnd.github+json",
          // GitHub requires a User-Agent on every request. 60 req/hr/IP
          // unauthenticated is plenty for a one-shot install lookup.
          "User-Agent": `openpa-installer/${d.getVersion()}`
        }
      },
      (i) => {
        if (i.statusCode && i.statusCode >= 400)
          return i.resume(), r(new Error(`${n} returned HTTP ${i.statusCode}`));
        let s = "";
        i.setEncoding("utf8"), i.on("data", (a) => {
          s += a;
        }), i.on("end", () => {
          try {
            t(JSON.parse(s));
          } catch (a) {
            r(a);
          }
        });
      }
    );
    o.on("error", r), o.setTimeout(e, () => {
      o.destroy(new Error(`${n} timed out after ${e}ms`));
    });
  });
}
async function He() {
  const n = d.getVersion().split(/[-+]/)[0], e = _, t = await je(Fe);
  if (!Array.isArray(t))
    throw new Error("GitHub /releases did not return a list payload");
  const r = {}, o = [];
  for (const s of t) {
    const a = (s == null ? void 0 : s.tag_name) ?? "";
    {
      if (!s.prerelease) continue;
      const f = Ve.exec(a);
      if (!f) continue;
      const m = `${f[1]}.${f[2]}.${f[3]}`;
      if (m !== n) continue;
      const c = `${m}.dev${parseInt(f[4], 10)}`;
      o.push(c), s.html_url && (r[c] = s.html_url);
    }
  }
  o.sort(Me);
  const i = o.length > 0 ? o[o.length - 1] : null;
  return { electronVersion: n, channel: e, versions: o, latest: i, htmlUrls: r };
}
function q(n, e, t = 5e3) {
  return new Promise((r, o) => {
    const i = B(n, e, { stdio: ["ignore", "pipe", "pipe"] });
    let s = "", a = !1;
    const f = setTimeout(() => {
      a = !0;
      try {
        i.kill("SIGKILL");
      } catch {
      }
    }, t);
    i.stdout.on("data", (m) => {
      s += m.toString();
    }), i.on("error", (m) => {
      clearTimeout(f), o(m);
    }), i.on("close", (m) => {
      if (clearTimeout(f), a) return o(new Error(`${n} timed out`));
      if (m !== 0) return o(new Error(`${n} exited ${m}`));
      r(s.trim());
    });
  });
}
async function Be(n, e) {
  const t = (c, u) => {
    n.isDestroyed() || n.send(c, u);
  };
  t("openpa:installer:log", { stream: "info", line: "Detecting platform..." });
  const r = await we();
  if (e.mode === "docker" && !r.hasDocker)
    throw new Error("Docker mode selected but Docker is not available on this machine.");
  if (e.deployment === "server" && !e.appHost)
    throw new Error("Server deployment requires a public host (IP or domain).");
  const o = r.os === "windows", i = o ? "install.ps1" : "install.sh";
  let s;
  {
    const c = `${ie}/${i}`, u = l.join(d.getPath("userData"), "installer");
    p.mkdirSync(u, { recursive: !0 }), s = l.join(u, i), t("openpa:installer:log", { stream: "info", line: `Downloading ${c}...` }), await ke(c, s), o || p.chmodSync(s, 493);
  }
  const a = [
    "--deployment",
    e.deployment,
    "--mode",
    e.mode,
    "--unattended",
    "--no-launch"
  ];
  if (e.appHost && a.push("--host", e.appHost), e.deployment === "custom" && e.customFields) {
    const c = e.customFields;
    c.listen_host && a.push("--listen-host", c.listen_host), c.public_url && a.push("--public-url", c.public_url), c.allowed_origins && a.push("--allowed-origins", c.allowed_origins), c.wizard_preset && a.push("--wizard-preset", c.wizard_preset);
  }
  a.push("--channel", _), a.push("--electron-version", d.getVersion().split(/[-+]/)[0]), e.version && a.push("--version", e.version);
  let f, m;
  if (o) {
    f = "powershell.exe";
    const c = a.flatMap((u) => u === "--deployment" ? ["-Deployment"] : u === "--host" ? ["-AppHost"] : u === "--mode" ? ["-Mode"] : u === "--unattended" ? ["-Unattended"] : u === "--no-launch" ? ["-NoLaunch"] : u === "--channel" ? ["-Channel"] : u === "--listen-host" ? ["-ListenHost"] : u === "--public-url" ? ["-PublicUrl"] : u === "--allowed-origins" ? ["-AllowedOrigins"] : u === "--wizard-preset" ? ["-WizardPreset"] : u === "--version" ? ["-Version"] : u === "--electron-version" ? ["-ElectronVersion"] : [u]);
    m = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", s, ...c];
  } else
    f = "bash", m = [s, ...a];
  return t("openpa:installer:log", {
    stream: "info",
    line: `Running ${f} ${m.join(" ")} (channel: ${_})`
  }), new Promise((c, u) => {
    const k = B(f, m, {
      env: {
        ...process.env,
        // Preserve any custom template/script base the user set, so
        // staging installs can be tested end-to-end from the GUI.
        OPENPA_TEMPLATE_BASE: process.env.OPENPA_TEMPLATE_BASE ?? `${ie}/templates`,
        // Tell the script the install is being driven by the Electron
        // app so it suppresses the "Wizard URL: …" handoff text. The
        // Electron app navigates to the in-window wizard itself once
        // the script reports exitCode = 0.
        OPENPA_INSTALLER_FRONTEND: "electron"
      }
    });
    A = k, k.stdout.on("data", (P) => {
      t("openpa:installer:log", { stream: "stdout", line: P.toString() });
    }), k.stderr.on("data", (P) => {
      t("openpa:installer:log", { stream: "stderr", line: P.toString() });
    });
    let S = !1;
    const T = (P, U) => {
      var te;
      if (S) return;
      if (S = !0, A = null, U !== void 0) {
        t("openpa:installer:done", { exitCode: -1, error: String(U) }), u(U);
        return;
      }
      const L = P ?? -1;
      if (L === 0) {
        let x;
        if (e.deployment === "local")
          x = "http://localhost:1112";
        else if (e.deployment === "custom") {
          const oe = ((te = e.customFields) == null ? void 0 : te.public_url) ?? "";
          if (oe)
            try {
              const re = new URL(oe);
              x = `${re.protocol}//${re.host}`;
            } catch {
              x = "http://localhost:1112";
            }
          else
            x = "http://localhost:1112";
        } else
          x = `http://${e.appHost}:1112`;
        j({
          agentUrl: x,
          deploymentType: e.deployment
        });
      }
      t("openpa:installer:done", { exitCode: L }), c({ exitCode: L });
    };
    k.on("error", (P) => T(null, P)), k.on("exit", (P) => T(P));
  });
}
async function We(n) {
  if (C)
    throw new Error("An upgrade is already running.");
  const e = (r, o) => {
    n.isDestroyed() || n.send(r, o);
  }, t = he();
  if (!p.existsSync(t)) {
    const r = `venv python missing at ${t}`;
    return e("openpa:backend-upgrade:done", { exitCode: -1, ok: !1, error: r }), { exitCode: -1, ok: !1, error: r };
  }
  return e("openpa:backend-upgrade:status", { phase: "starting" }), e("openpa:backend-upgrade:log", {
    stream: "info",
    line: `$ ${t} -m app.cli.main upgrade apply --yes`
  }), new Promise((r) => {
    var f, m;
    let o;
    try {
      o = B(t, ["-m", "app.cli.main", "upgrade", "apply", "--yes"], {
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: !0,
        env: ue()
      });
    } catch (c) {
      const u = String(c);
      e("openpa:backend-upgrade:done", { exitCode: -1, ok: !1, error: u }), r({ exitCode: -1, ok: !1, error: u });
      return;
    }
    C = o, e("openpa:backend-upgrade:status", { phase: "upgrading" });
    const i = (c) => (u) => {
      const k = u.toString();
      for (const S of k.split(/\r?\n/))
        S.length > 0 && e("openpa:backend-upgrade:log", { stream: c, line: S });
    };
    (f = o.stdout) == null || f.on("data", i("stdout")), (m = o.stderr) == null || m.on("data", i("stderr"));
    let s = !1;
    const a = async (c, u) => {
      if (s) return;
      if (s = !0, C = null, u !== void 0) {
        const T = String(u);
        e("openpa:backend-upgrade:done", { exitCode: -1, ok: !1, error: T }), r({ exitCode: -1, ok: !1, error: T });
        return;
      }
      const k = c ?? -1;
      if (k !== 0) {
        e("openpa:backend-upgrade:done", { exitCode: k, ok: !1 }), r({ exitCode: k, ok: !1 });
        return;
      }
      e("openpa:backend-upgrade:status", { phase: "restarting" }), v && v.pid && (R(v.pid), v = null), await new Promise((T) => setTimeout(T, 1e3));
      const S = await Z();
      if (!S.ok) {
        e("openpa:backend-upgrade:done", {
          exitCode: k,
          ok: !1,
          error: `backend failed to restart: ${S.error ?? "unknown"}`
        }), r({ exitCode: k, ok: !1, error: S.error });
        return;
      }
      e("openpa:backend-upgrade:done", { exitCode: k, ok: !0 }), ne(), (async () => {
        if (!await K(3e4)) {
          e("openpa:backend-upgrade:done", {
            exitCode: k,
            ok: !1,
            error: "backend did not become healthy after upgrade"
          });
          return;
        }
        const P = String(Date.now());
        for (const U of M.getAllWindows()) {
          if (U.isDestroyed()) continue;
          const L = U.webContents.getURL();
          if (!(!L.startsWith("http://") && !L.startsWith("https://"))) {
            try {
              await U.webContents.executeJavaScript(
                `try { sessionStorage.setItem('openpa:just_updated', '${P}') } catch {}`,
                !0
              );
            } catch {
            }
            U.reload();
          }
        }
        ye();
      })(), r({ exitCode: k, ok: !0 });
    };
    o.on("error", (c) => {
      a(null, c);
    }), o.on("exit", (c) => {
      a(c);
    });
  });
}
function ke(n, e) {
  return new Promise((t, r) => {
    const o = p.createWriteStream(e);
    Q.get(n, (s) => {
      if (s.statusCode === 301 || s.statusCode === 302) {
        s.resume();
        const a = s.headers.location;
        return a ? (o.close(), ke(a, e).then(t, r)) : r(new Error(`Redirect from ${n} without Location`));
      }
      if (!s.statusCode || s.statusCode >= 400)
        return s.resume(), r(new Error(`Failed to fetch ${n}: HTTP ${s.statusCode}`));
      s.pipe(o), o.on("finish", () => o.close((a) => a ? r(a) : t()));
    }).on("error", (s) => {
      try {
        p.unlinkSync(e);
      } catch {
      }
      r(s);
    });
  });
}
function qe(n, e) {
  for (const t of O)
    t.isDestroyed() || F.get(t) !== "vnc" && t.webContents.send(n, e);
}
function ee() {
  const n = (e) => {
    e.isMinimized() && e.restore(), e.isVisible() || e.show(), e.focus();
  };
  if (E && !E.isDestroyed()) {
    n(E);
    return;
  }
  for (const e of O)
    if (!e.isDestroyed() && F.get(e) !== "vnc") {
      n(e);
      return;
    }
  b("main");
}
function $(n) {
  const e = new RegExp(`#/[^/?#]+/${n}(?:[?#]|$)`);
  for (const t of O)
    if (!t.isDestroyed() && F.get(t) !== "vnc" && e.test(t.webContents.getURL())) {
      t.isMinimized() && t.restore(), t.isVisible() || t.show(), t.focus();
      return;
    }
  b(n);
}
function W(n) {
  if (!n) return null;
  try {
    const e = new URL(n);
    return e.port = "6080", e.pathname = "/vnc.html", e.search = "", e.hash = "", `${e.toString()}?autoconnect=1&resize=remote`;
  } catch {
    return null;
  }
}
function ne() {
  return new Promise((n) => {
    const e = () => {
      Pe(), ve(), be(), n();
    };
    if (!h.agentUrl) {
      V = null, H = null, e();
      return;
    }
    let t;
    try {
      t = new URL("/api/services/tray-capabilities", h.agentUrl);
    } catch {
      e();
      return;
    }
    const o = (t.protocol === "https:" ? Q : le).get(t.toString(), { timeout: 4e3 }, (i) => {
      if ((i.statusCode ?? 0) >= 400) {
        i.resume(), e();
        return;
      }
      let s = "";
      i.setEncoding("utf8"), i.on("data", (a) => {
        s += a;
      }), i.on("end", () => {
        try {
          const a = JSON.parse(s);
          V = a.install_mode ?? null, H = Array.isArray(a.ui_features) ? new Set(a.ui_features) : null;
        } catch {
        }
        e();
      });
    });
    o.on("error", () => e()), o.on("timeout", () => {
      o.destroy(), e();
    });
  });
}
function ve() {
  if (process.platform !== "win32") return;
  const n = d.isPackaged ? "" : process.env.APP_ROOT ?? "", e = l.join(process.env.VITE_PUBLIC, "logo.ico"), t = (o, i, s) => {
    const a = [];
    return n && a.push(`"${n}"`), a.push(`--open=${o}`), {
      type: "task",
      program: process.execPath,
      args: a.join(" "),
      iconPath: e,
      iconIndex: 0,
      title: i,
      description: s
    };
  }, r = [];
  h.agentUrl && V === "docker" && W(h.agentUrl) && r.push(t("vnc", "Open VNC Desktop", "Open the OpenPA desktop VNC viewer")), h.agentUrl ? (r.push(t("main", "Open Main Page", "Open a new OpenPA chat window")), r.push(t("settings", "Open Settings", "Open the OpenPA settings window")), I("processes") && r.push(t("processes", "Process Manager", "Open the OpenPA process manager")), I("events") && r.push(t("events", "Events", "Open the OpenPA skill events page")), I("channels") && r.push(t("channels", "Channels", "Open the OpenPA channels page"))) : r.push(t("main", "Open OpenPA", "Open the OpenPA application")), d.setJumpList([{ type: "tasks", items: r }]);
}
function Pe() {
  if (!N) return;
  const n = [];
  h.agentUrl && V === "docker" && W(h.agentUrl) && n.push({ label: "Open VNC Desktop", click: () => {
    b("vnc");
  } }), h.agentUrl && (n.push({ label: "Open Main Page", click: () => {
    b("main");
  } }), n.push({ label: "Open Settings", click: () => {
    b("settings");
  } }), I("processes") && n.push({ label: "Process Manager", click: () => $("processes") }), I("events") && n.push({ label: "Events", click: () => $("events") }), I("channels") && n.push({ label: "Channels", click: () => $("channels") }), n.push({ type: "separator" })), n.push({ label: "Show", click: () => ee() }), n.push({ label: "Exit", click: () => {
    d.quit();
  } }), N.setContextMenu(ae.buildFromTemplate(n));
}
function be() {
  if (process.platform !== "darwin") return;
  const n = d.dock;
  if (!n) return;
  const e = [];
  h.agentUrl && V === "docker" && W(h.agentUrl) && e.push({ label: "Open VNC Desktop", click: () => {
    b("vnc");
  } }), h.agentUrl && (e.push({ label: "Open Main Page", click: () => {
    b("main");
  } }), e.push({ label: "Open Settings", click: () => {
    b("settings");
  } }), I("processes") && e.push({ label: "Process Manager", click: () => $("processes") }), I("events") && e.push({ label: "Events", click: () => $("events") }), I("channels") && e.push({ label: "Channels", click: () => $("channels") })), n.setMenu(ae.buildFromTemplate(e));
}
function ze() {
  const n = Ee.createFromPath(l.join(process.env.VITE_PUBLIC, "tray-logo-64x64.png"));
  N = new Ae(n), N.setToolTip("OpenPA"), Pe(), N.on("click", () => ee());
}
function b(n) {
  if (n === "vnc") {
    const r = W(h.agentUrl);
    if (!r)
      return b("main");
    const o = new M({
      width: 1280,
      height: 800,
      resizable: !0,
      autoHideMenuBar: !0,
      icon: l.join(process.env.VITE_PUBLIC, "logo.png"),
      title: "OpenPA VNC Desktop",
      webPreferences: {
        // No preload — this window loads third-party content (noVNC)
        // and must not have access to the openpa IPC bridge.
        contextIsolation: !0,
        sandbox: !0,
        devTools: se()
      }
    });
    return O.add(o), F.set(o, "vnc"), o.on("closed", () => {
      O.delete(o), E === o && (E = null);
    }), o.loadURL(r), o;
  }
  const e = new M({
    width: 1100,
    height: 750,
    resizable: !0,
    autoHideMenuBar: !0,
    titleBarStyle: "hidden",
    titleBarOverlay: {
      color: "#242424",
      symbolColor: "#ffffff",
      height: 32
    },
    icon: l.join(process.env.VITE_PUBLIC, "logo.png"),
    webPreferences: {
      preload: l.join(ce, "preload.mjs"),
      devTools: se()
    }
  });
  O.add(e), F.set(e, n), E = e, e.on("focus", () => {
    E = e;
  }), e.on("closed", () => {
    O.delete(e), E === e && (E = null);
  }), e.webContents.on("did-finish-load", () => {
    e.isDestroyed() || e.webContents.send("main-process-message", (/* @__PURE__ */ new Date()).toLocaleString());
  });
  const t = `#/?openpa_window=${n}`;
  return De(e, t), e;
}
function R(n) {
  if (!(!n || n <= 0))
    try {
      if (process.platform === "win32")
        _e("taskkill", ["/PID", String(n), "/T", "/F"], { stdio: "ignore" });
      else
        try {
          process.kill(-n, "SIGTERM");
        } catch {
          try {
            process.kill(n, "SIGTERM");
          } catch {
          }
        }
    } catch {
    }
}
function Ge() {
  v && v.pid && (R(v.pid), v = null);
  const n = l.join(d.getPath("home"), ".openpa", "install.pid");
  try {
    const e = parseInt(p.readFileSync(n, "utf8").trim(), 10);
    e > 0 && R(e);
    try {
      p.unlinkSync(n);
    } catch {
    }
  } catch {
  }
  A && A.pid && (R(A.pid), A = null), C && C.pid && (R(C.pid), C = null);
}
let y = null;
function Je() {
  if (!d.isPackaged) return;
  try {
    y = require("electron-updater").autoUpdater;
  } catch (e) {
    console.warn("[main] electron-updater unavailable, skipping auto-update:", e);
    return;
  }
  y.channel = _, y.autoDownload = !1, y.autoInstallOnAppQuit = !0;
  const n = (e, t = {}) => {
    qe("openpa:updater:status", { status: e, ...t });
  };
  y.on("checking-for-update", () => n("checking")), y.on("update-available", (e) => n("available", { info: e })), y.on("update-not-available", (e) => n("up_to_date", { info: e })), y.on("error", (e) => n("error", { error: String(e) })), y.on("download-progress", (e) => n("downloading", { progress: e })), y.on("update-downloaded", (e) => n("ready", { info: e })), h.autoUpdate !== !1 && y.checkForUpdates().catch((e) => {
    console.warn("[main] initial update check failed:", e);
  });
}
w.handle("openpa:updater:check", async () => {
  if (!y) return { status: "unavailable" };
  try {
    const n = await y.checkForUpdates();
    return n != null && n.updateInfo ? { status: "available", info: n.updateInfo } : { status: "up_to_date" };
  } catch (n) {
    return { status: "error", error: String(n) };
  }
});
w.handle("openpa:updater:download", async () => {
  if (!y) return { ok: !1, error: "updater unavailable" };
  try {
    return await y.downloadUpdate(), { ok: !0 };
  } catch (n) {
    return { ok: !1, error: String(n) };
  }
});
w.handle("openpa:updater:install", () => y ? (setImmediate(() => y.quitAndInstall(!1, !0)), { ok: !0 }) : { ok: !1, error: "updater unavailable" });
process.platform === "win32" && d.setAppUserModelId(d.isPackaged ? "openpa-ui.client" : "openpa-ui.client.dev");
const Ke = d.requestSingleInstanceLock();
Ke ? (d.on("window-all-closed", () => {
}), d.on("before-quit", () => {
  Ge();
}), d.on("second-instance", (n, e) => {
  const t = e.find((r) => r.startsWith("--open="));
  if (t) {
    const r = t.slice(7);
    if (r === "main" || r === "settings" || r === "vnc") {
      b(r);
      return;
    }
    if (r === "processes" || r === "events" || r === "channels") {
      $(r);
      return;
    }
  }
  ee();
}), d.whenReady().then(async () => {
  if (h = Te(), g = Oe(), Le(), !G()) {
    try {
      await Se.defaultSession.clearStorageData({
        storages: ["localstorage", "indexdb", "cookies", "serviceworkers"]
      }), console.log("[openpa] first-run detected: cleared renderer storage");
    } catch (n) {
      console.warn("[openpa] failed to clear renderer storage", n);
    }
    g = {
      ...D,
      tokens: {},
      loggedInProfiles: [],
      reasoningEnabled: {}
    };
    try {
      X(g);
    } catch {
    }
  }
  ze(), ve(), be(), b("main"), Je(), G() && (async () => (await Z()).ok && (await ne(), ye()))();
})) : d.quit();
export {
  on as MAIN_DIST,
  fe as RENDERER_DIST,
  J as VITE_DEV_SERVER_URL
};
