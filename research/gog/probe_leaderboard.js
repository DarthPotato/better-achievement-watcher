// Research probe: what does the SDK do when the game calls FindLeaderboard? (logs only, changes nothing)
function log(msg) { send({ type: 'log', msg }); }

const t = setInterval(() => {
  const mod = Process.findModuleByName('Galaxy64.dll');
  if (!mod) return;
  clearInterval(t);
  const ex = mod.enumerateExports();
  const stats = ex.find(e => /\?Stats@api@galaxy@@YAPEAVIStats@12@XZ/.test(e.name));
  const getError = ex.find(e => /\?GetError@api@galaxy@@YAPEBVIError@12@XZ/.test(e.name));
  log('exports: ' + ex.length + ', Stats=' + (stats && stats.name) + ', GetError=' + (getError && getError.name));
  if (!stats) { log(ex.map(e => e.name).filter(n => /Stats|GetError/.test(n)).join('\n')); return; }
  // IStats vtable slot +0x110 is FindLeaderboard (taken from the wrapper's call).
  const iface = new NativeFunction(stats.address, 'pointer', [])();
  const vtable = iface.readPointer();
  const target = vtable.add(0x110).readPointer();
  log('IStats::FindLeaderboard at ' + mod.name + '+0x' + target.sub(mod.base).toString(16));
  Interceptor.attach(target, {
    onEnter(args) { let n = '?'; try { n = args[1].readCString(); } catch (e) {} log('FindLeaderboard("' + n + '") called'); },
    onLeave() {
      if (!getError) return;
      const err = new NativeFunction(getError.address, 'pointer', [])();
      if (err.isNull()) { log('FindLeaderboard: no error'); return; }
      let msg = '', name = '';
      try { const f = err.readPointer().add(0x18).readPointer(); msg = new NativeFunction(f, 'pointer', ['pointer'])(err).readCString(); } catch (e) { msg = '(could not read: ' + e + ')'; }
      try { const f = err.readPointer().add(0x10).readPointer(); name = new NativeFunction(f, 'pointer', ['pointer'])(err).readCString(); } catch (e) {}
      log('FindLeaderboard error: ' + name + ' / ' + msg);
    },
  });
}, 50);

// Diagnostic for the ~5 minute crash: log every "specific listener" dispatch (Galaxy64 1.152.10 only, offset from Ghidra).
const dt = setInterval(() => {
  const mod = Process.findModuleByName('Galaxy64.dll');
  if (!mod) return;
  clearInterval(dt);
  if (mod.size !== 14495744) { log('dispatch probe skipped: other Galaxy64 build'); return; }
  Interceptor.attach(mod.base.add(0x198330), { onEnter() { log('listener dispatch (type 12) at ' + new Date().toISOString()); } });
}, 50);

// Show what the SDK itself logs (it has no log file). galaxy::api::Logger() returns an object whose vtable slots
// +0x18 / +0x28 / +0x30 are the info / warning / error printf-style calls (as used by the SDK's own code).
const lt = setInterval(() => {
  const mod = Process.findModuleByName('Galaxy64.dll');
  if (!mod) return;
  clearInterval(lt);
  const ex = mod.enumerateExports().find(e => /\?Logger@api@galaxy@@/.test(e.name));
  if (!ex) { log('no Logger export'); return; }
  const logger = new NativeFunction(ex.address, 'pointer', [])();
  if (logger.isNull()) { log('Logger() is null'); return; }
  const vt = logger.readPointer();
  const str = p => { try { const s = p.readCString(160); return s && /^[\x20-\x7e]+$/.test(s) ? s : null; } catch (e) { return null; } };
  for (const [off, level] of [[0x18, 'I'], [0x20, '?'], [0x28, 'W'], [0x30, 'E']]) {
    try {
      Interceptor.attach(vt.add(off).readPointer(), { onEnter(a) {
        const f = str(a[1]); if (!f) return;
        log('SDK ' + level + ': ' + f + [a[2], a[3], a[4]].map(x => { const s = str(x); return s ? ' | ' + s : ''; }).join(''));
      } });
    } catch (e) { log('logger hook ' + off + ' failed: ' + e); }
  }
  log('SDK logger hooked');
}, 50);
