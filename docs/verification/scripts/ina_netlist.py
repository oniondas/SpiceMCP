"""The instrumentation amplifier netlist, written the way a user (or an assistant) would
write it for start_optimization: {NAME} placeholders and .meas lines. The op-amp model is
copied from a template run (query_run what=netlist), not invented."""

def ina(oa: str) -> str:
    stage = lambda s, ip, inn: f"""X1{s} {ip} n1{s} vcc vee o1{s} SPMCP_OA
X2{s} {inn} n2{s} vcc vee o2{s} SPMCP_OA
R1a{s} o1{s} n1{s} {{R1A}}
R1b{s} o2{s} n2{s} {{R1B}}
Rg{s} n1{s} n2{s} {{RG}}
R2a{s} o1{s} a{s} {{R2A}}
R3a{s} a{s} out{s} {{R3A}}
R2b{s} o2{s} b{s} {{R2B}}
R3b{s} b{s} 0 {{R3B}}
X3{s} b{s} a{s} vcc vee out{s} SPMCP_OA
RL{s} out{s} 0 10k
CL{s} out{s} 0 100p
"""
    loop = f"""X1 inp n1 vcc vee o1 SPMCP_OA
X2 inn n2 vcc vee o2 SPMCP_OA
R1a o1 n1 {{R1A}}
R1b o2 n2 {{R1B}}
Rg n1 n2 {{RG}}
R2a o1 a {{R2A}}
R3a a out {{R3A}}
R2b o2 b {{R2B}}
R3b b 0 {{R3B}}
X3 b fbk vcc vee out SPMCP_OA
RL out 0 10k
CL out 0 100p
Lbrk a fbk 1k
Cinj fbk inj 1k
"""
    return f"""* Three-op-amp instrumentation amplifier, gain 100 (40 dB), +-5 V supply
* differential input (inp, inn) -> out; a second copy driven common-mode for CMRR
Vcc vcc 0 5
Vee vee 0 -5
Vp inp 0 AC 0.5
Vn inn 0 AC 0.5 180
Vc cm 0 AC 1
{stage('', 'inp', 'inn')}{stage('c', 'cm', 'cm')}{oa}.ac dec 50 10 10Meg
.meas AC gain_db FIND mag(V(out)) AT 100
.meas AC bw WHEN mag(V(out))=70.71 FALL=1
.meas AC cm_db FIND mag(V(outc)) AT 60
.end
* Output stage stability: loop gain, feedback broken at X3's inverting input
Vcc vcc 0 5
Vee vee 0 -5
Vp inp 0 0
Vn inn 0 0
{loop}{oa}Vinj inj 0 AC 1
.ac dec 50 1k 1G
.meas AC ugf WHEN mag(V(a))=1 FALL=1
.meas AC pm FIND V(a) WHEN mag(V(a))=1 FALL=1
.end
"""
