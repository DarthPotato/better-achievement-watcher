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
