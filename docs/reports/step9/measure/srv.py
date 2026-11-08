# Server wrapper used only for measurement: the simulate tool hardcodes LTspice, so on a
# Linux box every simulation is routed to ngspice. Answer shapes are unchanged.
import spicemcp.server as S
_o = S.sim.simulate
def _f(netlist, params, cfg, *a, **k):
    cfg = dict(cfg or {}); cfg["backend"] = "ngspice"; return _o(netlist, params, cfg, *a, **k)
S.sim.simulate = _f
_s = S.sim.simulate_sweep
def _g(netlist, p, v, w, stem="sweep", sim_config=None):
    c = dict(sim_config or {}); c["backend"] = "ngspice"; return _s(netlist, p, v, w, stem, c)
S.sim.simulate_sweep = _g
S.mcp.run()
