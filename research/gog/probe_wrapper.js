// Research probe: what does GOG's Steam wrapper (steam_api64.dll) return for FindLeaderboard, and does it reach the SDK? Logs only.
function log(msg) { send({ type: 'log', msg }); }

const wt = setInterval(() => {
  const mod = Process.findModuleByName('steam_api64.dll');
  if (!mod) return;
  clearInterval(wt);
  log('steam_api64.dll at ' + mod.base + ' size 0x' + mod.size.toString(16));
  const names = mod.enumerateExports().filter(e => /FindLeaderboard|BLoggedOn|ISteamUserStats$|SteamUserStats/.test(e.name));
  log('exports: ' + names.map(e => e.name).join(', '));
  for (const e of names.filter(e => /FindLeaderboard/.test(e.name))) {
    Interceptor.attach(e.address, {
      onEnter(args) { this.n = ''; try { this.n = args[1].readCString(); } catch (x) {} log(e.name + '("' + this.n + '")'); },
      onLeave(ret) { log(e.name + ' -> ' + ret); },
    });
  }
  // the wrapper's own implementation (FUN_180086dc0 in this build)
  const impl = mod.base.add(0x86dc0);
  Interceptor.attach(impl, {
    onEnter(args) { log('wrapper FindLeaderboard impl entered'); },
    onLeave(ret) { log('wrapper FindLeaderboard impl -> ' + ret); },
  });
}, 50);
