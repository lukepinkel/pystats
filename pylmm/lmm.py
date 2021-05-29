# -*- coding: utf-8 -*-
"""
Created on Wed Feb 10 00:01:29 2021

@author: lukepinkel
"""

import re
import patsy
import pandas as pd
import numpy as np # analysis:ignore
import scipy as sp # analysis:ignore
import scipy.sparse as sps # analysis:ignore
from ..utilities.linalg_operations import (dummy, vech, invech, _check_np, 
                                           khatri_rao, sparse_woodbury_inversion,
                                           _check_shape)
from ..utilities.special_mats import lmat, nmat
from ..utilities.numerical_derivs import so_gc_cd, so_fc_cd
from .families import (Binomial, ExponentialFamily, Poisson, NegativeBinomial, Gaussian, InverseGaussian)
from ..utilities.output import get_param_table
from sksparse.cholmod import cholesky

def replace_duplicate_operators(match):
    return match.group()[-1:]

def parse_random_effects(formula):
    matches = re.findall("\([^)]+[|][^)]+\)", formula)
    groups = [re.search("\(([^)]+)\|([^)]+)\)", x).groups() for x in matches]
    frm = formula
    for x in matches:
        frm = frm.replace(x, "")
    fe_form = re.sub("(\+|\-)(\+|\-)+", replace_duplicate_operators, frm)
    return fe_form, groups

def construct_random_effects(groups, data, n_vars):
    re_vars, re_groupings = list(zip(*groups))
    re_vars, re_groupings = set(re_vars), set(re_groupings)
    Zdict = dict(zip(re_vars, [_check_np(patsy.dmatrix(x, data=data, return_type='dataframe')) for x in re_vars]))
    Jdict = dict(zip(re_groupings, [dummy(data[x]) for x in re_groupings]))
    dim_dict = {}
    Z = []
    for x, y in groups:
        Ji, Xi = Jdict[y], Zdict[x]
        dim_dict[y] = {'n_groups':Ji.shape[1], 'n_vars':Xi.shape[1]}
        Zi = khatri_rao(Ji.T, Xi.T).T
        Z.append(Zi)
    Z = np.concatenate(Z, axis=1)
    return Z, dim_dict

def construct_model_matrices(formula, data, return_fe=False):
    fe_form, groups = parse_random_effects(formula)
    yvars, fe_form = re.split("[~]", fe_form)
    fe_form = re.sub("\+$", "", fe_form)
    yvars = re.split(",", re.sub("\(|\)", "", yvars))
    yvars = [x.strip() for x in yvars]
    n_vars = len(yvars)
    Z, dim_dict = construct_random_effects(groups, data, n_vars)
    X = patsy.dmatrix(fe_form, data=data, return_type='dataframe')
    fe_vars = X.columns
    y = data[yvars]
    X, y = _check_np(X), _check_np(y)
    if return_fe:
        return X, Z, y, dim_dict, list(dim_dict.keys()), fe_vars
    else:
        return X, Z, y, dim_dict, list(dim_dict.keys())

def handle_missing(formula, data):
    fe_form, groups = parse_random_effects(formula)
    yvars, fe_form = re.split("[~]", fe_form)
    fe_form = re.sub("\+$", "", fe_form)
    g_vars = [x for y in groups for x in y]
    g_vars = [re.split("[+\-\*:]", x) for x in g_vars]
    re_vars = [x for y in g_vars for x in y]
    fe_vars = re.split("[+\-\*:]", fe_form)
    if type(yvars) is str:
        yvars = [yvars]
    vars_ = set(re_vars + fe_vars+yvars)
    cols = set(data.columns)
    var_subset = vars_.intersection(cols)
    valid_ind = ~data[var_subset].isnull().any(axis=1)
    return valid_ind


def make_theta(dims):
    theta, indices, index_start = [], {}, 0
    dims = dims.copy()
    dims['resid'] = dict(n_groups=0, n_vars=1)
    for key, value in dims.items():
        n_vars = value['n_vars']
        n_params = int(n_vars * (n_vars+1) //2)
        indices[key] = np.arange(index_start, index_start+n_params)
        theta.append(vech(np.eye(n_vars)))
        index_start += n_params
    theta = np.concatenate(theta)
    return theta, indices

def make_gcov(theta, indices, dims, inverse=False):
    Gmats, g_indices, start = {}, {}, 0
    for key, value in dims.items():
        dims_i = dims[key]
        ng, nv = dims_i['n_groups'],  dims_i['n_vars']
        nv2, nvng = nv*nv, nv*ng
        theta_i = theta[indices['theta'][key]]
        if inverse:
            theta_i = np.linalg.inv(invech(theta_i)).reshape(-1, order='F')
        else:
            theta_i = invech(theta_i).reshape(-1, order='F')
        row = np.repeat(np.arange(nvng), nv)
        col = np.repeat(np.arange(ng)*nv, nv2)
        col = col + np.tile(np.arange(nv), nvng)
        data = np.tile(theta_i, ng)
        Gmats[key] = sps.csc_matrix((data, (row, col)))
        g_indices[key] = np.arange(start, start+len(data))
        start += len(data)
    G = sps.block_diag(list(Gmats.values())).tocsc()
    return G, g_indices

def lndet_gmat(theta, dims, indices):
    lnd = 0.0
    for key, value in dims.items():
        dims_i = dims[key]
        ng = dims_i['n_groups']
        Sigma_i = invech(theta[indices['theta'][key]])
        lnd += ng*np.linalg.slogdet(Sigma_i)[1]
    return lnd

def lndet_gmat_chol(theta, dims, indices):
    lnd = 0.0
    for key, value in dims.items():
        dims_i = dims[key]
        ng = dims_i['n_groups']
        theta_i = theta[indices['theta'][key]]
        L_i = invech_chol(theta_i)
        Sigma_i = L_i.dot(L_i.T)            
        lnd += ng*np.linalg.slogdet(Sigma_i)[1]
    return lnd

def invech_chol(lvec):
    p = int(0.5 * ((8*len(lvec) + 1)**0.5 - 1))
    L = np.zeros((p, p))
    a, b = np.triu_indices(p)
    L[(b, a)] = lvec
    return L

def transform_theta(theta, dims, indices):
    for key in dims.keys():
        G = invech(theta[indices['theta'][key]])
        L = np.linalg.cholesky(G)
        theta[indices['theta'][key]] = vech(L)
    return theta
        
    
def inverse_transform_theta(theta, dims, indices):
    for key in dims.keys():
        L = invech_chol(theta[indices['theta'][key]])
        G = L.dot(L.T)
        theta[indices['theta'][key]] = vech(G)
    return theta
        
def get_d2_chol(dim_i):
    p = dim_i['n_vars']
    Lp = lmat(p).A
    T = np.zeros((p, p))
    H = []
    Ip = np.eye(p)
    for j, i in list(zip(*np.triu_indices(p))):
        T[i, j] = 1
        Hij = (Lp.dot(np.kron(Ip, T+T.T)).dot(Lp.T))[np.newaxis]
        H.append(Hij)
        T[i, j] = 0
    H = np.concatenate(H, axis=0)
    return H
        
      
def get_jacmats2(Zs, dims, indices, g_indices, theta):
    start = 0
    jac_mats = {}
    for key, value in dims.items():
        nv, ng =  value['n_vars'], value['n_groups']
        jac_mats[key] = []
        Zi = Zs[:, start:start+ng*nv]
        theta_i = theta[indices[key]]
        nv2, nvng = nv*nv, nv*ng
        row = np.repeat(np.arange(nvng), nv)
        col = np.repeat(np.arange(ng)*nv, nv2)
        col = col + np.tile(np.arange(nv), nvng)
        for i in range(len(theta_i)):
            dtheta_i = np.zeros_like(theta_i)
            dtheta_i[i] = 1.0
            dtheta_i = invech(dtheta_i).reshape(-1, order='F')
            data = np.tile(dtheta_i, ng)
            dGi = sps.csc_matrix((data, (row, col)))
            dVi = Zi.dot(dGi).dot(Zi.T)
            jac_mats[key].append(dVi)
        start+=ng*nv
    jac_mats['resid'] = [sps.eye(Zs.shape[0])]
    return jac_mats
         
def get_jacmats(Zs, dims, indices, g_indices, theta):
    start = 0
    jac_mats = {}
    jac_inds = {}
    for key, value in dims.items():
        nv, ng =  value['n_vars'], value['n_groups']
        jac_mats[key] = []
        theta_i = theta[indices[key]]
        jac_inds[key] = np.arange(start, start+ng*nv)
        nv2, nvng = nv*nv, nv*ng
        row = np.repeat(np.arange(nvng), nv)
        col = np.repeat(np.arange(ng)*nv, nv2)
        col = col + np.tile(np.arange(nv), nvng)
        for i in range(len(theta_i)):
            dtheta_i = np.zeros_like(theta_i)
            dtheta_i[i] = 1.0
            dtheta_i = invech(dtheta_i).reshape(-1, order='F')
            data = np.tile(dtheta_i, ng)
            dGi = sps.csc_matrix((data, (row, col)))
            jac_mats[key].append(dGi)
        start+=ng*nv
    jac_mats['resid'] = [sps.eye(Zs.shape[0])]
    return jac_mats, jac_inds
    

class LMM:
    
    def __init__(self, formula, data, weights=None):
        """
        Parameters
        ----------
        formula : string
            lme4 style formula with random effects specified by terms in 
            parentheses with a bar
            
        data : dataframe
            Dataframe containing data.  Missing values should be dropped 
            manually before passing the dataframe.
            
        weights : ndarray, optional
            Array of model weights. The default is None, which sets the
            weights to one internally.

        Returns
        -------
        None.

        """
        indices = {}
        X, Z, y, dims, levels, fe_vars = construct_model_matrices(formula, data, 
                                                                  return_fe=True)
        theta, theta_indices = make_theta(dims)
    
        indices['theta'] = theta_indices
    
        G, g_indices = make_gcov(theta, indices, dims)
    
        indices['g'] = g_indices
    
    
        XZ, Xty, Zty, yty = np.hstack([X, Z]), X.T.dot(y), Z.T.dot(y), y.T.dot(y)
    
        C, m = sps.csc_matrix(XZ.T.dot(XZ)), sps.csc_matrix(np.vstack([Xty, Zty]))
        M = sps.bmat([[C, m], [m.T, yty]])
        M = M.tocsc()
        self.fe_vars = fe_vars
        self.X, self.Z, self.y, self.dims, self.levels = X, Z, y, dims, levels
        self.XZ, self.Xty, self.Zty, self.yty = XZ, Xty, Zty, yty
        self.C, self.m, self.M = C, m, M
        self.theta, self.theta_chol = theta, transform_theta(theta, dims, indices)
        self.G = G
        self.indices = indices
        self.R = sps.eye(Z.shape[0])
        self.Zs = sps.csc_matrix(Z)
        self.g_derivs, self.jac_inds = get_jacmats(self.Zs, self.dims, 
                                                   self.indices['theta'],
                                                   self.indices['g'], self.theta)
        self.t_indices = list(zip(*np.triu_indices(len(theta))))
        self.elim_mats, self.symm_mats, self.iden_mats = {}, {}, {}
        self.d2g_dchol = {}
        for key in self.levels:
            p = self.dims[key]['n_vars']
            self.elim_mats[key] = lmat(p).A
            self.symm_mats[key] = nmat(p).A
            self.iden_mats[key] = np.eye(p)
            self.d2g_dchol[key] = get_d2_chol(self.dims[key])
        self.bounds = [(0, None) if x==1 else (None, None) for x in self.theta]
        self.bounds_2 = [(1e-6, None) if x==1 else (None, None) for x in self.theta]
        self.zero_mat = sp.sparse.eye(self.X.shape[1])*0.0
        self.zero_mat2 = sp.sparse.eye(1)*0.0
 

        
    def update_mme(self, Ginv, s):
        """
        Parameters
        ----------
        Ginv: sparse matrix
             scipy sparse matrix with inverse covariance block diagonal
            
        s: float
            resid covariance
        
        Returns
        -------
        M: sparse matrix
            updated mixed model matrix
            
        """
        M = self.M.copy()/s
        Omega = sp.sparse.block_diag([self.zero_mat, Ginv, self.zero_mat2])
        M+=Omega
        return M
    
    def update_gmat(self, theta, inverse=False):
        """
        Parameters
        ----------
        theta: ndarray
             covariance parameters on the original scale
            
        inverse: bool
            whether or not to inverse G
        
        Returns
        -------
        G: sparse matrix
            updated random effects covariance
            
        """
        G = self.G
        for key in self.levels:
            ng = self.dims[key]['n_groups']
            theta_i = theta[self.indices['theta'][key]]
            if inverse:
                theta_i = np.linalg.inv(invech(theta_i)).reshape(-1, order='F')
            else:
                theta_i = invech(theta_i).reshape(-1, order='F')
            G.data[self.indices['g'][key]] = np.tile(theta_i, ng)
        return G
        
    def loglike(self, theta, reml=True, use_sw=False):
        """
        Parameters
        ----------
        
        theta: array_like
            The original parameterization of the model parameters
        
        Returns
        -------
        loglike: scalar
            Log likelihood of the model
        """
        Ginv = self.update_gmat(theta, inverse=True)
        M = self.update_mme(Ginv, theta[-1])
        L = np.linalg.cholesky(M.A)
        ytPy = np.diag(L)[-1]**2
        logdetG = lndet_gmat(theta, self.dims, self.indices)
        logdetR = np.log(theta[-1]) * self.Z.shape[0]
        if reml:
            logdetC = np.sum(2*np.log(np.diag(L))[:-1])
            ll = logdetR + logdetC + logdetG + ytPy
        else:
            Rinv = self.R / theta[-1]
            RZ = Rinv.dot(self.Zs)
            Q = Ginv + self.Zs.T.dot(RZ)
            _, logdetV = cholesky(Q).slogdet()
            ll = logdetR + logdetV + logdetG + ytPy
        return ll

    def gradient(self, theta, reml=True, use_sw=False):
        """
        Parameters
        ----------
        theta: array_like
            The original parameterization of the components
        
        Returns
        -------
        gradient: array_like
            The gradient of the log likelihood with respect to the covariance
            parameterization
        
        Notes
        -----

            
        """
        Rinv = self.R / theta[-1]
        Ginv = self.update_gmat(theta, inverse=True)
        RZ = Rinv.dot(self.Zs)
        Q = Ginv + self.Zs.T.dot(RZ)
        M = cholesky(Q).inv()
        W = Rinv - RZ.dot(M).dot(RZ.T)

        WZ = W.dot(self.Zs)
        ZtWZ = self.Zs.T.dot(WZ)
        WX = W.dot(self.X)
        XtWX = WX.T.dot(self.X)
        ZtWX = self.Zs.T.dot(WX)
        U = np.linalg.solve(XtWX, WX.T)
        Py = W.dot(self.y) - WX.dot(U.dot(self.y))
        ZtPy = self.Zs.T.dot(Py)
        grad = []
        for key in (self.levels):
            ind = self.jac_inds[key]
            ZtWZi = ZtWZ[ind][:, ind]
            ZtWXi = ZtWX[ind]
            ZtPyi = ZtPy[ind]
            for dGdi in self.g_derivs[key]:
                g1 = dGdi.dot(ZtWZi).diagonal().sum() 
                g2 = ZtPyi.T.dot(dGdi.dot(ZtPyi))
                if reml:
                    g3 = np.trace(np.linalg.solve(XtWX, ZtWXi.T.dot(dGdi.dot(ZtWXi))))
                else:
                    g3 = 0
                gi = g1 - g2 - g3
                grad.append(gi)
        ZtR = self.Zs.T.dot(Rinv)
        for dR in self.g_derivs['resid']:
            g1 = Rinv.diagonal().sum() - (M.dot((ZtR).dot(dR).dot(ZtR.T))).diagonal().sum()
            g2 = Py.T.dot(Py)
            if reml:
                g3 = np.trace(np.linalg.solve(XtWX, WX.T.dot(WX)))
            else:
                g3 = 0
            gi = g1 - g2 - g3
            grad.append(gi)
        grad = np.concatenate(grad)
        grad = _check_shape(np.array(grad))
        return grad
    
    def hessian(self, theta, reml=True, use_sw=False):
        """
        Parameters
        ----------
        theta: array_like
            The original parameterization of the components
        
        Returns
        -------
        H: array_like
            The hessian of the log likelihood with respect to the covariance
            parameterization
        
        Notes
        -----
        This function has the infrastructure to support more complex residual
        covariances that are yet to be implemented.  

        """
        Ginv = self.update_gmat(theta, inverse=True)
        Rinv = self.R / theta[-1]
        RZ = Rinv.dot(self.Zs)
        Q = Ginv + self.Zs.T.dot(RZ)
        M = cholesky(Q).inv()
        W = Rinv - RZ.dot(M).dot(RZ.T)

        WZ = W.dot(self.Zs)
        WX = W.dot(self.X)
        XtWX = WX.T.dot(self.X)
        ZtWX = self.Zs.T.dot(WX)
        U = np.linalg.solve(XtWX, WX.T)
        ZtP = WZ.T - ZtWX.dot(np.linalg.solve(XtWX, WX.T))
        ZtPZ = self.Zs.T.dot(ZtP.T)
        Py = W.dot(self.y) - WX.dot(U.dot(self.y))
        ZtPy = self.Zs.T.dot(Py)
        PPy = W.dot(Py) - WX.dot(U.dot(Py))
        ZtPPy =  self.Zs.T.dot(PPy)
        H = np.zeros((len(self.theta), len(self.theta)))
        PJ, yPZJ, ZPJ = [], [], []
        ix = []
        for key in (self.levels):
            ind = self.jac_inds[key]
            ZtPZi = ZtPZ[ind]
            ZtPyi = ZtPy[ind]
            ZtPi = ZtP[ind]
            for i in range(len(self.g_derivs[key])):
                Gi = self.g_derivs[key][i]
                PJ.append(Gi.dot(ZtPZi))
                yPZJ.append(Gi.dot(ZtPyi))
                ZPJ.append((Gi.dot(ZtPi)).T)
                ix.append(ind)
            
        t_indices = list(zip(*np.triu_indices(len(self.theta)-1)))
        for i, j in t_indices:
            ZtPZij = ZtPZ[ix[i]][:, ix[j]]
            PJi, PJj = PJ[i][:, ix[j]], PJ[j][:, ix[i]]
            yPZJi, JjZPy = yPZJ[i], yPZJ[j]
            Hij = -np.einsum('ij,ji->', PJi, PJj)\
                    + (2 * (yPZJi.T.dot(ZtPZij)).dot(JjZPy))[0]
            H[i, j] = H[j, i] = Hij
        dR = self.g_derivs['resid'][0]
        dRZtP = (dR.dot(ZtP.T))
        for i in range(len(self.theta)-1):
            yPZJi = yPZJ[i]
            ZPJi = ZPJ[i]
            ZtPPyi = ZtPPy[ix[i]]
            H[i, -1] = H[-1, i] = 2*yPZJi.T.dot(ZtPPyi) - np.einsum('ij,ji->', ZPJi.T, dRZtP[:, ix[i]])
        P = W - WX.dot(U)
        H[-1, -1] = Py.T.dot(PPy)*2 - np.einsum("ij,ji->", P, P)
        return H
    
    def update_chol(self, theta, inverse=False):
        """
        Parameters
        ----------
        theta: array_like
            array containing the lower triangular components of the cholesky
            for each random effect covariance
            
        inverse: bool
        
        Returns
        -------
        L_dict: dict of array_like
            Dictionary whose keys and values correspond to level names
            and the corresponding cholesky of the level's random effects 
            covariance
            
        """
        L_dict = {}
        for key in self.levels:
            theta_i = theta[self.indices['theta'][key]]
            L_i = invech_chol(theta_i)
            L_dict[key] = L_i
        return L_dict
    
    def dg_dchol(self, L_dict):
        """
        
        Parameters
        ----------
        
        L_dict: dict of array_like
            Dictionary whose keys and values correspond to level names
            and the corresponding cholesky of the level's random effects 
            covariance
        
        
        Returns
        -------
        
        Jf: dict of array_like
            For each level contains the derivative of the cholesky parameters
            with respect to the covariance
        
        Notes
        -----
        
        Function evaluates the derivative of the cholesky parameterization 
        with respect to the lower triangular components of the covariance
        
        """
        
        Jf = {}
        for key in self.levels:
            L = L_dict[key]
            E = self.elim_mats[key]
            N = self.symm_mats[key]
            I = self.iden_mats[key]
            Jf[key] = E.dot(N.dot(np.kron(L, I))).dot(E.T)
        return Jf
    
    def loglike_c(self, theta_chol, reml=True, use_sw=False):
        """
        Parameters
        ----------
        
        theta_chol: array_like
            The cholesky parameterization of the components
        
        Returns
        -------
        loglike: scalar
            Log likelihood of the model
        """
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.loglike(theta, reml, use_sw)
    
    def gradient_c(self, theta_chol, reml=True, use_sw=False):
        """
        Parameters
        ----------
        
        theta_chol: array_like
            The cholesky parameterization of the components
        
        Returns
        -------
        gradient: array_like
            The gradient of the log likelihood with respect to the covariance
            parameterization
            
        """
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.gradient(theta, reml, use_sw)
    
    
    def hessian_c(self, theta_chol, reml=True):
        """
        Parameters
        ----------
        
        theta_chol: array_like
            The cholesky parameterization of the components
        
        Returns
        -------
        hessian: array_like
            The hessian of the log likelihood with respect to the covariance
            parameterization
            
        """
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.hessian(theta, reml)
    
    def gradient_chol(self, theta_chol, reml=True, use_sw=False):
        """
        Parameters
        ----------
        
        theta_chol: array_like
            The cholesky parameterization of the components
        
        Returns
        -------
        gradient: array_like
            The gradient of the log likelihood with respect to the cholesky
            parameterization
            
        """
        L_dict = self.update_chol(theta_chol)
        Jf_dict = self.dg_dchol(L_dict)
        Jg = self.gradient_c(theta_chol, reml, use_sw)
        Jf = sp.linalg.block_diag(*Jf_dict.values()) 
        Jf = np.pad(Jf, [[0, 1]])
        Jf[-1, -1] = 1
        return Jg.dot(Jf)
    
    def hessian_chol(self, theta_chol, reml=True):
        """
        Parameters
        ----------
        
        theta_chol: array_like
            The cholesky parameterization of the components
        
        Returns
        -------
        hessian: array_like
            The hessian of the log likelihood with respect to the cholesky
            parameterization
            
        """
        L_dict = self.update_chol(theta_chol)
        Jf_dict = self.dg_dchol(L_dict)
        Hq = self.hessian_c(theta_chol, reml)
        Jg = self.gradient_c(theta_chol, reml)
        Hf = self.d2g_dchol
        Jf = sp.linalg.block_diag(*Jf_dict.values()) 
        Jf = np.pad(Jf, [[0, 1]])
        Jf[-1, -1] = 1
        A = Jf.T.dot(Hq).dot(Jf)  
        B = np.zeros_like(Hq)
        
        for key in self.levels:
            ix = self.indices['theta'][key]
            Jg_i = Jg[ix]
            Hf_i = Hf[key]
            C = np.einsum('i,ijk->jk', Jg_i, Hf_i)  
            B[ix, ix[:, None]] += C
        H = A + B
        return H
    
    def _compute_effects(self, theta=None):
        """

        Parameters
        ----------
        theta : ndarray, optional
            Model parameters in the covariance form

        Returns
        -------
        beta : ndarray
            Fixed effects estimated at theta.
        XtViX_inv : ndarray
            Fixed effects covariance matrix.
        u : ndarray
            Random effect estimate at theta.
        G : csc_matrix
            Random effects covariance matrix.
        R : dia_matrix
            Matrix of residual covariance.
        V : csc_matrix
            Model covariance matrix given fixed effects.

        """
        theta = self.theta if theta is None else theta
        G = self.update_gmat(theta, inverse=False).copy()
        R = self.R * theta[-1]
        V = self.Zs.dot(G).dot(self.Zs.T) + R
        Ginv = self.update_gmat(theta, inverse=True)
        Rinv = self.R / theta[-1]
        RZ = Rinv.dot(self.Zs)
        Q = Ginv + self.Zs.T.dot(RZ)
        M = cholesky(Q).inv()
        Vinv = Rinv - RZ.dot(M).dot(RZ.T)

        XtVi = (Vinv.dot(self.X)).T
        XtViX = XtVi.dot(self.X)
        XtViX_inv = np.linalg.inv(XtViX)
        beta = _check_shape(XtViX_inv.dot(XtVi.dot(self.y)))
        fixed_resids = _check_shape(self.y) - _check_shape(self.X.dot(beta))
        Vinvr = Vinv.dot(fixed_resids)
        u = G.dot(self.Zs.T).dot(Vinvr)
        return beta, XtViX_inv, u, G, R, V
    
    def _optimize(self, reml=True, use_grad=True, use_hess=False, opt_kws={}):
        """

        Parameters
        ----------
        use_grad : bool, optional
            If true, the analytic gradient is used during optimization.
            The default is True.
        use_hess : bool, optional
            If true, the analytic hessian is used during optimization.
            The default is False.
        opt_kws : dict, optional
            Dictionary of options to use in scipy.optimize.minimize.
            The default is {}.

        Returns
        -------
        None.

        """
        
        if use_grad:
            default_opt_kws = dict(verbose=0, gtol=1e-6, xtol=1e-6)
            if use_hess:
               hess = self.hessian_chol
            else:
                hess = None
            for key, value in default_opt_kws.items():
                if key not in opt_kws.keys():
                    opt_kws[key] = value
            optimizer = sp.optimize.minimize(self.loglike_c, self.theta, args=(reml,),
                                             jac=self.gradient_chol, hess=hess, 
                                             options=opt_kws, bounds=self.bounds,
                                             method='trust-constr')
        else:
            default_opt_kws = dict(disp=True, gtol=1e-14, ftol=1e-14, 
                                   finite_diff_rel_step='3-point', eps=1e-7,
                                   iprint=99)
            for key, value in default_opt_kws.items():
                if key not in opt_kws.keys():
                    opt_kws[key] = value
            optimizer = sp.optimize.minimize(self.loglike_c, self.theta, 
                                             args=(reml,),bounds=self.bounds_2, 
                                             method='L-BFGS-B',
                                             options=opt_kws)
        theta_chol = optimizer.x
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        return theta, theta_chol, optimizer
        
        
    def _post_fit(self, theta, theta_chol, optimizer, reml=True,
                  use_grad=True, analytic_se=False):
        """

        Parameters
        ----------
        use_grad : bool, optional
            If true and analytic_se is False, the gradient is used in the
            numerical approximation of the hessian. The default is True.
        analytic_se : bool, optional
            If true, then the hessian is used to compute standard errors.
            The default is False.

        Returns
        -------
        None.

        """
        beta, XtWX_inv, u, G, R,  V = self._compute_effects(theta)
        params = np.concatenate([beta, theta])
        re_covs, re_corrs = {}, {}
        for key, value in self.dims.items():
            re_covs[key] = invech(theta[self.indices['theta'][key]].copy())
            C = re_covs[key]
            v = np.diag(np.sqrt(1/np.diag(C)))
            re_corrs[key] = v.dot(C).dot(v)
        
        if analytic_se:
            Htheta = self.hessian(theta)
        elif use_grad:
            Htheta = so_gc_cd(self.gradient, theta)
        else:
            Htheta = so_fc_cd(self.loglike, theta)
        
        self.theta, self.beta, self.u, self.params = theta, beta, u, params
        self.Hinv_beta = XtWX_inv
        self.Hinv_theta = np.linalg.pinv(Htheta/2.0)
        self.se_beta = np.sqrt(np.diag(XtWX_inv))
        self.se_theta = np.sqrt(np.diag(self.Hinv_theta))
        self.se_params = np.concatenate([self.se_beta, self.se_theta])  
        self._G, self._R, self._V = G, R, V
        self.optimizer = optimizer
        self.theta_chol = theta_chol
        if reml:
            self.llconst = (self.X.shape[0] - self.X.shape[1])*np.log(2*np.pi)
        else:
            self.llconst = self.X.shape[0] * np.log(2*np.pi)
        self.lltheta = self.optimizer.fun
        self.ll = (self.llconst + self.lltheta)
        self.llf = self.ll / -2.0
        self.re_covs = re_covs
        self.re_corrs = re_corrs
        if reml:
            n = self.X.shape[0] - self.X.shape[1]
            d = len(self.theta)
        else:
            n = self.X.shape[0]
            d = self.X.shape[1] + len(self.theta)
        self.AIC = self.ll + 2.0 * d
        self.AICC = self.ll + 2 * d * n / (n-d-1)
        self.BIC = self.ll + d * np.log(n)
        self.CAIC = self.ll + d * (np.log(n) + 1)
        sumstats = np.array([self.ll, self.llf, self.AIC, self.AICC,
                             self.BIC, self.CAIC])
        self.sumstats = pd.DataFrame(sumstats, index=['ll', 'llf', 'AIC', 'AICC',
                                                      'BIC', 'CAIC'], columns=['value'])
    
    def predict(self, X=None, Z=None):
        """
        Parameters
        ----------
        X : ndarray, optional
            Model matrix for fixed effects. The default is None.
        Z : ndarray, optional
            Model matrix from random effects. The default is None.

        Returns
        -------
        yhat : ndarray
            Model predictions evaluated at X and Z.

        """
        if X is None:
            X = self.X
        if Z is None:
            Z = self.Z
        yhat = X.dot(self.beta)+Z.dot(self.u)
        return yhat
    
    def fit(self, reml=True, use_grad=True, use_hess=False, analytic_se=False,
            opt_kws={}):
        """
        

        Parameters
        ----------
        use_grad : bool, optional
            If true, the analytic gradient is used during optimization.
            The default is True.
        use_hess : bool, optional
            If true, the analytic hessian is used during optimization.
            The default is False.
        analytic_se : bool, optional
            If true, then the hessian is used to compute standard errors.
            The default is False.
        opt_kws : dict, optional
            Dictionary of options to use in scipy.optimize.minimize.
            The default is {}.

        Returns
        -------
        None.

        """
        theta, theta_chol, optimizer = self._optimize(reml, use_grad, use_hess, 
                                                      opt_kws)
        self._post_fit(theta, theta_chol, optimizer, reml, use_grad, 
                       analytic_se)
        param_names = list(self.fe_vars)
        for level in self.levels:
            for i, j in list(zip(*np.triu_indices(self.dims[level]['n_vars']))):
                param_names.append(f"{level}:G[{i}][{j}]")
        param_names.append("resid_cov")
        self.param_names = param_names
        res = np.vstack((self.params, self.se_params)).T
        res = pd.DataFrame(res, index=param_names, columns=['estimate', 'SE'])
        res['t'] = res['estimate'] / res['SE']
        res['p'] = sp.stats.t(self.X.shape[0]-self.X.shape[1]).sf(np.abs(res['t']))
        self.res = res
        
        
 

class WLMM:
    
    def __init__(self, formula, data, weights=None, fix_resid_cov=False):
        if weights is None:
            weights = np.eye(len(data))
        self.weights = sps.csc_matrix(weights)
        self.weights_inv = sps.csc_matrix(np.linalg.inv(weights))
      
        indices = {}
        X, Z, y, dims, levels = construct_model_matrices(formula, data)
        theta, theta_indices = make_theta(dims)
    
        indices['theta'] = theta_indices
    
        G, g_indices = make_gcov(theta, indices, dims)
    
        indices['g'] = g_indices
    
    
        XZ, Xty, Zty, yty = np.hstack([X, Z]), X.T.dot(y), Z.T.dot(y), y.T.dot(y)
    
        C, m = sps.csc_matrix(XZ.T.dot(XZ)), sps.csc_matrix(np.vstack([Xty, Zty]))
        M = sps.bmat([[C, m], [m.T, yty]])
        M = M.tocsc()
        self.F = sps.csc_matrix(XZ)
        self.X, self.Z, self.y, self.dims, self.levels = X, Z, y, dims, levels
        self.XZ, self.Xty, self.Zty, self.yty = XZ, Xty, Zty, yty
        self.C, self.m, self.M = C, m, M
        self.theta, self.theta_chol = theta, transform_theta(theta, dims, indices)
        self.G = G
        self.indices = indices
        self.R = sps.eye(Z.shape[0])
        self.Zs = sps.csc_matrix(Z)
        self.jac_mats = get_jacmats2(self.Zs, self.dims, self.indices['theta'], 
                                     self.indices['g'], self.theta)
        self.t_indices = list(zip(*np.triu_indices(len(theta))))
        self.elim_mats, self.symm_mats, self.iden_mats = {}, {}, {}
        self.d2g_dchol = {}
        for key in self.levels:
            p = self.dims[key]['n_vars']
            self.elim_mats[key] = lmat(p).A
            self.symm_mats[key] = nmat(p).A
            self.iden_mats[key] = np.eye(p)
            self.d2g_dchol[key] = get_d2_chol(self.dims[key])
        self.bounds = [(0, None) if x==1 else (None, None) for x in self.theta]
        self.fix_resid_cov = fix_resid_cov
        
    def update_mme(self, Ginv, Rinv, s):
        C = sps.csc_matrix(self.F.T.dot(Rinv).dot(self.F))
        m = self.F.T.dot(Rinv).dot(self.y)
        yty = np.array(np.atleast_2d(self.y.T.dot(Rinv.A).dot(self.y)))
        M = sps.bmat([[C, m], [m.T, yty]]).tocsc()
        M[-Ginv.shape[0]-1:-1, -Ginv.shape[0]-1:-1] += Ginv
        return M
    
    def update_gmat(self, theta, inverse=False):
        G = self.G
        for key in self.levels:
            ng = self.dims[key]['n_groups']
            theta_i = theta[self.indices['theta'][key]]
            if inverse:
                theta_i = np.linalg.inv(invech(theta_i)).reshape(-1, order='F')
            else:
                theta_i = invech(theta_i).reshape(-1, order='F')
            G.data[self.indices['g'][key]] = np.tile(theta_i, ng)
        return G
        
    def loglike(self, theta):
        if self.fix_resid_cov:
            s = 1
        else:
            s = theta[-1]
        Ginv = self.update_gmat(theta, inverse=True)
        Rinv = self.weights_inv.dot(self.R /s).dot(self.weights_inv)
        M = self.update_mme(Ginv, Rinv, s)
        logdetG = lndet_gmat(theta, self.dims, self.indices)
        L = np.linalg.cholesky(M.A)
        ytPy = np.diag(L)[-1]**2
        logdetC = np.sum(2*np.log(np.diag(L))[:-1])
        R = (self.weights.dot(self.R * theta[-1]).dot(self.weights))
        logdetR = np.sum(np.log(R.diagonal()))
        ll = logdetR + logdetC + logdetG + ytPy
        return ll
    
    def gradient(self, theta):
        if self.fix_resid_cov:
            s = 1
        else:
            s = theta[-1]
        Ginv = self.update_gmat(theta, inverse=True)
        Rinv = self.weights_inv.dot(self.R / s).dot(self.weights_inv)
        Vinv = sparse_woodbury_inversion(self.Zs, Cinv=Ginv, Ainv=Rinv.tocsc())
        W = (Vinv.dot(self.X))
        XtW = W.T.dot(self.X)
        XtW_inv = np.linalg.inv(XtW)
        P = Vinv - np.linalg.multi_dot([W, XtW_inv, W.T])
        Py = P.dot(self.y)
        grad = []
        for key in (self.levels+['resid']):
            for dVdi in self.jac_mats[key]:
                gi = np.einsum("ij,ji->", dVdi.A, P) - Py.T.dot(dVdi.dot(Py))
                grad.append(gi)
        grad = np.concatenate(grad)
        grad = _check_shape(np.array(grad))
        return grad
    
    def hessian(self, theta):
        if self.fix_resid_cov:
            s = 1
        else:
            s = theta[-1]
        Ginv = self.update_gmat(theta, inverse=True)
        Rinv = self.weights_inv.dot(self.R / s).dot(self.weights_inv)
        Vinv = sparse_woodbury_inversion(self.Zs, Cinv=Ginv, Ainv=Rinv.tocsc())
        W = (Vinv.dot(self.X))
        XtW = W.T.dot(self.X)
        XtW_inv = np.linalg.inv(XtW)
        P = Vinv - np.linalg.multi_dot([W, XtW_inv, W.T])
        Py = P.dot(self.y)
        H = []
        PJ, yPJ = [], []
        for key in (self.levels+['resid']):
            J_list = self.jac_mats[key]
            for i in range(len(J_list)):
                Ji = J_list[i].T
                PJ.append((Ji.dot(P)).T)
                yPJ.append((Ji.dot(Py)).T)
        t_indices = self.t_indices
        for i, j in t_indices:
            PJi, PJj = PJ[i], PJ[j]
            yPJi, JjPy = yPJ[i], yPJ[j].T
            Hij = -(PJi.dot(PJj)).diagonal().sum()\
                        + (2 * (yPJi.dot(P)).dot(JjPy))[0]
            H.append(np.array(Hij[0]))
        H = invech(np.concatenate(H)[:, 0])
        return H
    
    def update_chol(self, theta, inverse=False):
        L_dict = {}
        for key in self.levels:
            theta_i = theta[self.indices['theta'][key]]
            L_i = invech_chol(theta_i)
            L_dict[key] = L_i
        return L_dict
    
    def dg_dchol(self, L_dict):
        Jf = {}
        for key in self.levels:
            L = L_dict[key]
            E = self.elim_mats[key]
            N = self.symm_mats[key]
            I = self.iden_mats[key]
            Jf[key] = E.dot(N.dot(np.kron(L, I))).dot(E.T)
        return Jf
    
    def loglike_c(self, theta_chol):
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.loglike(theta)
    
    def gradient_c(self, theta_chol):
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.gradient(theta)
    
    def hessian_c(self, theta_chol):
        theta = inverse_transform_theta(theta_chol.copy(), self.dims, self.indices)
        theta[-1] = theta_chol[-1]
        return self.hessian(theta)
    
    def gradient_chol(self, theta_chol):
        L_dict = self.update_chol(theta_chol)
        Jf_dict = self.dg_dchol(L_dict)
        Jg = self.gradient_c(theta_chol)
        Jf = sp.linalg.block_diag(*Jf_dict.values()) 
        Jf = np.pad(Jf, [[0, 1]])
        Jf[-1, -1] = 1
        return Jg.dot(Jf)
    
    def hessian_chol(self, theta_chol):
        L_dict = self.update_chol(theta_chol)
        Jf_dict = self.dg_dchol(L_dict)
        Hq = self.hessian_c(theta_chol)
        Jg = self.gradient_c(theta_chol)
        Hf = self.d2g_dchol
        Jf = sp.linalg.block_diag(*Jf_dict.values()) 
        Jf = np.pad(Jf, [[0, 1]])
        Jf[-1, -1] = 1
        A = Jf.T.dot(Hq).dot(Jf)  
        B = np.zeros_like(Hq)
        
        for key in self.levels:
            ix = self.indices['theta'][key]
            Jg_i = Jg[ix]
            Hf_i = Hf[key]
            C = np.einsum('i,ijk->jk', Jg_i, Hf_i)  
            B[ix, ix[:, None]] += C
        H = A + B
        return H
    
    def _compute_effects(self, theta=None):
        G = self.update_gmat(theta, inverse=False).copy()
        Ginv = self.update_gmat(theta, inverse=True)
        R = (self.weights.dot(self.R * theta[-1]).dot(self.weights))
        Rinv = self.weights_inv.dot(self.R / theta[-1]).dot(self.weights_inv)
        V = self.Zs.dot(G).dot(self.Zs.T)+R
        Vinv = sparse_woodbury_inversion(self.Zs, Cinv=Ginv, Ainv=Rinv.tocsc())
        XtVi = (Vinv.dot(self.X)).T
        XtViX = XtVi.dot(self.X)
        XtViX_inv = np.linalg.inv(XtViX)
        beta = _check_shape(XtViX_inv.dot(XtVi.dot(self.y)))
        fixed_resids = _check_shape(self.y) - _check_shape(self.X.dot(beta))
        
        Zt = self.Zs.T
        u = G.dot(Zt.dot(Vinv)).dot(fixed_resids)
        
        return beta, XtViX_inv, u, G, R, Rinv, V, Vinv
    
    def _fit(self, use_hess=False, opt_kws={}):
        default_opt_kws = dict(verbose=0, gtol=1e-6, xtol=1e-6)
        if use_hess:
            hess = self.hessian_chol
        else:
            hess = None
        for key, value in default_opt_kws.items():
            if key not in opt_kws.keys():
                opt_kws[key] = value
                
        optimizer = sp.optimize.minimize(self.loglike_c, self.theta, 
                                              jac=self.gradient_chol, hess=hess, 
                                              options=opt_kws, bounds=self.bounds,
                                              method='trust-constr')
        theta_chol = optimizer.x
        theta = inverse_transform_theta(theta_chol, self.dims, self.indices)
        
        beta, XtWX_inv, u, G, R, Rinv, V, Vinv = self._compute_effects(theta)
        params = np.concatenate([beta, theta])
        self.theta, self.beta, self.u, self.params = theta, beta, u, params
        self.Hinv_beta = XtWX_inv
        self.se_beta = np.sqrt(np.diag(XtWX_inv))
        self._G, self._R, self._Rinv, self._V, self._Vinv = G, R, Rinv, V, Vinv
        
    def _post_fit(self):
        Htheta = self.hessian(self.theta)
        self.Hinv_theta = np.linalg.pinv(Htheta)
        self.se_theta = np.sqrt(np.diag(self.Hinv_theta))
        self.se_params = np.concatenate([self.se_beta, self.se_theta])        
    
    def predict(self, X=None, Z=None):
        if X is None:
            X = self.X
        if Z is None:
            Z = self.Z
        return X.dot(self.beta)+Z.dot(self.u)

    
    
class GLMM(WLMM):
    '''
    Currently an ineffecient implementation of a GLMM, mostly done 
    for fun.  A variety of implementations for GLMMs have been proposed in the
    literature, and a variety of names have been used to refer to each model;
    the implementation here is based of off linearization using a taylor
    approximation of the error (assumed to be gaussian) around the current
    estimates of fixed and random effects.  This type of approach may be 
    referred to as penalized quasi-likelihood, or pseudo-likelihood, and 
    may be abbreviated PQL, REPL, RPL, or RQL.

    '''
    def __init__(self, formula, data, weights=None, fam=None):
        if isinstance(fam, ExponentialFamily) is False:
            fam = fam()
        self.f = fam
        self.mod = WLMM(formula, data, weights=None)        
        self.theta_init = self.mod.theta.copy()
        
        self.y = self.mod.y
        self.non_continuous = [isinstance(self.f, Binomial),
                               isinstance(self.f, NegativeBinomial),
                               isinstance(self.f, Poisson)]
        if np.any(self.non_continuous):
            self.mod.bounds = self.mod.bounds[:-1]+[(1, 1)]
            self.mod.fix_resid_cov=True
        self.mod._fit()
        
        if isinstance(self.f, Binomial):
            self.mod.u /= np.linalg.norm(self.mod.u)
        self._nfixed_params = self.mod.X.shape[1]
        self._n_obs = self.mod.X.shape[0]
        self._n_cov_params = len(self.mod.bounds)
        self._df1 = self._n_obs - self._nfixed_params
        self._df2 = self._n_obs - self._nfixed_params - self._n_cov_params - 1
        self._ll_const = self._df1 / 2 * np.log(2*np.pi)
        
    
    def _update_model(self, W, nu):
        nu = _check_shape(nu, 2)
        self.mod.weights = sps.csc_matrix(W)
        self.mod.weights_inv = sps.csc_matrix(np.diag(1.0/np.diag((W))))
        self.mod.y = nu
        self.mod.Xty = self.mod.X.T.dot(nu)
        self.mod.Zty = self.mod.Z.T.dot(nu)
        self.mod.theta = self.theta_init
        self.mod.yty = nu.T.dot(nu)
        
        
    
    def _get_pseudovar(self):
        eta = self.mod.predict()
        mu = self.f.inv_link(eta)
        var_mu = _check_shape(self.f.var_func(mu=mu), 1)
        gp = self.f.dlink(mu)
        nu = eta + gp * (_check_shape(self.y, 1) - mu)
        W = np.diag(np.sqrt(var_mu * (self.f.dlink(mu)**2)))
        return W, nu
    
    def _sandwich_cov(self, r):
        M = self.mod.Hinv_beta
        X = self.mod.X
        Vinv = self.mod.Vinv
        B = (Vinv.dot(X))
        d = _check_shape(r, 2)**2
        B = B * d
        C = B.T.dot(B)
        Cov = M.dot(C).dot(M)
        return Cov

    
    def predict(self, fixed=True, random=True):
        yhat = 0.0
        if fixed:
            yhat += self.mod.X.dot(self.mod.beta)
        if random:
            yhat += self.mod.Z.dot(self.mod.u)
        return yhat        
        

    def fit(self, n_iters=200, tol=1e-3, optimizer_kwargs={}, 
            verbose_outer=True, hess=False):
        if 'options' in optimizer_kwargs.keys():
            if 'verbose' not in optimizer_kwargs['options'].keys():
                optimizer_kwargs['options']['verbose'] = 0
        else:
            optimizer_kwargs['options'] = dict(verbose=0)
        
        if hess:
            hessian = self.mod.hessian
        else:
            hessian = None
            
        theta = self.mod.theta.copy()
        fit_hist = {}
        for i in range(n_iters):
            W, nu = self._get_pseudovar()
            self._update_model(W, nu)
            self.mod._fit(opt_kws={}, use_hess=hessian)
            tvar = (np.linalg.norm(theta)+np.linalg.norm(self.mod.theta))
            eps = np.linalg.norm(theta - self.mod.theta) / tvar
            fit_hist[i] = dict(param_change=eps, theta=self.mod.theta,
                               nu=nu)
            if verbose_outer:
                print(eps)
            if eps < tol:
                break
            theta = self.mod.theta.copy()
        self.mod._post_fit()
        self.res = get_param_table(self.mod.params, self.mod.se_params, 
                                   self.mod.X.shape[0]-len(self.mod.params))
        
        
        eta_fe = self.predict(fixed=True, random=False)
        eta = self.predict(fixed=True, random=True)
        mu = self.f.inv_link(eta)
        gp = self.f.dlink(mu)
        var_mu  =  _check_shape(self.f.var_func(mu=mu), 1)
        r_eta_fe = _check_shape(self.mod.y, 1) - eta_fe

        generalized_chi2 = r_eta_fe.T.dot(self.mod._Vinv.dot(r_eta_fe))
        resids_raw_linear = _check_shape(self.mod.y, 1) - eta
        resids_raw_mean = _check_shape(self.y, 1) - mu
        
        var_pearson_linear = self.mod._R.diagonal() / gp**2
        var_pearson_mean = var_mu
        
        resids_pearson_linear = resids_raw_linear / np.sqrt(var_pearson_linear)
        resids_pearson_mean = resids_raw_mean / np.sqrt(var_pearson_mean)
        
        pll = self.mod.loglike(self.mod.theta) / -2.0 - self._ll_const
        aicc = -2 * pll + 2 * self._n_cov_params  * self._df1 / self._df2
        bic = -2 * pll + self._n_cov_params * np.log(self._df1)
        self.sumstats = dict(generalized_chi2=generalized_chi2,
                             pseudo_loglike=pll,
                             AICC=aicc,
                             BIC=bic)
        self.resids = dict(resids_raw_linear=resids_raw_linear,
                           resids_raw_mean=resids_raw_mean,
                           resids_pearson_linear=resids_pearson_linear,
                           resids_pearson_mean=resids_pearson_mean)
        
 
"""       
from pystats.utilities.random_corr import vine_corr
from pystats.tests.test_data import generate_data
from pylmm.pylmm.lmm import LME
from pylmm.pylmm.glmm import WLME, GLMM


from pystats.utilities import numerical_derivs


    
 

np.set_printoptions(precision=3, suppress=True, linewidth=200)
formula = "y~1+x1+x2+(1+x3|id1)+(1+x4|id2)"
model_dict = {}
model_dict['gcov'] = {'id1':invech(np.array([2., 0.4, 2.])),
                      'id2':invech(np.array([2.,-0.4, 2.]))}

model_dict['ginfo'] = {'id1':dict(n_grp=200, n_per=10),
                       'id2':dict(n_grp=400, n_per=5)}
 
model_dict['mu'] = np.zeros(4)
model_dict['vcov'] = vine_corr(4, 20)
model_dict['beta'] = np.array([1, -1, 1])
model_dict['n_obs'] = 2000
data, formula = generate_data(formula, model_dict, r=0.6**0.5)




model_original = LME(formula, data)
model_cholesky = LME3(formula, data)
model_original._fit()
model_cholesky._fit(opt_kws=dict(verbose=3))
model_cholesky._post_fit()

model_original.se_params
model_cholesky.se_params




"""

