# -*- coding: utf-8 -*-

# Copyright 1996-2015 PSERC. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

# Copyright (c) 2016-2018 by University of Kassel and Fraunhofer Institute for Energy Economics
# and Energy System Technology (IEE), Kassel. All rights reserved.



"""Runs a power flow.
"""

from time import time

from numpy import flatnonzero as find, r_, zeros, pi, exp, argmax, real

from pandapower.idx_bus import PD, QD, VM, VA, BUS_TYPE, PQ, REF
from pandapower.idx_gen import PG, QG, VG, QMAX, QMIN, GEN_BUS, GEN_STATUS
from pandapower.pf.bustypes import bustypes
from pandapower.pf.makeSbus import makeSbus
from pandapower.pf.pfsoln_pypower import pfsoln
from pandapower.pf.run_newton_raphson_pf import _run_dc_pf
from pandapower.pf.ppci_variables import _get_pf_variables_from_ppci, _store_results_from_pf_in_ppci
try:
    from pypower.makeB import makeB
    from pypower.ppoption import ppoption
    from pypower.fdpf import fdpf
    from pypower.gausspf import gausspf
except ImportError:
    raise ImportError("Cannot import PYPOWER functions. Select a different solver, for example nr, or install PYPOWER")

try:
    import pplog as logging
except ImportError:
    import logging

logger = logging.getLogger(__name__)


def _runpf_pypower(ppci, options, **kwargs):
    """
    This is a modified version* of runpf() to run the algorithms gausspf and fdpf from PYPOWER.
    See PYPOWER documentation for more information.

    * mainly the verbose functions and ext2int() int2ext() were deleted
    """

    ##-----  run the power flow  -----
    t0 = time()
    # ToDo: Options should be extracted in every subfunction not here...
    init_va_degree, ac, numba, recycle, ppopt = _get_options(options, **kwargs)

    if ac:  # AC formulation
        if init_va_degree == "dc":
            ppci = _run_dc_pf(ppci)
            success = True

        ppci, success, bus, gen, branch = _ac_runpf(ppci, ppopt, numba, recycle)
    else:  ## DC formulation
        ppci = _run_dc_pf(ppci)
        success = True

    et = time() - t0
    ppci = _store_results_from_pf_in_ppci(ppci, bus, gen, branch, success, None, et)
    return ppci, success


def _get_options(options, **kwargs):
    init_va_degree = options["init_va_degree"]
    ac = options["ac"]
    recycle = options["recycle"]
    numba = options["numba"]
    enforce_q_lims = options["enforce_q_lims"]
    tolerance_kva = options["tolerance_kva"]
    algorithm = options["algorithm"]
    max_iteration = options["max_iteration"]

    # algorithms implemented within pypower
    algorithm_pypower_dict = {'nr': 1, 'fdbx': 2, 'fdxb': 3, 'gs': 4}

    ppopt = ppoption(ENFORCE_Q_LIMS=enforce_q_lims, PF_TOL=tolerance_kva * 1e-3,
                     PF_ALG=algorithm_pypower_dict[algorithm], **kwargs)
    ppopt['PF_MAX_IT'] = max_iteration
    ppopt['PF_MAX_IT_GS'] = max_iteration
    ppopt['PF_MAX_IT_FD'] = max_iteration
    ppopt['VERBOSE'] = 0
    return init_va_degree, ac, numba, recycle, ppopt


def _ac_runpf(ppci, ppopt, numba, recycle):
    numba, makeYbus = _import_numba_extensions_if_flag_is_true(numba)
    if ppopt["ENFORCE_Q_LIMS"]:
        return _run_ac_pf_with_qlims_enforced(ppci, recycle, makeYbus, ppopt)
    else:
        return _run_ac_pf_without_qlims_enforced(ppci, recycle, makeYbus, ppopt)


def _import_numba_extensions_if_flag_is_true(numba):
    ## check if numba is available and the corresponding flag
    if numba:
        try:
            from numba import _version as nb_version
            # get numba Version (in order to use it it must be > 0.25)
            nb_version = float(nb_version.version_version[:4])

            if nb_version < 0.25:
                logger.warning('Warning: Numba version too old -> Upgrade to a version > 0.25. Numba is disabled\n')
                numba = False

        except ImportError:
            # raise UserWarning('numba cannot be imported. Call runpp() with numba=False!')
            logger.warning('Warning: Numba cannot be imported. Numba is disabled. Call runpp() with Numba=False!\n')
            numba = False

    if numba:
        from pandapower.pf.makeYbus import makeYbus
    else:
        from pandapower.pf.makeYbus_pypower import makeYbus

    return numba, makeYbus


def _get_Y_bus(ppci, recycle, makeYbus, baseMVA, bus, branch):
    if recycle["Ybus"] and ppci["internal"]["Ybus"].size:
        Ybus, Yf, Yt = ppci["internal"]['Ybus'], ppci["internal"]['Yf'], ppci["internal"]['Yt']
    else:
        ## build admittance matrices
        Ybus, Yf, Yt = makeYbus(baseMVA, bus, branch)
        if recycle["Ybus"]:
            ppci["internal"]['Ybus'], ppci["internal"]['Yf'], ppci["internal"]['Yt'] = Ybus, Yf, Yt

    return ppci, Ybus, Yf, Yt


def _run_ac_pf_without_qlims_enforced(ppci, recycle, makeYbus, ppopt):
    baseMVA, bus, gen, branch, ref, pv, pq, _, gbus, V0 = _get_pf_variables_from_ppci(ppci)

    ppci, Ybus, Yf, Yt = _get_Y_bus(ppci, recycle, makeYbus, baseMVA, bus, branch)

    ## compute complex bus power injections [generation - load]
    Sbus = makeSbus(baseMVA, bus, gen)

    ## run the power flow
    V, success = _call_power_flow_function(baseMVA, bus, branch, Ybus, Sbus, V0, ref, pv, pq, ppopt)

    ## update data matrices with solution
    bus, gen, branch = pfsoln(baseMVA, bus, gen, branch, Ybus, Yf, Yt, V, ref)

    return ppci, success, bus, gen, branch


def _run_ac_pf_with_qlims_enforced(ppci, recycle, makeYbus, ppopt):
    baseMVA, bus, gen, branch, ref, pv, pq, on, gbus, V0 = _get_pf_variables_from_ppci(ppci)

    qlim = ppopt["ENFORCE_Q_LIMS"]
    limited = []  ## list of indices of gens @ Q lims
    fixedQg = zeros(gen.shape[0])  ## Qg of gens at Q limits

    while True:
        ppci, success, bus, gen, branch = _run_ac_pf_without_qlims_enforced(ppci, recycle, makeYbus, ppopt)

        ## find gens with violated Q constraints
        gen_status = gen[:, GEN_STATUS] > 0
        qg_max_lim = gen[:, QG] > gen[:, QMAX]
        qg_min_lim = gen[:, QG] < gen[:, QMIN]

        mx = find(gen_status & qg_max_lim)
        mn = find(gen_status & qg_min_lim)

        if len(mx) > 0 or len(mn) > 0:  ## we have some Q limit violations
            # No PV generators
            if len(pv) == 0:
                success = 0
                break

            ## one at a time?
            if qlim == 2:  ## fix largest violation, ignore the rest
                k = argmax(r_[gen[mx, QG] - gen[mx, QMAX],
                              gen[mn, QMIN] - gen[mn, QG]])
                if k > len(mx):
                    mn = mn[k - len(mx)]
                    mx = []
                else:
                    mx = mx[k]
                    mn = []

                    ## save corresponding limit values
            fixedQg[mx] = gen[mx, QMAX]
            fixedQg[mn] = gen[mn, QMIN]
            mx = r_[mx, mn].astype(int)

            ## convert to PQ bus
            gen[mx, QG] = fixedQg[mx]  ## set Qg to binding
            for i in mx:  ## [one at a time, since they may be at same bus]
                gen[i, GEN_STATUS] = 0  ## temporarily turn off gen,
                bi = gen[i, GEN_BUS].astype(int)  ## adjust load accordingly,
                bus[bi, [PD, QD]] = (bus[bi, [PD, QD]] - gen[i, [PG, QG]])

            if len(ref) > 1 and any(bus[gen[mx, GEN_BUS].astype(int), BUS_TYPE] == REF):
                raise ValueError('Sorry, pandapower cannot enforce Q '
                                 'limits for slack buses in systems '
                                 'with multiple slacks.')

            bus[gen[mx, GEN_BUS].astype(int), BUS_TYPE] = PQ  ## & set bus type to PQ

            ## update bus index lists of each type of bus
            ref, pv, pq = bustypes(bus, gen)

            limited = r_[limited, mx].astype(int)
        else:
            break  ## no more generator Q limits violated

    if len(limited) > 0:
        ## restore injections from limited gens [those at Q limits]
        gen[limited, QG] = fixedQg[limited]  ## restore Qg value,
        for i in limited:  ## [one at a time, since they may be at same bus]
            bi = gen[i, GEN_BUS].astype(int)  ## re-adjust load,
            bus[bi, [PD, QD]] = bus[bi, [PD, QD]] + gen[i, [PG, QG]]
            gen[i, GEN_STATUS] = 1  ## and turn gen back on

    return ppci, success, bus, gen, branch


def _call_power_flow_function(baseMVA, bus, branch, Ybus, Sbus, V0, ref, pv, pq, ppopt):
    alg = ppopt["PF_ALG"]
    # alg == 1 was deleted = nr -> moved as own pandapower solver
    if alg == 2 or alg == 3:
        Bp, Bpp = makeB(baseMVA, bus, real(branch), alg)
        V, success, _ = fdpf(Ybus, Sbus, V0, Bp, Bpp, ref, pv, pq, ppopt)
    elif alg == 4:
        V, success, _ = gausspf(Ybus, Sbus, V0, ref, pv, pq, ppopt)
    else:
        raise ValueError('Only PYPOWERS fast-decoupled, and '
                         'Gauss-Seidel power flow algorithms currently '
                         'implemented.\n')

    return V, success
