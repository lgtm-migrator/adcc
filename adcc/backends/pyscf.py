#!/usr/bin/env python3
## vi: tabstop=4 shiftwidth=4 softtabstop=4 expandtab
## ---------------------------------------------------------------------
##
## Copyright (C) 2018 by the adcc authors
##
## This file is part of adcc.
##
## adcc is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published
## by the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## adcc is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with adcc. If not, see <http://www.gnu.org/licenses/>.
##
## ---------------------------------------------------------------------
import numpy as np

from pyscf import ao2mo, gto, scf

from adcc.misc import cached_property

from .InvalidReference import InvalidReference
from .eri_build_helper import EriBuilder

from libadcc import HartreeFockProvider


class PyScfOperatorIntegralProvider:
    def __init__(self, scfres):
        self.scfres = scfres
        self.backend = "pyscf"

    @cached_property
    def electric_dipole(self):
        return list(self.scfres.mol.intor_symmetric('int1e_r', comp=3))


# TODO: refactor ERI builder to be more general
# IntegralBuilder would be good
class PyScfEriBuilder(EriBuilder):
    def __init__(
        self, scfres, n_orbs, n_orbs_alpha, n_alpha, n_beta, restricted
    ):
        self.scfres = scfres
        if restricted:
            self.mo_coeff = (self.scfres.mo_coeff,
                             self.scfres.mo_coeff)
        else:
            self.mo_coeff = self.scfres.mo_coeff
        super().__init__(n_orbs, n_orbs_alpha, n_alpha, n_beta, restricted)

    @property
    def coefficients(self):
        return {
            "Oa": self.mo_coeff[0][:, :self.n_alpha],
            "Ob": self.mo_coeff[1][:, :self.n_beta],
            "Va": self.mo_coeff[0][:, self.n_alpha:],
            "Vb": self.mo_coeff[1][:, self.n_beta:],
        }

    def compute_mo_eri(self, block, coeffs, use_cache=True):
        if block in self.eri_cache and use_cache:
            return self.eri_cache[block]
        sizes = [i.shape[1] for i in coeffs]
        eri = ao2mo.general(self.scfres.mol, coeffs,
                            compact=False).reshape(sizes[0], sizes[1],
                                                   sizes[2], sizes[3])
        self.eri_cache[block] = eri
        return eri


class PyScfHFProvider(HartreeFockProvider):
    """
        This implementation is only valid
        if no orbital reordering is required.
    """
    def __init__(self, scfres):
        # Do not forget the next line,
        # otherwise weird errors result
        super().__init__()
        self.scfres = scfres
        self.eri_ffff = None
        n_alpha, n_beta = scfres.mol.nelec
        self.eri_builder = PyScfEriBuilder(
            self.scfres, self.n_orbs, self.n_orbs_alpha, n_alpha, n_beta,
            self.restricted
        )
        self.operator_integral_provider = PyScfOperatorIntegralProvider(
            self.scfres
        )

        if not self.restricted:
            assert self.scfres.mo_coeff[0].shape[1] == \
                self.scfres.mo_coeff[1].shape[1]

    def get_backend(self):
        return "pyscf"

    def get_conv_tol(self):
        if self.scfres.conv_tol_grad is None:
            conv_tol_grad = np.sqrt(self.scfres.conv_tol)
        else:
            conv_tol_grad = self.scfres.conv_tol_grad
        conv_tol = max(10 * self.scfres.conv_tol, conv_tol_grad)
        return conv_tol

    def get_restricted(self):
        if isinstance(self.scfres.mo_occ, list):
            restricted = len(self.scfres.mo_occ) < 2
        elif isinstance(self.scfres.mo_occ, np.ndarray):
            restricted = self.scfres.mo_occ.ndim < 2
        else:
            raise InvalidReference("Unusual pyscf SCF class encountered. Could not "
                                   "determine restricted / unrestricted.")
        return restricted

    def get_energy_scf(self):
        return float(self.scfres.e_tot)

    def get_spin_multiplicity(self):
        # Note: In the pyscf world spin is 2S, so the multiplicity
        #       is spin + 1
        return int(self.scfres.mol.spin) + 1

    def get_n_orbs_alpha(self):
        if self.restricted:
            return self.scfres.mo_coeff.shape[1]
        else:
            return self.scfres.mo_coeff[0].shape[1]

    def get_n_bas(self):
        return int(self.scfres.mol.nao_nr())

    def get_nuclear_multipole(self, order):
        charges = self.scfres.mol.atom_charges()
        if order == 0:
            # The function interface needs to be a np.array on return
            return np.array([np.sum(charges)])
        elif order == 1:
            coords = self.scfres.mol.atom_coords()
            return np.einsum('i,ix->x', charges, coords)
        else:
            raise NotImplementedError("get_nuclear_multipole with order > 1")

    def fill_occupation_f(self, out):
        if self.restricted:
            out[:] = np.hstack((self.scfres.mo_occ / 2,
                                self.scfres.mo_occ / 2))
        else:
            out[:] = np.hstack((self.scfres.mo_occ[0],
                                self.scfres.mo_occ[1]))

    def fill_orbcoeff_fb(self, out):
        if self.restricted:
            mo_coeff = (self.scfres.mo_coeff,
                        self.scfres.mo_coeff)
        else:
            mo_coeff = self.scfres.mo_coeff
        out[:] = np.transpose(
            np.hstack((mo_coeff[0], mo_coeff[1]))
        )

    def fill_orben_f(self, out):
        if self.restricted:
            out[:] = np.hstack((self.scfres.mo_energy,
                                self.scfres.mo_energy))
        else:
            out[:] = np.hstack((self.scfres.mo_energy[0],
                                self.scfres.mo_energy[1]))

    def fill_fock_ff(self, slices, out):
        diagonal = np.empty(self.n_orbs)
        self.fill_orben_f(diagonal)
        out[:] = np.diag(diagonal)[slices]

    def fill_eri_ffff(self, slices, out):
        if self.eri_ffff is None:
            self.eri_ffff = self.eri_builder.build_full_eri_ffff()
        out[:] = self.eri_ffff[slices]

    def fill_eri_phys_asym_ffff(self, slices, out):
        self.eri_builder.fill_slice(slices, out)

    def has_eri_phys_asym_ffff(self):
        return True

    def flush_cache(self):
        self.eri_builder.flush_cache()
        self.eri_ffff = None


def import_scf(scfres):
    # TODO The error messages here could be a bit more verbose

    if not isinstance(scfres, scf.hf.SCF):
        raise InvalidReference("Unsupported type for backends.pyscf.import_scf.")

    if not scfres.converged:
        raise InvalidReference("Cannot start an adc calculation on top of an SCF, "
                               "which is not yet converged. Did you forget to run "
                               "the kernel() or the scf() function of the pyscf scf "
                               "object?")

    # TODO Check for point-group symmetry,
    #      check for density-fitting or choleski

    return PyScfHFProvider(scfres)


def run_hf(xyz, basis, charge=0, multiplicity=1, conv_tol=1e-12,
           conv_tol_grad=1e-8, max_iter=150):
    mol = gto.M(
        atom=xyz,
        basis=basis,
        unit="Bohr",
        # spin in the pyscf world is 2S
        spin=multiplicity - 1,
        charge=charge,
        # Disable commandline argument parsing in pyscf
        parse_arg=False,
        dump_input=False,
        verbose=0,
    )
    mf = scf.HF(mol)
    mf.conv_tol = conv_tol
    mf.conv_tol_grad = conv_tol_grad
    mf.max_cycle = max_iter
    # since we want super tight convergence for tests,
    # tweak the options for non-RHF systems
    if multiplicity != 1:
        mf.max_cycle += 500
        mf.diis = scf.EDIIS()
        mf.diis_space = 3
        mf = scf.addons.frac_occ(mf)
    mf.kernel()
    return mf


def run_core_hole(xyz, basis, charge=0, multiplicity=1,
                  conv_tol=1e-12, conv_tol_grad=1e-8, max_iter=150):
    mol = gto.M(
        atom=xyz,
        basis=basis,
        unit="Bohr",
        # spin in the pyscf world is 2S
        spin=multiplicity - 1,
        charge=charge,
        # Disable commandline argument parsing in pyscf
        parse_arg=False,
        dump_input=False,
        verbose=0,
    )

    # First normal run
    mf = scf.UHF(mol)
    mf.conv_tol = conv_tol
    mf.conv_tol_grad = conv_tol_grad
    mf.max_cycle = max_iter
    # since we want super tight convergence for tests,
    # tweak the options for non-RHF systems
    if multiplicity != 1:
        mf.max_cycle += 500
        mf.diis = scf.EDIIS()
        mf.diis_space = 3
        mf = scf.addons.frac_occ(mf)
    mf.kernel()

    # make beta core hole
    mo0 = tuple(c.copy() for c in mf.mo_coeff)
    occ0 = tuple(o.copy() for o in mf.mo_occ)
    occ0[1][0] = 0.0
    dm0 = mf.make_rdm1(mo0, occ0)

    # Run second SCF with MOM
    mf_chole = scf.UHF(mol)
    scf.addons.mom_occ_(mf_chole, mo0, occ0)
    mf_chole.conv_tol = conv_tol
    mf_chole.conv_tol_grad = conv_tol_grad
    mf_chole.max_cycle += 500
    mf_chole.diis = scf.EDIIS()
    mf_chole.diis_space = 3
    mf_chole.kernel(dm0)
    return mf_chole
