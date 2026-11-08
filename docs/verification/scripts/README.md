# Verification scripts

Independent re-runs of the claims in `docs/final/SpiceMCP_Engineering_Report.pdf`.
Each script talks to SpiceMCP through its MCP interface (or, for single-claim
testbenches, through the simulator directly) and writes JSON to `docs/verification/results/`.

    pip install -e ".[dev]"
    cd docs/verification/scripts
    python t_tools.py                    # tool count and description lengths
    python t_templates.py ltspice 40     # 5 templates: start, tune, finish, yield (40 boards)
    python t_ina.py ltspice 40           # the new instrumentation amplifier, prompt to parts list
    python ck_claims.py ltspice          # Sallen-Key at 1 MHz, LDO phase margin, op-amp, buck step
    python sch_check.py                  # drawings, read back by LTspice, compared pin by pin

Use `ngspice` in place of `ltspice` on Linux.
