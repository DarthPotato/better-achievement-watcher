// Research hook: lets the Galaxy SDK's online-only calls (leaderboards...) run while the stand-in answers offline.
// Finds the "<api>: not logged on" guard by what it does, not by address, so it is meant to survive SDK builds:
//   1. find the text "%s: not logged on" in the SDK module (Galaxy64.dll / Galaxy.dll / GalaxyPeer64.dll),
//   2. find the code that loads it (a rip-relative lea),
//   3. take the function containing that code (from the exception table),
//   4. if it is a small shared helper (SDK 1.152: FUN_18016ae80), replace it with a no-op.
// Nothing on disk changes and nothing is sent anywhere; the change lives in the game's memory only.

const MODULES = ['Galaxy64.dll', 'Galaxy.dll', 'GalaxyPeer64.dll', 'GalaxyPeer.dll'];
const NEEDLE = '%s: not logged on';
const MAX_HELPER_SIZE = 0x200; // a shared guard is tiny; the per-API inline guards live in big functions

function log(msg) { send({ type: 'log', msg }); }
const keepAlive = [];

// Finds `lea reg, [rip+disp]` instructions (REX.W 8D /r, mod=00 rm=101) whose target is `target`; returns how many.
const scanner = new CModule(`
#include <stdint.h>
#include <stddef.h>
size_t lea_scan(const uint8_t *p, size_t n, const uint8_t *target, uint64_t *out, size_t max) {
  size_t k = 0;
  for (size_t i = 0; i + 7 <= n && k < max; i++) {
    if ((p[i] == 0x48 || p[i] == 0x4c) && p[i + 1] == 0x8d && (p[i + 2] & 0xc7) == 0x05) {
      int32_t disp = *(const int32_t *)(p + i + 3);
      if (p + i + 7 + disp == target) out[k++] = i;
    }
  }
  return k;
}`);
const leaScanNative = new NativeFunction(scanner.lea_scan, 'uint64', ['pointer', 'uint64', 'pointer', 'pointer', 'uint64']);
function leaScan(p, n, target, out, max) { return Number(leaScanNative(p, n, target, out, max)); }

function sections(mod) {
  const base = mod.base;
  const nt = base.add(base.add(0x3c).readU32());
  const count = nt.add(6).readU16();
  const optSize = nt.add(20).readU16();
  const table = nt.add(24 + optSize);
  const out = [];
  for (let i = 0; i < count; i++) {
    const s = table.add(i * 40);
    out.push({
      size: Math.max(s.add(8).readU32(), s.add(16).readU32()),
      rva: s.add(12).readU32(), flags: s.add(36).readU32(),
    });
  }
  return { sections: out, nt };
}

function functionTable(mod, nt) {
  const dir = nt.add(24 + 112 + 3 * 8); // IMAGE_DIRECTORY_ENTRY_EXCEPTION
  const rva = dir.readU32(), size = dir.add(4).readU32();
  return { base: mod.base.add(rva), count: Math.floor(size / 12) };
}

function containing(mod, table, rva) {
  let lo = 0, hi = table.count - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const e = table.base.add(mid * 12);
    const begin = e.readU32(), end = e.add(4).readU32();
    if (rva < begin) hi = mid - 1; else if (rva >= end) lo = mid + 1; else return { begin, end };
  }
  return null;
}

function install(mod) {
  const { sections: secs, nt } = sections(mod);
  const table = functionTable(mod, nt);
  const needle = Array.from(NEEDLE).map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join(' ');
  const strings = [];
  for (const s of secs) {
    if (s.flags & 0x20000000) continue; // code
    for (const hit of Memory.scanSync(mod.base.add(s.rva), s.size, needle)) strings.push(hit.address);
  }
  if (!strings.length) { log(mod.name + ': no "' + NEEDLE + '" text'); return false; }

  // Native scan (a JS loop over 10 MB takes seconds, and the game would call FindLeaderboard before we patch).
  const found = new Map(); // function rva -> {size, refs}
  const out = Memory.alloc(8 * 64);
  for (const s of secs) {
    if (!(s.flags & 0x20000000)) continue;
    for (const str of strings) {
      const n = leaScan(mod.base.add(s.rva), s.size, str, out, 64);
      for (let k = 0; k < n; k++) {
        const fn = containing(mod, table, s.rva + out.add(k * 8).readU64().toNumber());
        if (!fn) continue;
        if (!found.has(fn.begin)) found.set(fn.begin, { size: fn.end - fn.begin, refs: 0 });
        found.get(fn.begin).refs++;
      }
    }
  }
  let patched = 0;
  for (const [begin, info] of found) {
    const where = mod.name + '+0x' + begin.toString(16);
    if (info.size > MAX_HELPER_SIZE) { log(where + ': guard is inline in a big function (' + info.size + ' bytes), left alone'); continue; }
    const noop = new NativeCallback(function (obj, api) {
      let name = '?';
      try { name = api.readCString(); } catch (e) {}
      log('guard skipped for ' + name);
    }, 'void', ['pointer', 'pointer']);
    keepAlive.push(noop); // a collected NativeCallback would leave the game calling freed code
    Interceptor.replace(mod.base.add(begin), noop);
    log(where + ': "not logged on" guard helper (' + info.size + ' bytes) replaced with a no-op');
    patched++;
  }
  if (!patched) log(mod.name + ': found the text but no small shared helper; nothing patched');
  return patched > 0;
}

// Diagnostics: report C++ exceptions thrown from inside the SDK (which function, which text).
function traceThrows(mod) {
  const sym = Module.findExportByName(null, '_CxxThrowException');
  if (!sym) { log('no _CxxThrowException export to trace'); return; }
  Interceptor.attach(sym, {
    onEnter(args) {
      const ret = this.returnAddress;
      if (ret.compare(mod.base) < 0 || ret.compare(mod.base.add(mod.size)) >= 0) return;
      let text = '';
      try { // the SDK's error objects keep their message as a std::string a little into the object
        for (let off = 0; off < 0x60 && !text; off += 8) {
          const p = args[0].add(off).readPointer();
          const s = p.readCString(200);
          if (s && s.length > 6 && /[a-zA-Z]{4}/.test(s)) text = s;
        }
      } catch (e) {}
      log('SDK throws from ' + mod.name + '+0x' + ret.sub(mod.base).toString(16) + (text ? ' : ' + text : ''));
    },
  });
}

const done = new Set();
const timer = setInterval(() => {
  for (const name of MODULES) {
    if (done.has(name)) continue;
    const mod = Process.findModuleByName(name);
    if (!mod) continue;
    done.add(name);
    log('found ' + name + ' at ' + mod.base);
    try { traceThrows(mod); } catch (e) { log(name + ': throw trace failed: ' + e); }
    try { install(mod); } catch (e) { log(name + ': hook failed: ' + e); }
  }
}, 50);
log('waiting for ' + MODULES.join(' / '));
